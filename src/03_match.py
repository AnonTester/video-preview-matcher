"""
03_match.py — Score every preview-candidate video pair and store ranked matches.

PREFILTER:
    Before any hash comparison, candidates are restricted by duration: a
    preview can't be a subset of something shorter than itself, and in
    practice a preview is a small fraction of its source's length. The
    --min-ratio/--max-ratio bounds (preview_duration / candidate_duration)
    cut the O(n*m) comparison matrix down before the expensive part runs.

VISUAL SCORE:
    For each scene-hash in the preview, find the best (lowest Hamming
    distance) match anywhere in the candidate's scene timeline, checking
    the preview frame's normal/cropped/flipped hash against the
    candidate's normal/cropped/flipped hash (9 combinations), so any one
    crop+flip transformation in either direction is caught. A match
    "counts" if the best distance is under --hash-threshold AND, as a
    guard against the pHash color-blind-spot, the color-signature
    distance between the matched pair is under --color-threshold (see
    phash.py color_signature docstring for why this guard exists).

    visual_score = (# preview scenes with a counted match) / (# preview scenes)

MINIMUM MATCHED-SCENE COUNT (--min-matched-scenes, default 3):
    A real preview splices together *several* moments from its source —
    a single matching scene is weak, easily-coincidental evidence, not
    confirmation. This bit the project for real: three stored matches
    against the real library were each a single isolated scene
    match, at exactly --hash-threshold's boundary distance, on previews
    with only 2-6 total scenes — meaning one lucky coincidental
    hit on a near-featureless frame (skin tone, blur — exactly the kind
    of low-texture content this library is full of) was enough to clear
    a deceptively high *fraction* (1/2 = 50%) even though it's a single
    data point. --min-visual-score (the fraction) and --min-matched-scenes
    (the absolute count) are both required, specifically so a tiny preview
    can't pass on fraction alone. Tightened defaults after that incident:
    --hash-threshold 12 -> 8 (all three false positives sat exactly at the
    old boundary) and --min-matched-scenes added at 3.

AUDIO SCORE:
    Chromaprint fingerprints are compared only when both sides have one
    (fp_ok=1). A previewer with narration dubbed over the original audio
    will legitimately have no usable audio match — that's expected, not
    a failure, so audio_score is NULLABLE and excluded from the combined
    score (re-normalized) when unavailable, rather than penalizing the
    pair for missing data it was never going to have.

COMBINED SCORE:
    If audio available:  0.65 * visual + 0.35 * audio
    If audio unavailable: visual_score directly (no audio penalty/bonus)
    Visual is weighted higher throughout because it's the only signal
    guaranteed to survive narration overdubs, which you flagged as a
    realistic case in this library.

DISMISSED PAIRS ("not a match" feedback):
    The review UI lets a human flag one specific (preview, candidate)
    pairing as a false positive without deciding the preview as a whole
    (see `match_feedback` in db.py). Since this stage deletes and fully
    recomputes `matches` on every run, those dismissals are loaded
    up-front (`load_dismissed_pairs`) and the pair is skipped entirely —
    otherwise a dismissed false positive would resurrect the moment
    thresholds change and you re-run this stage.

PERFORMANCE (preload + parallelize — found live, against a real 2000-video
library, 1,851,501 pairs after the duration prefilter, taking ~45 minutes
on a single core):
    The dominant cost was NOT hashing, NOT missing indices, and NOT SQLite
    page-cache misses — `scenes` has `idx_scenes_video` already, and the
    whole DB is ~30MB, trivially OS-page-cached after the first read. The
    actual cost was query *count*: the old code called `load_scenes()`
    (a fresh `SELECT ... WHERE video_id = ?`) twice per pair, every pair,
    for a library where the same ~2000 videos each appear in *thousands*
    of pairs — millions of redundant round-trips re-fetching identical
    rows. Fixed by `load_all_scenes`/`load_all_audio`: one full-table
    SELECT each, up front, into in-memory dicts keyed by video_id. Every
    pair's scoring after that is pure in-memory comparison, zero DB
    access. Hashes are also parsed hex->int once at load time (`int(h, 16)`
    on every Hamming-distance call, across millions of comparisons, was
    real overhead) and `best_scene_match` uses `int.bit_count()` instead
    of `bin(x).count("1")` for the XOR popcount.

    With that fixed, scoring itself is pure CPU work with no shared state
    needed beyond the read-only preloaded dicts, so it parallelizes
    cleanly: `--workers` > 1 preloads once in the main process, then
    forks a `ProcessPoolExecutor` whose workers each receive a copy of
    the preloaded dicts *once* (via the pool initializer, not per-task)
    and score *chunks* of pairs (not one task per pair — submitting 1.85M
    individual single-pair tasks would drown in IPC/scheduling overhead
    before the actual CPU work even started). All DB writes still happen
    only in the main process after every worker's results come back, same
    as before — workers never touch the database.

    There's no I/O or shared resource contention in this stage (no
    ffmpeg, no NFS, no GPU) unlike 02_fingerprint.py, so unlike that
    stage's worker-count tuning, more cores should mostly just help here.
    Exactly how much, and whether leaving a core free (the default) vs.
    using every core matters in practice, hasn't been benchmarked for
    real on production hardware yet — do that before assuming the
    default is optimal; see README's "Tuning" section.

Usage:
    python3 src/03_match.py --db data/library.db [--hash-threshold 8]
                             [--color-threshold 0.25] [--min-ratio 0.02]
                             [--max-ratio 0.95] [--top-n 5]
                             [--min-visual-score 0.15] [--min-matched-scenes 3]
                             [--workers N]
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import connect, init_db, update_scan_run
import phash as ph


def load_dismissed_pairs(conn) -> set[tuple[int, int]]:
    """(preview_id, candidate_id) pairs a human has explicitly flagged as
    not a match — see module docstring's DISMISSED PAIRS section."""
    rows = conn.execute(
        "SELECT preview_id, candidate_id FROM match_feedback WHERE status = 'not_a_match'"
    ).fetchall()
    return {(r["preview_id"], r["candidate_id"]) for r in rows}


def load_all_scenes(conn) -> dict[int, list[dict]]:
    """Every video's scenes, preloaded once — see module docstring's
    PERFORMANCE section for why this replaced a per-pair, per-side
    `SELECT`. Hashes are parsed from hex to int here, once, so the
    comparison loop never re-parses the same string across the many pairs
    that share a video."""
    rows = conn.execute(
        "SELECT video_id, scene_index, timestamp_sec, phash, phash_cropped, phash_flipped, color_sig "
        "FROM scenes ORDER BY video_id, scene_index"
    ).fetchall()
    by_video: dict[int, list[dict]] = {}
    for r in rows:
        by_video.setdefault(r["video_id"], []).append({
            "timestamp_sec": r["timestamp_sec"],
            "phash": int(r["phash"], 16),
            "phash_cropped": int(r["phash_cropped"], 16),
            "phash_flipped": int(r["phash_flipped"], 16),
            "color_sig": r["color_sig"],
        })
    return by_video


def load_all_audio(conn) -> dict[int, dict]:
    """Every video's audio fingerprint, preloaded once (same reasoning as
    load_all_scenes)."""
    rows = conn.execute("SELECT video_id, fingerprint, fp_ok FROM audio_fp").fetchall()
    return {r["video_id"]: {"fingerprint": r["fingerprint"], "fp_ok": r["fp_ok"]} for r in rows}


def best_scene_match(preview_scene, candidate_scenes, hash_threshold, color_threshold):
    """
    Compare one preview scene against every candidate scene across all
    crop/flip variant combinations. Returns (best_distance, candidate_ts,
    variant_label) or None if nothing clears the threshold. Hashes are
    already ints (see load_all_scenes) — XOR + int.bit_count() is the
    popcount, faster than bin(x).count("1") and avoids re-parsing hex.
    """
    p_variants = (
        ("normal", preview_scene["phash"]),
        ("cropped", preview_scene["phash_cropped"]),
        ("flipped", preview_scene["phash_flipped"]),
    )
    best = None  # (distance, candidate_ts, "p_variant-vs-c_variant", c_color_sig)

    for c_scene in candidate_scenes:
        c_variants = (
            ("normal", c_scene["phash"]),
            ("cropped", c_scene["phash_cropped"]),
            ("flipped", c_scene["phash_flipped"]),
        )
        for p_label, p_hash in p_variants:
            for c_label, c_hash in c_variants:
                dist = (p_hash ^ c_hash).bit_count()
                if best is None or dist < best[0]:
                    best = (dist, c_scene["timestamp_sec"], f"{p_label}-vs-{c_label}", c_scene["color_sig"])

    if best is None:
        return None

    dist, c_ts, variant, c_color_sig = best
    if dist > hash_threshold:
        return None

    # Color-collision guard: only applied against the "normal" comparison
    # color signature, since crop changes histogram proportions by design.
    c_dist = ph.color_distance(preview_scene["color_sig"], c_color_sig) if preview_scene.get("color_sig") and c_color_sig else 0.0
    if c_dist > color_threshold:
        return None

    return {"distance": dist, "candidate_ts": c_ts, "variant": variant, "preview_ts": preview_scene["timestamp_sec"]}


def score_pair(scenes_by_video, audio_by_video, preview_id, candidate_id, hash_threshold, color_threshold):
    """Pure in-memory scoring — no DB access. `scenes_by_video`/
    `audio_by_video` are the dicts produced by load_all_scenes/
    load_all_audio, shared read-only across all pairs (and, in the
    parallel path, across all worker processes)."""
    preview_scenes = scenes_by_video.get(preview_id)
    candidate_scenes = scenes_by_video.get(candidate_id)
    if not preview_scenes or not candidate_scenes:
        return None

    matched = []
    for p_scene in preview_scenes:
        m = best_scene_match(p_scene, candidate_scenes, hash_threshold, color_threshold)
        if m:
            matched.append(m)

    visual_score = len(matched) / len(preview_scenes)

    # Audio score (only if both sides have a usable fingerprint)
    audio_score = None
    p_audio = audio_by_video.get(preview_id)
    c_audio = audio_by_video.get(candidate_id)
    if p_audio and c_audio and p_audio["fp_ok"] and c_audio["fp_ok"] and p_audio["fingerprint"] and c_audio["fingerprint"]:
        audio_score = chromaprint_similarity(p_audio["fingerprint"], c_audio["fingerprint"])

    if audio_score is not None:
        combined = 0.65 * visual_score + 0.35 * audio_score
    else:
        combined = visual_score

    return {
        "visual_score": visual_score,
        "audio_score": audio_score,
        "combined_score": combined,
        "scene_matches": matched,
    }


def chromaprint_similarity(fp_a: str, fp_b: str) -> float:
    """
    Rough similarity between two raw chromaprint fingerprints (comma or
    space separated ints from `fpcalc -raw`). Computes best-effort bit
    overlap over the overlapping prefix length. This is intentionally
    simple — for production-grade alignment (handling offset/duration
    mismatch between a short preview clip and a long source), shipping
    against `pyacoustid`'s decode + cross-correlation is more robust;
    flagged in README as a tuning point for the Claude Code follow-up.
    """
    try:
        a = [int(x) for x in fp_a.replace(",", " ").split()]
        b = [int(x) for x in fp_b.replace(",", " ").split()]
    except ValueError:
        return 0.0
    if not a or not b:
        return 0.0

    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    matches = sum(1 for i in range(n) if bin(a[i] ^ b[i]).count("1") <= 4)
    return matches / n


def _chunk_pairs(pairs: list, workers: int) -> list[list]:
    """Split `pairs` into chunks sized so there are comfortably more
    chunks than workers (for load balancing and incremental progress
    reporting) without going so fine-grained that per-task scheduling/IPC
    overhead dominates the actual scoring work. Aims for ~4x as many
    chunks as workers, floored at 40 chunks total so small workers counts
    still get reasonable progress granularity, with a minimum chunk size
    of 1 (so this never divides by zero or produces an empty chunk)."""
    if not pairs:
        return []
    target_chunks = max(workers * 4, 40)
    chunk_size = max(1, -(-len(pairs) // target_chunks))  # ceil division
    return [pairs[i:i + chunk_size] for i in range(0, len(pairs), chunk_size)]


# Populated once per worker process by _init_worker — see module docstring's
# PERFORMANCE section for why this is an initializer (run once per worker)
# rather than data passed with every task.
_WORKER_STATE: dict = {}


def _init_worker(scenes_by_video, audio_by_video, hash_threshold, color_threshold):
    _WORKER_STATE["scenes"] = scenes_by_video
    _WORKER_STATE["audio"] = audio_by_video
    _WORKER_STATE["hash_threshold"] = hash_threshold
    _WORKER_STATE["color_threshold"] = color_threshold


def _score_chunk(pairs_chunk: list) -> list:
    """Runs in a worker process. Returns (preview_id, candidate_id, result)
    for every pair in the chunk — filtering against --min-visual-score/
    --min-matched-scenes happens back in the main process, same as the
    sequential path, so both code paths apply identical thresholds."""
    scenes = _WORKER_STATE["scenes"]
    audio = _WORKER_STATE["audio"]
    hash_threshold = _WORKER_STATE["hash_threshold"]
    color_threshold = _WORKER_STATE["color_threshold"]
    return [
        (preview_id, candidate_id, score_pair(scenes, audio, preview_id, candidate_id, hash_threshold, color_threshold))
        for preview_id, candidate_id in pairs_chunk
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "data/library.db"), help="SQLite DB path (env: DB_PATH)")
    ap.add_argument("--hash-threshold", type=int, default=8, help="max Hamming distance (0-64) to count a scene as matched")
    ap.add_argument("--color-threshold", type=float, default=0.25, help="max color-signature distance (0-1) to count a scene as matched")
    ap.add_argument("--min-ratio", type=float, default=0.02, help="min preview/candidate duration ratio")
    ap.add_argument("--max-ratio", type=float, default=0.95, help="max preview/candidate duration ratio")
    ap.add_argument("--top-n", type=int, default=5, help="store top N candidate matches per preview")
    ap.add_argument("--min-visual-score", type=float, default=0.15, help="skip storing matches below this visual score (noise floor)")
    ap.add_argument("--min-matched-scenes", type=int, default=3,
                     help="skip storing matches with fewer than this many matched scenes — a high *fraction* "
                          "from a tiny sample (e.g. 1/2) is weak, coincidental evidence; see module docstring")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1),
                     help="parallel scoring processes (pure CPU-bound, no I/O contention — unlike "
                          "02_fingerprint.py's --workers, more cores should mostly just help here, but "
                          "this hasn't been benchmarked for real yet; see README's Tuning section). "
                          "1 = sequential, no process pool. Default: cpu count - 1")
    ap.add_argument("--run-id", type=int, default=None,
                     help="internal: scan_runs row to report progress to (set by the web UI's scan orchestrator)")
    args = ap.parse_args()

    if args.workers < 1:
        ap.error("--workers must be >= 1")

    init_db(args.db)

    with connect(args.db) as conn:
        videos = conn.execute(
            "SELECT id, path, filename, duration_sec FROM videos WHERE fingerprinted_at IS NOT NULL"
        ).fetchall()
        dismissed = load_dismissed_pairs(conn)
        scenes_by_video = load_all_scenes(conn)
        audio_by_video = load_all_audio(conn)

    if not videos:
        update_scan_run(args.db, args.run_id, stage="match", stage_total=0, stage_done=0,
                         stage_started_at=time.time(), message="no fingerprinted videos found",
                         updated_at=time.time())
        print("No fingerprinted videos found. Run 02_fingerprint.py first.")
        return

    print(f"{len(videos)} fingerprinted videos loaded. Building candidate pairs by duration ratio...")
    if dismissed:
        print(f"{len(dismissed)} pair(s) excluded by prior 'not a match' feedback.")

    pairs = []
    for p in videos:
        for c in videos:
            if p["id"] == c["id"]:
                continue
            if (p["id"], c["id"]) in dismissed:
                continue
            if not p["duration_sec"] or not c["duration_sec"] or c["duration_sec"] <= 0:
                continue
            ratio = p["duration_sec"] / c["duration_sec"]
            # candidate must be longer (or comparable) — preview is the shorter side
            if args.min_ratio <= ratio <= args.max_ratio:
                pairs.append((p["id"], c["id"]))

    print(f"{len(pairs)} candidate pairs after duration prefilter (out of {len(videos)*(len(videos)-1)} possible)")
    print(f"Scoring with {args.workers} worker{'s' if args.workers != 1 else ''}.")
    update_scan_run(args.db, args.run_id, stage="match", stage_total=len(pairs), stage_done=0,
                     stage_started_at=time.time(), message=f"{len(pairs)} candidate pairs",
                     updated_at=time.time())

    results_by_preview = {}
    t0 = time.time()
    done_pairs = 0

    if args.workers == 1:
        for i, (preview_id, candidate_id) in enumerate(pairs, 1):
            res = score_pair(scenes_by_video, audio_by_video, preview_id, candidate_id, args.hash_threshold, args.color_threshold)
            if (res and res["visual_score"] >= args.min_visual_score
                    and len(res["scene_matches"]) >= args.min_matched_scenes):
                results_by_preview.setdefault(preview_id, []).append((candidate_id, res))

            if i % 500 == 0:
                print(f"  ...{i}/{len(pairs)} pairs scored ({(time.time()-t0):.1f}s elapsed)")
                update_scan_run(args.db, args.run_id, stage_done=i,
                                 message=f"{i}/{len(pairs)} pairs scored", updated_at=time.time())
        done_pairs = len(pairs)
    else:
        chunks = _chunk_pairs(pairs, args.workers)
        with ProcessPoolExecutor(
            max_workers=args.workers, initializer=_init_worker,
            initargs=(scenes_by_video, audio_by_video, args.hash_threshold, args.color_threshold),
        ) as pool:
            futures = {pool.submit(_score_chunk, chunk): len(chunk) for chunk in chunks}
            for fut in as_completed(futures):
                for preview_id, candidate_id, res in fut.result():
                    if (res and res["visual_score"] >= args.min_visual_score
                            and len(res["scene_matches"]) >= args.min_matched_scenes):
                        results_by_preview.setdefault(preview_id, []).append((candidate_id, res))
                done_pairs += futures[fut]
                print(f"  ...{done_pairs}/{len(pairs)} pairs scored ({(time.time()-t0):.1f}s elapsed)")
                update_scan_run(args.db, args.run_id, stage_done=done_pairs,
                                 message=f"{done_pairs}/{len(pairs)} pairs scored", updated_at=time.time())

    with connect(args.db) as conn:
        # Keep only top-N per preview, write to matches table
        conn.execute("DELETE FROM matches")  # matching is fully re-derivable, safe to recompute fresh
        total_stored = 0
        for preview_id, candidates in results_by_preview.items():
            candidates.sort(key=lambda x: x[1]["combined_score"], reverse=True)
            for candidate_id, res in candidates[: args.top_n]:
                conn.execute(
                    """INSERT OR REPLACE INTO matches
                       (preview_id, candidate_id, visual_score, audio_score, combined_score, scene_matches_json, computed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (preview_id, candidate_id, res["visual_score"], res["audio_score"],
                     res["combined_score"], json.dumps(res["scene_matches"]), time.time()),
                )
                total_stored += 1

    update_scan_run(args.db, args.run_id, stage_done=len(pairs),
                     message=f"{total_stored} matches stored", updated_at=time.time())
    print(f"\nDone in {(time.time()-t0):.1f}s. {total_stored} match rows stored for {len(results_by_preview)} previews with at least one candidate.")
    print("Next: launch the review UI (04_serve.py) to inspect and confirm matches.")


if __name__ == "__main__":
    main()
