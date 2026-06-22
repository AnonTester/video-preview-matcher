"""
Tests 03_match.py's preload-once + parallel-scoring rework: load_all_scenes/
load_all_audio (replacing a per-pair, per-side SELECT — see module
docstring's PERFORMANCE section for why that was the actual bottleneck
against a real library), the in-memory score_pair()/best_scene_match(), and
_chunk_pairs() (the work-splitting helper for --workers > 1). Doesn't spin
up a real ProcessPoolExecutor — that part is exercised live (see README's
Tuning section); this only covers the pure logic feeding it. Loads
03_match.py directly via importlib (its filename starts with a digit, so it
can't be `import`ed normally). Run from project root:

    python3 tests/match_scoring_test.py
"""

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from db import connect, init_db  # noqa: E402

spec = importlib.util.spec_from_file_location("match_scoring_mod", PROJECT_ROOT / "src" / "03_match.py")
match_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(match_mod)

TMP_DB = Path("/tmp/match_scoring_test.db")


def reset():
    TMP_DB.unlink(missing_ok=True)
    init_db(TMP_DB)


def test_load_all_scenes_parses_hex_to_int_and_groups_by_video():
    reset()
    with connect(TMP_DB) as conn:
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (1, '/a.mp4', 'a.mp4')")
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (2, '/b.mp4', 'b.mp4')")
        conn.execute(
            "INSERT INTO scenes (video_id, scene_index, timestamp_sec, phash, phash_cropped, phash_flipped, color_sig) "
            "VALUES (1, 0, 1.0, 'ff00', 'ff01', 'ff02', 'aa')"
        )
        conn.execute(
            "INSERT INTO scenes (video_id, scene_index, timestamp_sec, phash, phash_cropped, phash_flipped, color_sig) "
            "VALUES (2, 0, 2.0, '0000', '0001', '0002', 'bb')"
        )

    with connect(TMP_DB) as conn:
        by_video = match_mod.load_all_scenes(conn)

    assert set(by_video.keys()) == {1, 2}
    assert by_video[1][0]["phash"] == int("ff00", 16)
    assert by_video[1][0]["phash_cropped"] == int("ff01", 16)
    assert isinstance(by_video[1][0]["phash"], int)
    print("test_load_all_scenes_parses_hex_to_int_and_groups_by_video: OK")


def test_load_all_scenes_empty_table_returns_empty_dict():
    reset()
    with connect(TMP_DB) as conn:
        by_video = match_mod.load_all_scenes(conn)
    assert by_video == {}
    print("test_load_all_scenes_empty_table_returns_empty_dict: OK")


def test_load_all_audio_groups_by_video():
    reset()
    with connect(TMP_DB) as conn:
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (1, '/a.mp4', 'a.mp4')")
        conn.execute(
            "INSERT INTO audio_fp (video_id, duration_sec, fingerprint, fp_ok) VALUES (1, 10.0, '1,2,3', 1)"
        )
    with connect(TMP_DB) as conn:
        by_video = match_mod.load_all_audio(conn)
    assert by_video == {1: {"fingerprint": "1,2,3", "fp_ok": 1}}
    print("test_load_all_audio_groups_by_video: OK")


def _scene(ts, phash_hex, color_sig="00", duration_to_next=None):
    h = int(phash_hex, 16)
    return {"timestamp_sec": ts, "phash": h, "phash_cropped": h, "phash_flipped": h,
            "color_sig": color_sig, "duration_to_next": duration_to_next}


def _insert_scene(conn, video_id, scene_index, ts, phash_hex="ff00"):
    conn.execute(
        "INSERT INTO scenes (video_id, scene_index, timestamp_sec, phash, phash_cropped, phash_flipped, color_sig) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (video_id, scene_index, ts, phash_hex, phash_hex, phash_hex, "00"),
    )


def test_load_all_scenes_computes_duration_to_next_and_leaves_last_scene_none():
    reset()
    with connect(TMP_DB) as conn:
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (1, '/a.mp4', 'a.mp4')")
        _insert_scene(conn, 1, 0, 0.0)
        _insert_scene(conn, 1, 1, 2.0)
        _insert_scene(conn, 1, 2, 5.0)

    with connect(TMP_DB) as conn:
        by_video = match_mod.load_all_scenes(conn)  # min_scene_duration=0.0 default, nothing dropped

    durations = [s["duration_to_next"] for s in by_video[1]]
    assert durations == [2.0, 3.0, None]
    print("test_load_all_scenes_computes_duration_to_next_and_leaves_last_scene_none: OK")


def test_load_all_scenes_drops_scenes_shorter_than_min_duration():
    # Mimics video #2237's rapid-cut intro: three quick cuts (gaps of 1s)
    # followed by a genuine longer scene. Only the long scene plus the
    # undated last scene should survive a 2.0s floor.
    reset()
    with connect(TMP_DB) as conn:
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (1, '/a.mp4', 'a.mp4')")
        _insert_scene(conn, 1, 0, 0.0)
        _insert_scene(conn, 1, 1, 1.0)
        _insert_scene(conn, 1, 2, 2.0)
        _insert_scene(conn, 1, 3, 10.0)
        _insert_scene(conn, 1, 4, 20.0)

    with connect(TMP_DB) as conn:
        by_video = match_mod.load_all_scenes(conn, min_scene_duration=2.0)

    # ts=0.0 (duration 1.0) and ts=1.0 (duration 1.0) are dropped — their
    # own gap to the next cut is under the floor. ts=2.0 survives despite
    # being close to the dropped pair, since *its* forward gap (to 10.0)
    # is 8s — duration is about each scene's own span, not proximity to
    # neighbors.
    kept_ts = [s["timestamp_sec"] for s in by_video[1]]
    assert kept_ts == [2.0, 10.0, 20.0], kept_ts
    print("test_load_all_scenes_drops_scenes_shorter_than_min_duration: OK")


def test_best_scene_match_finds_identical_hash_at_zero_distance():
    preview_scene = _scene(1.0, "ff00ff00ff00ff00")
    candidate_scenes = [_scene(5.0, "ff00ff00ff00ff00")]
    m = match_mod.best_scene_match(preview_scene, candidate_scenes, hash_threshold=8, color_threshold=0.25)
    assert m is not None
    assert m["distance"] == 0
    assert m["candidate_ts"] == 5.0
    print("test_best_scene_match_finds_identical_hash_at_zero_distance: OK")


def test_best_scene_match_rejects_over_threshold():
    # 0xffff... vs 0x0000... is maximally different (all bits flipped)
    preview_scene = _scene(1.0, "ffffffffffffffff")
    candidate_scenes = [_scene(5.0, "0000000000000000")]
    m = match_mod.best_scene_match(preview_scene, candidate_scenes, hash_threshold=8, color_threshold=0.25)
    assert m is None
    print("test_best_scene_match_rejects_over_threshold: OK")


def test_best_scene_match_carries_scene_durations_through():
    preview_scene = _scene(1.0, "ff00ff00ff00ff00", duration_to_next=4.0)
    candidate_scenes = [_scene(5.0, "ff00ff00ff00ff00", duration_to_next=6.0)]
    m = match_mod.best_scene_match(preview_scene, candidate_scenes, hash_threshold=8, color_threshold=0.25)
    assert m["preview_scene_duration"] == 4.0
    assert m["candidate_scene_duration"] == 6.0
    print("test_best_scene_match_carries_scene_durations_through: OK")


def test_score_pair_missing_scenes_returns_none():
    res = match_mod.score_pair({}, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert res is None
    print("test_score_pair_missing_scenes_returns_none: OK")


def test_score_pair_perfect_visual_match_no_audio():
    scenes_by_video = {
        1: [_scene(1.0, "ff00ff00ff00ff00"), _scene(2.0, "00ff00ff00ff00ff"), _scene(3.0, "ffff0000ffff0000")],
        2: [_scene(10.0, "ff00ff00ff00ff00"), _scene(20.0, "00ff00ff00ff00ff"), _scene(30.0, "ffff0000ffff0000")],
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert res is not None
    assert res["visual_score"] == 1.0
    assert res["audio_score"] is None
    assert res["combined_score"] == 1.0  # no audio -> combined == visual, no penalty
    assert len(res["scene_matches"]) == 3
    assert res["match_spread_sec"] == 2.0  # preview_ts 1.0..3.0
    print("test_score_pair_perfect_visual_match_no_audio: OK")


def test_score_pair_single_match_has_zero_spread():
    scenes_by_video = {
        1: [_scene(1.0, "ff00ff00ff00ff00")],
        2: [_scene(10.0, "ff00ff00ff00ff00")],
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert res["match_spread_sec"] == 0.0
    print("test_score_pair_single_match_has_zero_spread: OK")


def test_score_pair_clustered_matches_have_small_spread():
    # video #2237 in miniature: three preview scenes within ~2s of each
    # other (a rapid-cut intro) all matching — weak corroboration even
    # though the raw matched-scene count clears --min-matched-scenes.
    scenes_by_video = {
        1: [_scene(0.2, "ff00ff00ff00ff00"), _scene(1.0, "00ff00ff00ff00ff"), _scene(1.8, "ffff0000ffff0000")],
        2: [_scene(0.1, "ff00ff00ff00ff00"), _scene(0.9, "00ff00ff00ff00ff"), _scene(1.7, "ffff0000ffff0000")],
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert len(res["scene_matches"]) == 3
    assert res["match_spread_sec"] == 1.6  # 1.8 - 0.2
    print("test_score_pair_clustered_matches_have_small_spread: OK")


def test_chunk_pairs_empty_list():
    assert match_mod._chunk_pairs([], workers=4) == []
    print("test_chunk_pairs_empty_list: OK")


def test_chunk_pairs_covers_every_pair_exactly_once_in_order():
    pairs = [(i, i + 1) for i in range(0, 2000, 2)]
    for workers in (1, 2, 8, 64):
        chunks = match_mod._chunk_pairs(pairs, workers)
        flattened = [p for chunk in chunks for p in chunk]
        assert flattened == pairs, f"workers={workers} lost or reordered pairs"
        assert all(len(c) > 0 for c in chunks), f"workers={workers} produced an empty chunk"
    print("test_chunk_pairs_covers_every_pair_exactly_once_in_order: OK")


def test_chunk_pairs_produces_more_chunks_than_workers_for_load_balancing():
    pairs = [(i, i + 1) for i in range(10000)]
    chunks = match_mod._chunk_pairs(pairs, workers=4)
    # target_chunks = max(4*4, 40) = 40
    assert len(chunks) >= 40, f"expected >=40 chunks for load balancing, got {len(chunks)}"
    print("test_chunk_pairs_produces_more_chunks_than_workers_for_load_balancing: OK")


if __name__ == "__main__":
    test_load_all_scenes_parses_hex_to_int_and_groups_by_video()
    test_load_all_scenes_empty_table_returns_empty_dict()
    test_load_all_scenes_computes_duration_to_next_and_leaves_last_scene_none()
    test_load_all_scenes_drops_scenes_shorter_than_min_duration()
    test_load_all_audio_groups_by_video()
    test_best_scene_match_finds_identical_hash_at_zero_distance()
    test_best_scene_match_rejects_over_threshold()
    test_best_scene_match_carries_scene_durations_through()
    test_score_pair_missing_scenes_returns_none()
    test_score_pair_perfect_visual_match_no_audio()
    test_score_pair_single_match_has_zero_spread()
    test_score_pair_clustered_matches_have_small_spread()
    test_chunk_pairs_empty_list()
    test_chunk_pairs_covers_every_pair_exactly_once_in_order()
    test_chunk_pairs_produces_more_chunks_than_workers_for_load_balancing()
    TMP_DB.unlink(missing_ok=True)
    print("\nAll match-scoring tests passed.")
