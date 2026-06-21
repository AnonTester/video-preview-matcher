"""
Regression test for the scene-extraction timeout scaling with video
duration. A flat 300s timeout wasn't enough for every video in a real
library — confirmed live, two ~45-minute 720p files genuinely exceeded a
flat 300s under software decode (no hwaccel), logged via subprocess.log
as "300.13s, TIMEOUT". See scene_extraction_timeout() in 02_fingerprint.py.
Loads 02_fingerprint.py directly via importlib (its filename starts with
a digit, so it can't be `import`ed normally). Run from project root:

    python3 tests/scene_extraction_timeout_test.py
"""

import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("fp_mod", PROJECT_ROOT / "src" / "02_fingerprint.py")
fp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp_mod)


def test_short_clip_gets_the_floor():
    assert fp_mod.scene_extraction_timeout(60) == 300
    print("test_short_clip_gets_the_floor: OK")


def test_unknown_duration_gets_the_floor():
    assert fp_mod.scene_extraction_timeout(None) == 300
    assert fp_mod.scene_extraction_timeout(0) == 300
    print("test_unknown_duration_gets_the_floor: OK")


def test_long_video_scales_with_duration():
    # the actual real-world case that motivated this: a ~46 minute video
    assert fp_mod.scene_extraction_timeout(2757) == 2757
    print("test_long_video_scales_with_duration: OK")


def test_pathological_duration_is_capped():
    assert fp_mod.scene_extraction_timeout(100_000) == 3600
    print("test_pathological_duration_is_capped: OK")


if __name__ == "__main__":
    test_short_clip_gets_the_floor()
    test_unknown_duration_gets_the_floor()
    test_long_video_scales_with_duration()
    test_pathological_duration_is_capped()
    print("\nAll scene-extraction-timeout tests passed.")
