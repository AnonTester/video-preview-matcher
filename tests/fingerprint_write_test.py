"""
Tests 02_fingerprint.py's write_fingerprint_result() — the DB-write half
of a completed fingerprint_video() call, extracted out of the main
scheduling loop specifically so this is testable without a real
ProcessPoolExecutor or real ffmpeg. Covers a real bug: a re-fingerprint
(file replaced/re-encoded at the same path) producing *fewer* scenes than
the previous version used to leave old scene rows at higher indices
silently in place, since the old code inserted with no prior DELETE.
Loads 02_fingerprint.py directly via importlib (its filename starts with
a digit, so it can't be `import`ed normally). Run from project root:

    python3 tests/fingerprint_write_test.py
"""

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from db import connect, init_db  # noqa: E402

spec = importlib.util.spec_from_file_location("fp_write_mod", PROJECT_ROOT / "src" / "02_fingerprint.py")
fp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp_mod)

TMP_DB = Path("/tmp/fingerprint_write_test.db")


def reset():
    TMP_DB.unlink(missing_ok=True)
    init_db(TMP_DB)
    with connect(TMP_DB) as conn:
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (1, '/v/1.mp4', '1.mp4')")


def _scene(idx, ts):
    return {
        "scene_index": idx, "timestamp_sec": ts,
        "phash": f"{idx:016x}", "phash_cropped": f"{idx:016x}",
        "phash_flipped": f"{idx:016x}", "color_sig": "0" * 64,
    }


def test_writes_scenes_and_audio_and_sets_fingerprinted_at():
    reset()
    res = {
        "scenes": [_scene(0, 0.0), _scene(1, 5.0)],
        "audio": {"duration_sec": 10.0, "fingerprint": "1,2,3", "fp_ok": 1},
    }
    with connect(TMP_DB) as conn:
        fp_mod.write_fingerprint_result(conn, 1, res)

    with connect(TMP_DB) as conn:
        scenes = conn.execute("SELECT scene_index FROM scenes WHERE video_id = 1 ORDER BY scene_index").fetchall()
        audio = conn.execute("SELECT * FROM audio_fp WHERE video_id = 1").fetchone()
        video = conn.execute("SELECT fingerprinted_at FROM videos WHERE id = 1").fetchone()

    assert [r["scene_index"] for r in scenes] == [0, 1]
    assert audio["fingerprint"] == "1,2,3" and audio["fp_ok"] == 1
    assert video["fingerprinted_at"] is not None
    print("test_writes_scenes_and_audio_and_sets_fingerprinted_at: OK")


def test_refingerprint_with_fewer_scenes_does_not_leave_stale_rows():
    """The actual bug: re-fingerprinting a video that now has fewer
    scenes than its previous version used to leave old scene rows at the
    higher indices in place (INSERT OR REPLACE only touches matching
    (video_id, scene_index) pairs, never deletes extras), so a re-encoded
    file's matching would silently mix old and new content's hashes."""
    reset()
    first = {
        "scenes": [_scene(0, 0.0), _scene(1, 5.0), _scene(2, 10.0), _scene(3, 15.0)],
        "audio": {"duration_sec": 20.0, "fingerprint": "1,2,3", "fp_ok": 1},
    }
    with connect(TMP_DB) as conn:
        fp_mod.write_fingerprint_result(conn, 1, first)

    second = {
        "scenes": [_scene(0, 0.0)],
        "audio": {"duration_sec": 5.0, "fingerprint": "9,9,9", "fp_ok": 1},
    }
    with connect(TMP_DB) as conn:
        fp_mod.write_fingerprint_result(conn, 1, second)

    with connect(TMP_DB) as conn:
        scenes = conn.execute("SELECT scene_index FROM scenes WHERE video_id = 1 ORDER BY scene_index").fetchall()

    assert [r["scene_index"] for r in scenes] == [0], \
        f"stale scene rows from the previous fingerprint survived: {[r['scene_index'] for r in scenes]}"
    print("test_refingerprint_with_fewer_scenes_does_not_leave_stale_rows: OK")


def test_refingerprint_with_zero_scenes_clears_all_old_rows():
    """Even a content change that produces *no* detected scenes at all
    (e.g. replaced with a near-uniform video) must clear every old scene
    row, not just skip writing new ones."""
    reset()
    first = {
        "scenes": [_scene(0, 0.0), _scene(1, 5.0)],
        "audio": {"duration_sec": 10.0, "fingerprint": None, "fp_ok": 0},
    }
    with connect(TMP_DB) as conn:
        fp_mod.write_fingerprint_result(conn, 1, first)

    second = {"scenes": [], "audio": {"duration_sec": None, "fingerprint": None, "fp_ok": 0}}
    with connect(TMP_DB) as conn:
        fp_mod.write_fingerprint_result(conn, 1, second)

    with connect(TMP_DB) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM scenes WHERE video_id = 1").fetchone()["n"]
    assert n == 0, n
    print("test_refingerprint_with_zero_scenes_clears_all_old_rows: OK")


if __name__ == "__main__":
    test_writes_scenes_and_audio_and_sets_fingerprinted_at()
    test_refingerprint_with_fewer_scenes_does_not_leave_stale_rows()
    test_refingerprint_with_zero_scenes_clears_all_old_rows()
    TMP_DB.unlink(missing_ok=True)
    print("\nAll fingerprint-write tests passed.")
