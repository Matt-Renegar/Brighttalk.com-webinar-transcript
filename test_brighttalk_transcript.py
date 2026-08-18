"""Tests for brighttalk_transcript.py.

Everything that touches the network, ffmpeg, or faster-whisper is mocked so
the suite runs fast and offline. Only the pure parsing/formatting functions
and the main() orchestration logic (with its dependencies substituted) are
exercised.
"""

# pylint: disable=missing-function-docstring,redefined-outer-name,unused-argument

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
import requests

import brighttalk_transcript as bt


# ---------------------------------------------------------------------------
# parse_webcast_url
# ---------------------------------------------------------------------------


def test_parse_webcast_url_extracts_ids():
    channel_id, webcast_id = bt.parse_webcast_url(
        "https://www.brighttalk.com/webcast/19773/645912"
    )
    assert channel_id == "19773"
    assert webcast_id == "645912"


def test_parse_webcast_url_ignores_query_string():
    channel_id, webcast_id = bt.parse_webcast_url(
        "https://www.brighttalk.com/webcast/19773/645912?utm_source=test"
    )
    assert (channel_id, webcast_id) == ("19773", "645912")


def test_parse_webcast_url_rejects_other_domains():
    with pytest.raises(bt.TranscriptError):
        bt.parse_webcast_url("https://example.com/webcast/19773/645912")


def test_parse_webcast_url_rejects_missing_ids():
    with pytest.raises(bt.TranscriptError):
        bt.parse_webcast_url("https://www.brighttalk.com/channel/19773")


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Cybersecurity Roundtable: Securing Data", "cybersecurity-roundtable-securing-data"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("Already-slugged", "already-slugged"),
        ("!!!", "webcast"),
        ("", "webcast"),
    ],
)
def test_slugify(title, expected):
    assert bt.slugify(title) == expected


# ---------------------------------------------------------------------------
# fetch_title
# ---------------------------------------------------------------------------


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error")
    else:
        resp.raise_for_status.side_effect = None
    return resp


def test_fetch_title_returns_title():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={"title": "My Webcast"})
    assert bt.fetch_title(session, "1", "2") == "My Webcast"


def test_fetch_title_falls_back_when_missing():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    assert bt.fetch_title(session, "1", "2") == "webcast-2"


def test_fetch_title_not_found_raises_transcript_error():
    session = MagicMock()
    session.get.return_value = _mock_response(status_code=404)
    with pytest.raises(bt.TranscriptError):
        bt.fetch_title(session, "1", "2")


def test_fetch_title_other_error_propagates():
    session = MagicMock()
    session.get.return_value = _mock_response(status_code=500)
    with pytest.raises(requests.HTTPError):
        bt.fetch_title(session, "1", "2")


# ---------------------------------------------------------------------------
# pick_audio_variant_url
# ---------------------------------------------------------------------------

MASTER_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=8490792,RESOLUTION=1920x1080,CODECS="avc1.640029,mp4a.40.2"
index_1.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1179428,RESOLUTION=640x360,CODECS="avc1.640029,mp4a.40.2"
index_3.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=142092,CODECS="mp4a.40.2"
index_4.m3u8
"""


def test_pick_audio_variant_url_prefers_audio_only():
    session = MagicMock()
    session.get.return_value = _mock_response(text=MASTER_PLAYLIST)
    url = bt.pick_audio_variant_url(session, "19773", "645912")
    assert url == f"{bt.CDN_HOST}/19773-645912/index_4.m3u8"


def test_pick_audio_variant_url_picks_lowest_bandwidth_when_no_audio_only():
    video_only_playlist = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=8490792,RESOLUTION=1920x1080,CODECS="avc1.640029,mp4a.40.2"
index_1.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1179428,RESOLUTION=640x360,CODECS="avc1.640029,mp4a.40.2"
index_3.m3u8
"""
    session = MagicMock()
    session.get.return_value = _mock_response(text=video_only_playlist)
    url = bt.pick_audio_variant_url(session, "19773", "645912")
    assert url == f"{bt.CDN_HOST}/19773-645912/index_3.m3u8"


def test_pick_audio_variant_url_manifest_forbidden_raises_transcript_error():
    session = MagicMock()
    session.get.return_value = _mock_response(status_code=403)
    with pytest.raises(bt.TranscriptError):
        bt.pick_audio_variant_url(session, "19773", "645912")


def test_pick_audio_variant_url_empty_manifest_raises_transcript_error():
    session = MagicMock()
    session.get.return_value = _mock_response(text="#EXTM3U\n")
    with pytest.raises(bt.TranscriptError):
        bt.pick_audio_variant_url(session, "19773", "645912")


# ---------------------------------------------------------------------------
# find_ffmpeg
# ---------------------------------------------------------------------------


def test_find_ffmpeg_uses_path_first(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda name: r"C:\tools\ffmpeg.exe")
    assert bt.find_ffmpeg() == r"C:\tools\ffmpeg.exe"


def test_find_ffmpeg_raises_when_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(bt.shutil, "which", lambda name: None)
    monkeypatch.setattr(bt.os.path, "isdir", lambda path: False)
    with pytest.raises(bt.TranscriptError):
        bt.find_ffmpeg()


# ---------------------------------------------------------------------------
# download_audio
# ---------------------------------------------------------------------------


def test_download_audio_success(monkeypatch):
    completed = MagicMock(returncode=0, stderr="")
    run_mock = MagicMock(return_value=completed)
    monkeypatch.setattr(bt.subprocess, "run", run_mock)

    bt.download_audio("ffmpeg", "https://example.com/stream.m3u8", "out.wav")

    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd[0] == "ffmpeg"
    assert "-i" in cmd and "https://example.com/stream.m3u8" in cmd
    assert cmd[-1] == "out.wav"


def test_download_audio_failure_raises_transcript_error(monkeypatch):
    completed = MagicMock(returncode=1, stderr="boom")
    monkeypatch.setattr(bt.subprocess, "run", MagicMock(return_value=completed))

    with pytest.raises(bt.TranscriptError, match="boom"):
        bt.download_audio("ffmpeg", "https://example.com/stream.m3u8", "out.wav")


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------


def test_transcribe_joins_segment_text(monkeypatch):
    segment_a = MagicMock(text=" Hello there. ")
    segment_b = MagicMock(text=" General Kenobi. ")
    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([segment_a, segment_b], MagicMock())

    fake_module = MagicMock()
    fake_module.WhisperModel.return_value = fake_model
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    result = bt.transcribe("audio.wav", "small")

    assert result == "Hello there. General Kenobi."
    fake_module.WhisperModel.assert_called_once_with("small", device="cpu", compute_type="int8")


def test_transcribe_skips_empty_segments(monkeypatch):
    segment_a = MagicMock(text="   ")
    segment_b = MagicMock(text="Real text")
    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([segment_a, segment_b], MagicMock())

    fake_module = MagicMock()
    fake_module.WhisperModel.return_value = fake_model
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    assert bt.transcribe("audio.wav", "small") == "Real text"


# ---------------------------------------------------------------------------
# main (end-to-end with all external dependencies mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def mocked_pipeline(monkeypatch):
    """Patch every function main() calls out to, so it runs with no I/O."""
    monkeypatch.setattr(bt, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(bt, "fetch_title", lambda session, c, w: "Test Webcast")
    monkeypatch.setattr(
        bt, "pick_audio_variant_url", lambda session, c, w: "https://cdn/audio.m3u8"
    )
    monkeypatch.setattr(bt, "download_audio", lambda ffmpeg, url, out: None)
    monkeypatch.setattr(bt, "transcribe", lambda wav_path, model: "This is the transcript.")
    monkeypatch.setattr(bt.os, "remove", lambda path: None)


def test_main_writes_transcript_file(mocked_pipeline, tmp_path, capsys):
    url = "https://www.brighttalk.com/webcast/19773/645912"
    exit_code = bt.main([url, "--out", str(tmp_path)])

    assert exit_code == 0
    out_file = tmp_path / "brighttalk-transcript-test-webcast.txt"
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "Test Webcast" in content
    assert url in content
    assert "This is the transcript." in content

    captured = capsys.readouterr()
    assert "Saved transcript (4 words)" in captured.out


def test_main_print_flag_echoes_transcript(mocked_pipeline, tmp_path, capsys):
    url = "https://www.brighttalk.com/webcast/19773/645912"
    bt.main([url, "--out", str(tmp_path), "--print"])

    captured = capsys.readouterr()
    assert "This is the transcript." in captured.out


def test_main_returns_1_on_transcript_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bt, "find_ffmpeg", lambda: "ffmpeg")
    def raise_transcript_error(session, c, w):
        raise bt.TranscriptError("nope")

    monkeypatch.setattr(bt, "fetch_title", raise_transcript_error)

    exit_code = bt.main(
        ["https://www.brighttalk.com/webcast/19773/645912", "--out", str(tmp_path)]
    )

    assert exit_code == 1
    assert "Error: nope" in capsys.readouterr().err


def test_main_returns_1_on_request_exception(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bt, "find_ffmpeg", lambda: "ffmpeg")

    def raise_connection_error(session, c, w):
        raise requests.ConnectionError("DNS lookup failed")

    monkeypatch.setattr(bt, "fetch_title", raise_connection_error)

    exit_code = bt.main(
        ["https://www.brighttalk.com/webcast/19773/645912", "--out", str(tmp_path)]
    )

    assert exit_code == 1
    assert "Network error" in capsys.readouterr().err
