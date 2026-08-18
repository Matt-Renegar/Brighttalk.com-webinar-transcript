# BrightTalk Webcast Transcript

Get a full text transcript of a BrightTalk webcast by downloading its audio
and transcribing it locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

BrightTalk only shows a working "CC" button for webcasts the presenter
actually ran auto-transcription on, and many recordings have none at all.
This tool doesn't depend on that — it pulls the webcast's audio track
directly from its public CDN manifest and transcribes it on your own
machine, so it works for any BrightTalk recording and never requires
logging in.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages the Python environment and dependencies)
- [ffmpeg](https://ffmpeg.org/) on your `PATH`
  - Windows: `winget install Gyan.FFmpeg` (restart your shell afterwards)
  - macOS: `brew install ffmpeg`
  - Linux: install via your package manager, e.g. `apt install ffmpeg`

No BrightTalk account or login is required.

## Usage

```bash
uv run brighttalk_transcript.py <webcast-url>
```

Example:

```bash
uv run brighttalk_transcript.py "https://www.brighttalk.com/webcast/19773/645912"
```

The first run downloads the chosen Whisper model (one-time, cached
afterwards). The tool then downloads the audio, transcribes it, and writes
a `brighttalk-transcript-<title>.txt` file in the output directory.

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--model {tiny,base,small,medium,large-v3}` | `small` | Whisper model size. Larger models are more accurate but much slower on CPU. |
| `--out DIR` | `.` | Directory to write the transcript (and audio, if kept) to. |
| `--keep-audio` | off | Keep the downloaded `.wav` audio file instead of deleting it after transcription. |
| `--print` | off | Also print the transcript to stdout. |

### Notes

- Transcription runs on CPU, so a ~1 hour webcast can take a while,
  especially with `medium` or `large-v3`. `small` is a reasonable
  accuracy/speed default; try `base` if you want faster, rougher results.
- The tool assumes BrightTalk serves recordings from a fixed CloudFront
  CDN layout. If a webcast fails at the "downloading audio" step, that
  layout may have changed — capture a browser HAR while playing the video
  (DevTools → Network → Preserve log → play → Save all as HAR) and look for
  the `.m3u8` request to find the current manifest URL.
