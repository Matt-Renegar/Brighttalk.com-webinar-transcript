"""Get the full transcript of a BrightTalk webcast by transcribing its audio locally.

BrightTalk only shows a "CC" button for webcasts the presenter actually ran
auto-transcription on; many recordings (like the one this was built against)
have no captions at all despite the channel supporting the feature. Rather
than depend on that, this downloads the webcast's audio track directly from
its public CDN manifest and transcribes it locally with faster-whisper.

Usage:
    uv run brighttalk_transcript.py <webcast-url> [--model small] [--out DIR] [--keep-audio]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urljoin, urlparse

import requests

BASE_URL = "https://www.brighttalk.com"
CDN_HOST = "https://d3du34tj8zwe0z.cloudfront.net"
WEBCAST_URL_RE = re.compile(r"/webcast/(?P<channel_id>\d+)/(?P<webcast_id>\d+)")
STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:(?P<attrs>.*)")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class TranscriptError(RuntimeError):
    """Raised for any expected failure in the download/transcription pipeline.

    Covers malformed URLs, missing webcasts, unreadable manifests, and
    ffmpeg/transcription failures. Caught in `main` and reported as a clean
    error message rather than a traceback.
    """


def parse_webcast_url(url: str) -> tuple[str, str]:
    """Extract the channel ID and webcast ID from a BrightTalk webcast URL.

    Args:
        url: A BrightTalk URL, e.g. "https://www.brighttalk.com/webcast/19773/645912".

    Returns:
        A (channel_id, webcast_id) tuple of digit strings.

    Raises:
        TranscriptError: If the URL isn't a brighttalk.com URL, or doesn't
            contain a /webcast/<channelId>/<webcastId> path segment.
    """
    parsed = urlparse(url)
    if "brighttalk.com" not in parsed.netloc:
        raise TranscriptError(f"Not a brighttalk.com URL: {url}")
    match = WEBCAST_URL_RE.search(parsed.path)
    if not match:
        raise TranscriptError(
            f"Could not find /webcast/<channelId>/<webcastId> in URL path: {parsed.path}"
        )
    return match.group("channel_id"), match.group("webcast_id")


def fetch_title(session: requests.Session, channel_id: str, webcast_id: str) -> str:
    """Look up the display title of a webcast via BrightTalk's public communication API.

    Args:
        session: A requests.Session to issue the request with.
        channel_id: The BrightTalk channel ID.
        webcast_id: The BrightTalk webcast (communication) ID.

    Returns:
        The webcast's title, or a fallback "webcast-<id>" string if the API
        response doesn't include one.

    Raises:
        TranscriptError: If the webcast doesn't exist (404).
        requests.HTTPError: For other non-2xx responses.
    """
    url = f"{BASE_URL}/service/communication/v1/channel/{channel_id}/communication/{webcast_id}/public"
    resp = session.get(url, timeout=15)
    if resp.status_code == 404:
        raise TranscriptError(f"Webcast {channel_id}/{webcast_id} not found.")
    resp.raise_for_status()
    return resp.json().get("title", f"webcast-{webcast_id}")


def pick_audio_variant_url(
    session: requests.Session, channel_id: str, webcast_id: str
) -> str:
    """Fetch the HLS master playlist and return the smallest (ideally audio-only) variant URL.

    BrightTalk serves recordings from a fixed CloudFront/S3 layout:
    {CDN_HOST}/{channelId}-{webcastId}/index.m3u8, publicly readable with no
    auth (confirmed via a captured browser HAR). Picking the smallest/audio-only
    stream keeps the download small since only audio is needed for transcription.

    Args:
        session: A requests.Session to issue the request with.
        channel_id: The BrightTalk channel ID.
        webcast_id: The BrightTalk webcast ID.

    Returns:
        The absolute URL of the chosen HLS variant playlist, preferring an
        audio-only stream and, among ties, the lowest bandwidth.

    Raises:
        TranscriptError: If the manifest can't be read (403/404) or contains
            no stream variants.
        requests.HTTPError: For other non-2xx responses.
    """
    master_url = f"{CDN_HOST}/{channel_id}-{webcast_id}/index.m3u8"
    resp = session.get(master_url, timeout=15)
    if resp.status_code == 403 or resp.status_code == 404:
        raise TranscriptError(
            f"Could not read the video manifest at {master_url} ({resp.status_code}). "
            "This channel may host video on a different CDN than the one this script "
            "assumes; capture a browser HAR while playing the video and look for the "
            ".m3u8 request to find the real URL."
        )
    resp.raise_for_status()

    lines = resp.text.splitlines()
    variants: list[tuple[int, bool, str]] = []
    for i, line in enumerate(lines):
        m = STREAM_INF_RE.match(line)
        if not m or i + 1 >= len(lines):
            continue
        attrs = m.group("attrs")
        bandwidth_m = re.search(r"BANDWIDTH=(\d+)", attrs)
        bandwidth = int(bandwidth_m.group(1)) if bandwidth_m else 0
        is_audio_only = "RESOLUTION=" not in attrs
        variant_url = urljoin(master_url, lines[i + 1].strip())
        variants.append((bandwidth, is_audio_only, variant_url))

    if not variants:
        raise TranscriptError(f"No stream variants found in manifest at {master_url}.")

    variants.sort(key=lambda v: (not v[1], v[0]))
    return variants[0][2]


def find_ffmpeg() -> str:
    """Locate an ffmpeg executable to use for audio extraction.

    Checks PATH first, then falls back to searching the Windows winget
    package install location for a WinGet-installed ffmpeg.exe.

    Returns:
        The path to the ffmpeg executable.

    Raises:
        TranscriptError: If no ffmpeg executable can be found.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    winget_root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_root):
        for root, _dirs, files in os.walk(winget_root):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")

    raise TranscriptError(
        "ffmpeg was not found on PATH. Install it (e.g. `winget install Gyan.FFmpeg`) "
        "and restart your shell, or pass its path via the FFMPEG_PATH environment variable."
    )


def download_audio(ffmpeg_path: str, stream_url: str, out_wav_path: str) -> None:
    """Download an HLS stream and convert it to a mono 16kHz PCM WAV file.

    Runs ffmpeg to pull the audio track from the given stream URL, stripping
    video and resampling to the format faster-whisper expects.

    Args:
        ffmpeg_path: Path to the ffmpeg executable.
        stream_url: URL of the HLS variant playlist (or media) to download.
        out_wav_path: Destination path for the converted .wav file.

    Raises:
        TranscriptError: If ffmpeg exits with a non-zero status.
    """
    cmd = [
        ffmpeg_path,
        "-y",
        "-loglevel",
        "error",
        "-i",
        stream_url,
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        out_wav_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TranscriptError(
            f"ffmpeg failed to download/convert audio:\n{result.stderr}"
        )


def transcribe(wav_path: str, model_size: str) -> str:
    """Transcribe a WAV file to text using faster-whisper on CPU.

    Loads the given faster-whisper model with int8 quantization, runs
    voice-activity-filtered transcription, and joins the resulting segments
    into a single space-separated transcript.

    Args:
        wav_path: Path to the mono 16kHz WAV file to transcribe.
        model_size: faster-whisper model size (e.g. "tiny", "small", "large-v3").

    Returns:
        The full transcript text, or an empty string if no speech was detected.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(wav_path, vad_filter=True)

    paragraphs: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            paragraphs.append(text)
    return " ".join(paragraphs)


def slugify(title: str) -> str:
    """Convert a title into a filesystem-safe slug.

    Replaces runs of non-alphanumeric characters with a single hyphen,
    lowercases the result, and strips leading/trailing hyphens.

    Args:
        title: The string to slugify (e.g. a webcast title).

    Returns:
        The slugified string, or "webcast" if the input reduces to nothing.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "webcast"


def main(argv: list[str] | None = None) -> int:
    """Run the CLI: parse args, download and transcribe a webcast, and write the transcript.

    Orchestrates the full pipeline: locate ffmpeg, resolve the webcast's
    title and audio stream, download and convert the audio, transcribe it,
    write the transcript to disk, and optionally print it to stdout.

    Args:
        argv: Command-line arguments to parse (defaults to sys.argv[1:] when None).

    Returns:
        Process exit code: 0 on success, 1 if a TranscriptError or
        requests.HTTPError occurs.
    """
    parser = argparse.ArgumentParser(
        description="Transcribe a BrightTalk webcast locally."
    )
    parser.add_argument(
        "url",
        help="BrightTalk webcast URL, e.g. https://www.brighttalk.com/webcast/19773/645912",
    )
    parser.add_argument(
        "--model",
        default="small",
        choices=["tiny", "base", "small", "medium", "large-v3"],
        help="faster-whisper model size (default: small). Larger = more accurate, much slower on CPU.",
    )
    parser.add_argument(
        "--out", default=".", help="Output directory (default: current directory)."
    )
    parser.add_argument(
        "--keep-audio", action="store_true", help="Keep the downloaded .wav audio file."
    )
    parser.add_argument(
        "--print",
        dest="do_print",
        action="store_true",
        help="Print the transcript to stdout.",
    )
    args = parser.parse_args(argv)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    try:
        ffmpeg_path = find_ffmpeg()
        channel_id, webcast_id = parse_webcast_url(args.url)
        title = fetch_title(session, channel_id, webcast_id)
        print(f'Webcast: "{title}" (channel {channel_id}, webcast {webcast_id})')

        stream_url = pick_audio_variant_url(session, channel_id, webcast_id)

        os.makedirs(args.out, exist_ok=True)
        slug = slugify(title)
        wav_path = os.path.join(
            args.out if args.keep_audio else tempfile.gettempdir(),
            f"brighttalk-{slug}.wav",
        )

        print("Downloading audio...")
        download_audio(ffmpeg_path, stream_url, wav_path)

        print(
            f"Transcribing with faster-whisper ({args.model} model, CPU)... this can take a while."
        )
        transcript = transcribe(wav_path, args.model)

        if not args.keep_audio:
            os.remove(wav_path)

        if not transcript:
            raise TranscriptError("Transcription produced no text.")

        out_path = os.path.join(args.out, f"brighttalk-transcript-{slug}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"{title}\n")
            f.write(f"Source: {args.url}\n\n")
            f.write(transcript)

        word_count = len(transcript.split())
        print(f"Saved transcript ({word_count} words) to: {out_path}")

        if args.do_print:
            print("\n--- Transcript ---\n")
            print(transcript)

    except TranscriptError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(
            f"Network error talking to BrightTalk or its CDN: {exc}\n"
            f"If this was the CDN request ({CDN_HOST}), BrightTalk may have changed "
            "hosting; capture a fresh browser HAR while playing the video to find "
            "the current manifest URL.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
