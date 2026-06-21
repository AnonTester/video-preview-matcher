"""
Regression test for the blank-frame pHash bug: a fully uniform image's
DCT is zero everywhere, so the median-threshold step produces the same
degenerate all-zero hash for every blank frame in the library, regardless
of which video it came from — two unrelated movies' blank intro frames
then "match" with Hamming distance 0. Confirmed against real library data
(see phash.is_blank()'s docstring and 02_fingerprint.py's blank-frame
guard). Run from project root:

    python3 tests/phash_test.py
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import phash as ph  # noqa: E402


def test_solid_black_image_is_blank():
    img = Image.new("RGB", (320, 180), (0, 0, 0))
    assert ph.is_blank(img)
    print("test_solid_black_image_is_blank: OK")


def test_solid_color_image_is_blank():
    img = Image.new("RGB", (320, 180), (128, 64, 200))
    assert ph.is_blank(img)
    print("test_solid_color_image_is_blank: OK")


def test_textured_image_is_not_blank():
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 256, size=(180, 320, 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB")
    assert not ph.is_blank(img)
    print("test_textured_image_is_not_blank: OK")


def test_blank_frames_from_two_unrelated_videos_would_have_collided():
    """The actual bug, reproduced: two solid-black frames (standing in for
    two unrelated videos' blank intro/fade frames — confirmed via real
    library data to be literally how this happened) hash identically
    (distance 0), and is_blank() flags both so 02_fingerprint.py now
    skips storing either rather than recording a meaningless 'match'."""
    video_a_blank_frame = Image.new("RGB", (640, 360), (0, 0, 0))
    video_b_blank_frame = Image.new("RGB", (720, 480), (0, 0, 0))  # different size/source, same blankness
    assert ph.is_blank(video_a_blank_frame)
    assert ph.is_blank(video_b_blank_frame)
    assert ph.hamming_distance(ph.phash(video_a_blank_frame), ph.phash(video_b_blank_frame)) == 0
    print("test_blank_frames_from_two_unrelated_videos_would_have_collided: OK")


if __name__ == "__main__":
    test_solid_black_image_is_blank()
    test_solid_color_image_is_blank()
    test_textured_image_is_not_blank()
    test_blank_frames_from_two_unrelated_videos_would_have_collided()
    print("\nAll phash tests passed.")
