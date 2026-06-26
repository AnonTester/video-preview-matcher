"""
Tests 04_serve.py's playback-remux decision logic: _parse_playability()
and _safe_to_remux() — the pure functions behind _ensure_playable()'s
/stream/{video_id} fallback, separated from the real ffprobe/ffmpeg
subprocess calls so they're testable without either.

Covers two real incidents:
  - The original fix: a real MP4 file with a missing/zero codec FourCC
    tag in its sample description plays in ffmpeg/VLC (extradata
    fallback) but not in a browser's native demuxer.
  - Video #5855: a file named *.mp4 that's actually raw MPEG-TS — its
    codec_tag is a real, non-zero MPEG-TS stream_type, so the original
    fix's codec_tag-only check never caught it. Also covers the
    necessary safety fix found while building that one: ffprobe reports
    codec_tag=0x0000 for *every* Matroska-family file unconditionally
    (no FourCC-tag concept there at all), so naively extending the
    broken-tag check to ignore container would have flagged every real
    .mkv/.webm file in the library as "broken".

Loads 04_serve.py directly via importlib (its filename starts with a
digit, so it can't be `import`ed normally). Run from project root:

    python3 tests/playback_remux_test.py
"""

import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("serve_mod", PROJECT_ROOT / "src" / "04_serve.py")
serve_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve_mod)


def _ffprobe_json(format_name, video_codec="h264", video_tag="0x0000", audio_codec="aac", with_audio=True):
    streams = [{"codec_name": video_codec, "codec_type": "video", "codec_tag": video_tag}]
    if with_audio:
        streams.append({"codec_name": audio_codec, "codec_type": "audio", "codec_tag": "0x0000"})
    return {"streams": streams, "format": {"format_name": format_name}}


def test_parse_playability_flags_broken_mp4_codec_tag():
    data = _ffprobe_json("mov,mp4,m4a,3gp,3g2,mj2", video_tag="0x0000")
    info = serve_mod._parse_playability(data)
    assert info["needs_remux"] is True
    assert info["video_codec"] == "h264"
    assert info["audio_codec"] == "aac"
    print("test_parse_playability_flags_broken_mp4_codec_tag: OK")


def test_parse_playability_leaves_valid_mp4_codec_tag_alone():
    # A real, non-zero MP4 FourCC tag (avc1 in hex) — no defect, no remux.
    data = _ffprobe_json("mov,mp4,m4a,3gp,3g2,mj2", video_tag="0x31637661")
    info = serve_mod._parse_playability(data)
    assert info["needs_remux"] is False
    print("test_parse_playability_leaves_valid_mp4_codec_tag_alone: OK")


def test_parse_playability_flags_mpeg_ts_despite_nonzero_codec_tag():
    # video #5855: codec_tag is a real, non-zero MPEG-TS stream_type
    # (0x1b = H.264) — the broken-tag check alone would miss this.
    data = _ffprobe_json("mpegts", video_tag="0x001b")
    info = serve_mod._parse_playability(data)
    assert info["needs_remux"] is True
    print("test_parse_playability_flags_mpeg_ts_despite_nonzero_codec_tag: OK")


def test_parse_playability_flags_flv_container():
    data = _ffprobe_json("flv", video_tag="0x0000")
    info = serve_mod._parse_playability(data)
    assert info["needs_remux"] is True
    print("test_parse_playability_flags_flv_container: OK")


def test_parse_playability_exempts_matroska_despite_zero_codec_tag():
    # ffprobe always reports codec_tag=0x0000 for Matroska files — this
    # must NOT be treated as the broken-MP4-tag defect, or every real
    # .mkv file in the library would get needlessly (and, for non-h264
    # codecs, dangerously) remuxed the first time it's ever streamed.
    data = _ffprobe_json("matroska,webm", video_tag="0x0000")
    info = serve_mod._parse_playability(data)
    assert info["needs_remux"] is False
    print("test_parse_playability_exempts_matroska_despite_zero_codec_tag: OK")


def test_parse_playability_exempts_webm_despite_zero_codec_tag():
    data = _ffprobe_json("matroska,webm", video_codec="vp9", video_tag="0x0000",
                          audio_codec="opus")
    info = serve_mod._parse_playability(data)
    assert info["needs_remux"] is False
    print("test_parse_playability_exempts_webm_despite_zero_codec_tag: OK")


def test_parse_playability_handles_missing_video_stream():
    data = {"streams": [], "format": {"format_name": "mpegts"}}
    info = serve_mod._parse_playability(data)
    assert info == {"needs_remux": False, "video_codec": None, "audio_codec": None}
    print("test_parse_playability_handles_missing_video_stream: OK")


def test_parse_playability_handles_no_audio_stream():
    data = _ffprobe_json("mpegts", video_tag="0x001b", with_audio=False)
    info = serve_mod._parse_playability(data)
    assert info["needs_remux"] is True
    assert info["audio_codec"] is None
    print("test_parse_playability_handles_no_audio_stream: OK")


def test_safe_to_remux_allows_h264_aac():
    assert serve_mod._safe_to_remux({"video_codec": "h264", "audio_codec": "aac"}) is True
    print("test_safe_to_remux_allows_h264_aac: OK")


def test_safe_to_remux_allows_h264_no_audio():
    assert serve_mod._safe_to_remux({"video_codec": "h264", "audio_codec": None}) is True
    print("test_safe_to_remux_allows_h264_no_audio: OK")


def test_safe_to_remux_rejects_non_h264_video():
    # This library does have a handful of av1/hevc/vp9 videos — forcing
    # the avc1 tag onto one of those would mislabel it, not fix it.
    for codec in ("hevc", "av1", "vp9"):
        assert serve_mod._safe_to_remux({"video_codec": codec, "audio_codec": "aac"}) is False, codec
    print("test_safe_to_remux_rejects_non_h264_video: OK")


def test_safe_to_remux_rejects_non_aac_audio():
    assert serve_mod._safe_to_remux({"video_codec": "h264", "audio_codec": "mp3"}) is False
    print("test_safe_to_remux_rejects_non_aac_audio: OK")


if __name__ == "__main__":
    test_parse_playability_flags_broken_mp4_codec_tag()
    test_parse_playability_leaves_valid_mp4_codec_tag_alone()
    test_parse_playability_flags_mpeg_ts_despite_nonzero_codec_tag()
    test_parse_playability_flags_flv_container()
    test_parse_playability_exempts_matroska_despite_zero_codec_tag()
    test_parse_playability_exempts_webm_despite_zero_codec_tag()
    test_parse_playability_handles_missing_video_stream()
    test_parse_playability_handles_no_audio_stream()
    test_safe_to_remux_allows_h264_aac()
    test_safe_to_remux_allows_h264_no_audio()
    test_safe_to_remux_rejects_non_h264_video()
    test_safe_to_remux_rejects_non_aac_audio()
    print("\nAll playback-remux tests passed.")
