"""
Tests 03_match.py's preload-once + parallel-scoring rework: load_all_scenes/
load_all_audio (replacing a per-pair, per-side SELECT — see module
docstring's PERFORMANCE section for why that was the actual bottleneck
against a real library), the in-memory score_pair()/score_scenes() (the
vectorized, numpy-backed scoring path — see module docstring's VECTORIZED
SCORING section), and _chunk_pairs() (the work-splitting helper for
--workers > 1). Doesn't spin up a real ProcessPoolExecutor — that part is
exercised live (see README's Tuning section); this only covers the pure
logic feeding it. Loads 03_match.py directly via importlib (its filename
starts with a digit, so it can't be `import`ed normally). Run from
project root:

    python3 tests/match_scoring_test.py
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np

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
    assert by_video[1].phash[0, 0] == int("ff00", 16)
    assert by_video[1].phash[0, 1] == int("ff01", 16)
    assert by_video[1].phash.dtype == np.uint64
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


def _video_scenes(entries):
    """Build a VideoScenes directly (bypassing load_all_scenes/a real DB)
    from a list of (ts, phash_hex, duration_to_next, color_sig_hex)
    tuples — the last two are optional (default None). phash_hex is used
    for all 3 variants (normal/cropped/flipped); tests that need them to
    differ build the arrays by hand instead. Mirrors exactly what
    load_all_scenes() would produce for the same data, so score_scenes()/
    score_pair() tests can construct precise scenarios without a DB."""
    n = len(entries)
    ts = np.array([e[0] for e in entries], dtype=np.float64)
    dur = np.array([np.nan if len(e) < 3 or e[2] is None else e[2] for e in entries], dtype=np.float64)
    phash = np.array([[int(e[1], 16)] * 3 for e in entries], dtype=np.uint64)
    sigs = [e[3] if len(e) > 3 else None for e in entries]
    sig_len = max((len(s) for s in sigs if s), default=0)
    color_sig = np.zeros((n, sig_len), dtype=np.uint8)
    has_color_sig = np.zeros(n, dtype=bool)
    for i, s in enumerate(sigs):
        if s:
            has_color_sig[i] = True
            color_sig[i] = [int(ch, 16) for ch in s]
    return match_mod.VideoScenes(timestamp_sec=ts, duration_to_next=dur, phash=phash,
                                  color_sig=color_sig, has_color_sig=has_color_sig)


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

    durations = by_video[1].duration_to_next
    assert list(durations[:2]) == [2.0, 3.0]
    assert np.isnan(durations[2])
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
    kept_ts = list(by_video[1].timestamp_sec)
    assert kept_ts == [2.0, 10.0, 20.0], kept_ts
    print("test_load_all_scenes_drops_scenes_shorter_than_min_duration: OK")


def test_score_scenes_finds_identical_hash_at_zero_distance():
    preview = _video_scenes([(1.0, "ff00ff00ff00ff00", None)])
    candidate = _video_scenes([(5.0, "ff00ff00ff00ff00", None)])
    matched = match_mod.score_scenes(preview, candidate, hash_threshold=8, color_threshold=0.25)
    assert len(matched) == 1
    assert matched[0]["distance"] == 0
    assert matched[0]["candidate_ts"] == 5.0
    print("test_score_scenes_finds_identical_hash_at_zero_distance: OK")


def test_score_scenes_rejects_over_threshold():
    # 0xffff... vs 0x0000... is maximally different (all bits flipped)
    preview = _video_scenes([(1.0, "ffffffffffffffff", None)])
    candidate = _video_scenes([(5.0, "0000000000000000", None)])
    matched = match_mod.score_scenes(preview, candidate, hash_threshold=8, color_threshold=0.25)
    assert matched == []
    print("test_score_scenes_rejects_over_threshold: OK")


def test_score_scenes_carries_scene_durations_through():
    preview = _video_scenes([(1.0, "ff00ff00ff00ff00", 4.0)])
    candidate = _video_scenes([(5.0, "ff00ff00ff00ff00", 6.0)])
    matched = match_mod.score_scenes(preview, candidate, hash_threshold=8, color_threshold=0.25)
    assert matched[0]["preview_scene_duration"] == 4.0
    assert matched[0]["candidate_scene_duration"] == 6.0
    print("test_score_scenes_carries_scene_durations_through: OK")


def test_score_scenes_empty_side_returns_empty_list():
    preview = _video_scenes([(1.0, "ff00ff00ff00ff00", None)])
    empty = _video_scenes([])
    assert match_mod.score_scenes(preview, empty, hash_threshold=8, color_threshold=0.25) == []
    assert match_mod.score_scenes(empty, preview, hash_threshold=8, color_threshold=0.25) == []
    print("test_score_scenes_empty_side_returns_empty_list: OK")


def test_score_scenes_color_guard_rejects_distant_color_even_at_zero_hash_distance():
    # Identical pHash but wildly different color signatures should still
    # be rejected by the color-collision guard (phash.py's color_sig
    # docstring: pHash alone is color-blind).
    preview = _video_scenes([(1.0, "ff00ff00ff00ff00", None, "f" * 64)])
    candidate = _video_scenes([(5.0, "ff00ff00ff00ff00", None, "0" * 64)])
    matched = match_mod.score_scenes(preview, candidate, hash_threshold=8, color_threshold=0.25)
    assert matched == []
    print("test_score_scenes_color_guard_rejects_distant_color_even_at_zero_hash_distance: OK")


def test_score_pair_missing_scenes_returns_none():
    res = match_mod.score_pair({}, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert res is None
    print("test_score_pair_missing_scenes_returns_none: OK")


def test_score_pair_perfect_visual_match_no_audio():
    scenes_by_video = {
        1: _video_scenes([(1.0, "ff00ff00ff00ff00", None), (2.0, "00ff00ff00ff00ff", None), (3.0, "ffff0000ffff0000", None)]),
        2: _video_scenes([(10.0, "ff00ff00ff00ff00", None), (20.0, "00ff00ff00ff00ff", None), (30.0, "ffff0000ffff0000", None)]),
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert res is not None
    assert res["visual_score"] == 1.0
    assert res["audio_score"] is None
    assert res["combined_score"] == 1.0  # no audio -> combined == visual, no penalty
    assert len(res["scene_matches"]) == 3
    assert res["match_spread_sec"] == 2.0  # preview_ts 1.0..3.0
    assert res["distinct_candidate_match_count"] == 3
    print("test_score_pair_perfect_visual_match_no_audio: OK")


def test_score_pair_single_match_has_zero_spread():
    scenes_by_video = {
        1: _video_scenes([(1.0, "ff00ff00ff00ff00", None)]),
        2: _video_scenes([(10.0, "ff00ff00ff00ff00", None)]),
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert res["match_spread_sec"] == 0.0
    print("test_score_pair_single_match_has_zero_spread: OK")


def test_score_pair_clustered_matches_have_small_spread():
    # video #2237 in miniature: three preview scenes within ~2s of each
    # other (a rapid-cut intro) all matching — weak corroboration even
    # though the raw matched-scene count clears --min-matched-scenes.
    scenes_by_video = {
        1: _video_scenes([(0.2, "ff00ff00ff00ff00", None), (1.0, "00ff00ff00ff00ff", None), (1.8, "ffff0000ffff0000", None)]),
        2: _video_scenes([(0.1, "ff00ff00ff00ff00", None), (0.9, "00ff00ff00ff00ff", None), (1.7, "ffff0000ffff0000", None)]),
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert len(res["scene_matches"]) == 3
    assert res["match_spread_sec"] == 1.6  # 1.8 - 0.2
    assert res["distinct_candidate_match_count"] == 3
    print("test_score_pair_clustered_matches_have_small_spread: OK")


def test_score_pair_repeated_intro_has_zero_candidate_spread():
    # video #4059: a ~2.8s intro appears three times in the preview, well
    # spread out (a real reused intro, not a rapid-cut sting — clears
    # --min-match-spread on the preview side), but the candidate only has
    # it once, so all three matches collapse onto the same single
    # candidate timestamp. Preview-side spread alone can't catch this —
    # it's exactly what candidate_match_spread_sec exists to catch.
    scenes_by_video = {
        1: _video_scenes([
            (1.0, "ff00ff00ff00ff00", None),
            (50.0, "ff00ff00ff00ff00", None),
            (100.0, "ff00ff00ff00ff00", None),
        ]),
        2: _video_scenes([(5.0, "ff00ff00ff00ff00", None)]),
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert len(res["scene_matches"]) == 3
    assert res["match_spread_sec"] == 99.0, res["match_spread_sec"]
    assert res["candidate_match_spread_sec"] == 0.0, res["candidate_match_spread_sec"]
    assert res["distinct_candidate_match_count"] == 1
    print("test_score_pair_repeated_intro_has_zero_candidate_spread: OK")


def test_score_pair_independent_matches_have_spread_on_both_sides():
    # The legitimate counterpart to the above: three genuinely distinct
    # preview scenes matching three genuinely distinct candidate scenes
    # should show real spread on both sides, not get penalized by the
    # new check.
    scenes_by_video = {
        1: _video_scenes([
            (1.0, "ff00ff00ff00ff00", None),
            (50.0, "00ff00ff00ff00ff", None),
            (100.0, "ffff0000ffff0000", None),
        ]),
        2: _video_scenes([
            (10.0, "ff00ff00ff00ff00", None),
            (200.0, "00ff00ff00ff00ff", None),
            (400.0, "ffff0000ffff0000", None),
        ]),
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert res["match_spread_sec"] == 99.0, res["match_spread_sec"]
    assert res["candidate_match_spread_sec"] == 390.0, res["candidate_match_spread_sec"]
    assert res["distinct_candidate_match_count"] == 3
    print("test_score_pair_independent_matches_have_spread_on_both_sides: OK")


def test_score_pair_distinct_candidate_match_count_collapses_duplicates():
    # video #936: 6 preview scenes (a repeated camera-flash frame) all
    # best-match the *identical* candidate scene, plus one unrelated,
    # genuinely coincidental match elsewhere. That 7th match alone gives
    # candidate_match_spread_sec a huge value (hundreds of seconds) even
    # though 6 of the 7 "matches" are really one match counted six
    # times — distinct_candidate_match_count must see through that and
    # report 2, not 7, so --min-matched-scenes (checked against this,
    # not the raw count) correctly rejects a pair with only 2 real
    # pieces of evidence.
    scenes_by_video = {
        1: _video_scenes([
            (16.4, "ff00ff00ff00ff00", None),
            (35.2, "ff00ff00ff00ff00", None),
            (40.6, "ff00ff00ff00ff00", None),
            (73.0, "ff00ff00ff00ff00", None),
            (78.9, "ff00ff00ff00ff00", None),
            (138.3, "ff00ff00ff00ff00", None),
            (533.1, "00ff00ff00ff00ff", None),
        ]),
        2: _video_scenes([
            (75.68, "ff00ff00ff00ff00", None),
            (717.6, "00ff00ff00ff00ff", None),
        ]),
    }
    res = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    assert len(res["scene_matches"]) == 7
    assert res["candidate_match_spread_sec"] == 717.6 - 75.68, res["candidate_match_spread_sec"]
    assert res["distinct_candidate_match_count"] == 2, res["distinct_candidate_match_count"]
    print("test_score_pair_distinct_candidate_match_count_collapses_duplicates: OK")


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


def test_chunk_pairs_with_cost_fn_covers_every_pair_exactly_once():
    pairs = [(i, i + 1) for i in range(0, 2000, 2)]
    chunks = match_mod._chunk_pairs(pairs, workers=8, cost_fn=lambda p, c: (p % 7) + 1)
    flattened = [pc for chunk in chunks for pc in chunk]
    assert sorted(flattened) == sorted(pairs), "cost-aware chunking lost or duplicated pairs"
    assert all(len(c) > 0 for c in chunks), "cost-aware chunking produced an empty chunk"
    print("test_chunk_pairs_with_cost_fn_covers_every_pair_exactly_once: OK")


def test_chunk_pairs_with_cost_fn_balances_better_than_equal_count_slicing():
    # Mirrors the real straggler scenario found live on homeserver: one
    # preview (id=1) has many scenes and appears in a contiguous run of
    # 20 expensive pairs, surrounded by 200 cheap ones — exactly what
    # main()'s preview-outer pair-building loop produces for a single
    # high-scene-count preview. Plain equal-count slicing can concentrate
    # that whole run into a couple of chunks (stragglers); cost-aware
    # bin-packing should spread it so no chunk needs much more than one
    # expensive pair's worth.
    cheap_pairs = [(100 + i, 200 + i) for i in range(200)]
    expensive_pairs = [(1, 300 + i) for i in range(20)]
    pairs = expensive_pairs + cheap_pairs

    def cost_fn(p, c):
        return 1000 if p == 1 else 1

    naive_chunks = match_mod._chunk_pairs(pairs, workers=4)  # no cost_fn -> old equal-count behavior
    naive_max = max(sum(cost_fn(p, c) for p, c in chunk) for chunk in naive_chunks)

    balanced_chunks = match_mod._chunk_pairs(pairs, workers=4, cost_fn=cost_fn)
    balanced_max = max(sum(cost_fn(p, c) for p, c in chunk) for chunk in balanced_chunks)

    assert balanced_max <= 1000, f"expected no chunk above ~1 expensive pair's cost, got {balanced_max}"
    assert balanced_max < naive_max, f"balanced max chunk cost {balanced_max} should beat naive {naive_max}"
    print("test_chunk_pairs_with_cost_fn_balances_better_than_equal_count_slicing: OK")


def test_chunk_pairs_with_cost_fn_reuses_pair_tuples_no_per_pair_allocation():
    # Regression guard for a real OOM/server-crash incident: an earlier
    # version of the cost-aware path built a new (cost, p, c) tuple per
    # pair via sorted(...) before bin-packing — ~2.5-3GB of purely
    # transient allocation at full-library scale (~11.7M pairs), which
    # contributed to crashing the production server. The shipped version
    # must place the *same* tuple objects from the input into chunks, not
    # rebuilt copies.
    pairs = [(i, i + 1) for i in range(500)]
    chunks = match_mod._chunk_pairs(pairs, workers=4, cost_fn=lambda p, c: 1.0)
    input_ids = {id(pc) for pc in pairs}
    for chunk in chunks:
        for pc in chunk:
            assert id(pc) in input_ids, "cost-aware chunking allocated a new pair tuple instead of reusing the input's"
    print("test_chunk_pairs_with_cost_fn_reuses_pair_tuples_no_per_pair_allocation: OK")


def test_chunk_pairs_progress_interval_increases_chunk_count_for_large_pair_counts():
    # The worker-based floor (workers*4, floored at 40) doesn't scale
    # with pair count at all, which is exactly why a real full-library
    # run reports progress only every few minutes (each chunk grows with
    # the pair count) while a small test run looks fine. A tight
    # progress_interval_sec against a large pair count should force well
    # past that floor.
    pairs = [(i, i + 1) for i in range(100_000)]
    default_chunks = match_mod._chunk_pairs(pairs, workers=4)
    tight_chunks = match_mod._chunk_pairs(pairs, workers=4, progress_interval_sec=1.0)
    assert len(default_chunks) == 40, f"expected the unchanged 40-chunk floor, got {len(default_chunks)}"
    assert len(tight_chunks) > len(default_chunks), \
        f"tight progress_interval_sec should raise chunk count above the floor, got {len(tight_chunks)}"
    print("test_chunk_pairs_progress_interval_increases_chunk_count_for_large_pair_counts: OK")


def test_trim_worker_memory_does_not_raise():
    # Smoke test only — malloc_trim() has no observable Python-level
    # return value to assert on; this just confirms the ctypes call (or
    # its graceful no-op on a non-glibc platform) doesn't raise, since
    # this runs unconditionally at the end of every _score_chunk() call
    # in a real worker process.
    match_mod._trim_worker_memory()
    print("test_trim_worker_memory_does_not_raise: OK")


def test_score_chunk_result_unaffected_by_memory_trim():
    # _score_chunk() is what actually runs in a worker process — confirm
    # its returned data is the same regardless of --trim-worker-memory
    # (now opt-in, off by default — see module docstring's MEMORY GROWTH
    # section for why it's disabled: measured live to add ~27% runtime
    # with zero effect on actual memory growth).
    scenes_by_video = {
        1: _video_scenes([(1.0, "ff00ff00ff00ff00", None), (2.0, "00ff00ff00ff00ff", None), (3.0, "ffff0000ffff0000", None)]),
        2: _video_scenes([(10.0, "ff00ff00ff00ff00", None), (20.0, "00ff00ff00ff00ff", None), (30.0, "ffff0000ffff0000", None)]),
    }
    direct = match_mod.score_pair(scenes_by_video, {}, preview_id=1, candidate_id=2, hash_threshold=8, color_threshold=0.25)
    for trim_memory in (False, True):
        match_mod._worker_state().update({
            "scenes": scenes_by_video, "audio": {}, "hash_threshold": 8, "color_threshold": 0.25,
            "trim_memory": trim_memory,
        })
        chunk_result = match_mod._score_chunk([(1, 2)])
        assert chunk_result == [(1, 2, direct)], f"trim_memory={trim_memory} changed the result"
    print("test_score_chunk_result_unaffected_by_memory_trim: OK")


def test_record_candidate_keeps_only_top_n_per_preview():
    top_candidates = {}
    for i in range(20):
        res = {"combined_score": i / 20.0}  # candidate i=19 is the best
        match_mod._record_candidate(top_candidates, preview_id=1, candidate_id=i, res=res, top_n=5)
    heap = top_candidates[1]
    assert len(heap) == 5, f"expected exactly top_n=5 entries, got {len(heap)}"
    kept_ids = sorted(candidate_id for _, candidate_id, _ in heap)
    assert kept_ids == [15, 16, 17, 18, 19], f"expected the 5 best-scoring candidates, got {kept_ids}"
    print("test_record_candidate_keeps_only_top_n_per_preview: OK")


def test_record_candidate_ties_at_eviction_boundary_keep_earlier_arrival():
    # Matches the old stable-sort-then-slice behavior in the sequential
    # path: on an exact score tie at the cutoff, whichever arrived first
    # is kept.
    top_candidates = {}
    match_mod._record_candidate(top_candidates, 1, candidate_id=100, res={"combined_score": 0.5}, top_n=1)
    match_mod._record_candidate(top_candidates, 1, candidate_id=200, res={"combined_score": 0.5}, top_n=1)
    heap = top_candidates[1]
    assert len(heap) == 1
    assert heap[0][1] == 100, f"expected the earlier-arriving candidate (100) to win the tie, got {heap[0][1]}"
    print("test_record_candidate_ties_at_eviction_boundary_keep_earlier_arrival: OK")


def test_record_candidate_separate_previews_dont_interfere():
    top_candidates = {}
    match_mod._record_candidate(top_candidates, 1, candidate_id=10, res={"combined_score": 0.9}, top_n=2)
    match_mod._record_candidate(top_candidates, 2, candidate_id=20, res={"combined_score": 0.1}, top_n=2)
    assert {pid: len(h) for pid, h in top_candidates.items()} == {1: 1, 2: 1}
    print("test_record_candidate_separate_previews_dont_interfere: OK")


def test_chunk_pairs_progress_interval_never_shrinks_below_worker_floor():
    # A loose progress_interval_sec (here, 1 hour) against a small pair
    # count would suggest just 1 chunk on its own — it must not pull
    # the result below the existing workers*4/40 floor.
    pairs = [(i, i + 1) for i in range(50)]
    default_chunks = match_mod._chunk_pairs(pairs, workers=4)
    loose_chunks = match_mod._chunk_pairs(pairs, workers=4, progress_interval_sec=3600.0)
    assert len(loose_chunks) == len(default_chunks), \
        f"a loose progress_interval_sec shouldn't change anything vs. the default floor: {len(loose_chunks)} != {len(default_chunks)}"
    print("test_chunk_pairs_progress_interval_never_shrinks_below_worker_floor: OK")


if __name__ == "__main__":
    test_load_all_scenes_parses_hex_to_int_and_groups_by_video()
    test_load_all_scenes_empty_table_returns_empty_dict()
    test_load_all_scenes_computes_duration_to_next_and_leaves_last_scene_none()
    test_load_all_scenes_drops_scenes_shorter_than_min_duration()
    test_load_all_audio_groups_by_video()
    test_score_scenes_finds_identical_hash_at_zero_distance()
    test_score_scenes_rejects_over_threshold()
    test_score_scenes_carries_scene_durations_through()
    test_score_scenes_empty_side_returns_empty_list()
    test_score_scenes_color_guard_rejects_distant_color_even_at_zero_hash_distance()
    test_score_pair_missing_scenes_returns_none()
    test_score_pair_perfect_visual_match_no_audio()
    test_score_pair_single_match_has_zero_spread()
    test_score_pair_clustered_matches_have_small_spread()
    test_score_pair_repeated_intro_has_zero_candidate_spread()
    test_score_pair_independent_matches_have_spread_on_both_sides()
    test_score_pair_distinct_candidate_match_count_collapses_duplicates()
    test_trim_worker_memory_does_not_raise()
    test_score_chunk_result_unaffected_by_memory_trim()
    test_record_candidate_keeps_only_top_n_per_preview()
    test_record_candidate_ties_at_eviction_boundary_keep_earlier_arrival()
    test_record_candidate_separate_previews_dont_interfere()
    test_chunk_pairs_empty_list()
    test_chunk_pairs_covers_every_pair_exactly_once_in_order()
    test_chunk_pairs_produces_more_chunks_than_workers_for_load_balancing()
    test_chunk_pairs_with_cost_fn_covers_every_pair_exactly_once()
    test_chunk_pairs_with_cost_fn_balances_better_than_equal_count_slicing()
    test_chunk_pairs_with_cost_fn_reuses_pair_tuples_no_per_pair_allocation()
    test_chunk_pairs_progress_interval_increases_chunk_count_for_large_pair_counts()
    test_chunk_pairs_progress_interval_never_shrinks_below_worker_floor()
    TMP_DB.unlink(missing_ok=True)
    print("\nAll match-scoring tests passed.")
