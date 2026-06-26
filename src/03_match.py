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

MINIMUM SCENE DURATION (--min-scene-duration, default 2.0s) AND MATCH
SPREAD (--min-match-spread, default 2.0s):
    A second, distinct false-positive class found via real review (video
    #2237): a preview and an unrelated candidate both opened with the same
    shared intro/logo animation, which ffmpeg's scene detection chopped
    into several quick cuts within ~2 seconds. All of them matched (hash
    distance 0, 0, 2) and cleared --min-matched-scenes (3/3) — but all
    three were the same ~4-second intro, not three independent
    corroborating moments. --min-matched-scenes guards against a single
    coincidental hit; it does nothing against several hits that are all
    really the same hit.
    --min-scene-duration drops any scene whose gap to the *next*
    scene-cut in its own video is below this threshold before scoring —
    a logo sting's rapid-fire cuts aren't independently identifiable
    scenes and shouldn't count as (or dilute/inflate) evidence either
    way. Applied once in load_all_scenes() to both sides of every pair
    (a video can be someone else's candidate), not per-pair. A scene with
    no next cut (the last one in its video) has no known duration and is
    always kept rather than guessed at.
    --min-match-spread independently requires the matched scenes'
    *preview* timestamps to span at least this many seconds — guards
    against scenes that individually pass the duration floor but still
    all land within the same narrow moment (e.g. a longer single shared
    title card). Both are starting points (2.0s), not calibrated values —
    tune with real false positives/negatives in hand, same as
    --hash-threshold's history above.

CANDIDATE-SIDE MATCH SPREAD (--min-candidate-match-spread, default 2.0s):
    A third false-positive class, found via real review (video #4059):
    a ~2.8s intro/bumper appeared *three separate times* in the preview
    (spliced in at three different points — a real preview re-using its
    own intro, not a rapid-cut sting), each cleanly above
    --min-scene-duration and spread well past --min-match-spread on the
    *preview* side. All three matched the *same single* candidate
    timestamp, because the candidate only has that intro once. The
    candidate itself was not actually a match at all — three repeats of
    one shared, genuinely-coincidental clip (a generic studio bumper) is
    not three independent corroborating moments, no matter how spread
    out they are in the preview. --min-match-spread didn't catch this
    because it only ever looked at the preview side; the *candidate*-side
    timestamps of those three matches were all identical (or
    near-identical), which is exactly the signal that's actually
    missing. --min-candidate-match-spread mirrors --min-match-spread
    but requires spread among the matched scenes' *candidate* timestamps
    instead — three matches that collapse onto one candidate moment now
    fail this regardless of how spread out they are in the preview.
    Both spread checks must pass; either axis alone is insufficient,
    since a real match needs corroboration that's independent on *both*
    sides, not just one.

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
    real overhead) — originally paired with `int.bit_count()` instead of
    `bin(x).count("1")` for the XOR popcount; both the per-pair Python
    loop and that scalar popcount were later replaced wholesale by
    `score_scenes`'s vectorized numpy version, see VECTORIZED SCORING
    below.

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

    COST-AWARE CHUNKING: `_chunk_pairs`'s `cost_fn` parameter balances
    each chunk's *estimated total cost* (scene-count product), not just
    its pair count — fixes a real "workers drain to a handful of
    stragglers" issue found live on homeserver. See `_chunk_pairs`'s own
    docstring for the full story, including a real OOM/server-crash
    incident caused by an earlier version of this fix that sorted a
    cost-tagged copy of every pair up front (~2.5-3GB of transient
    allocation at full-library scale) — the shipped version greedily
    bin-packs in arrival order instead, with no per-pair allocation
    beyond what plain slicing already needed.

VECTORIZED SCORING (`VideoScenes`/`score_scenes`, replacing the old
per-scene Python-dict / nested-loop `best_scene_match`): two separate
problems, one fix. (1) A 64-bit hash as a Python int is 36 bytes
(measured via `sys.getsizeof`); a bare scene dict's own shell (before any
of its values) is ~270 bytes — at full-library scale that's real memory,
not rounding error. (2) More importantly, the same OOM/server-crash
incident referenced above was most likely actually caused by something
this section fixes, not the chunking bug: `ProcessPoolExecutor`'s
`initargs` hands every forked worker the same preloaded scene data, and
Linux's `fork()` normally shares those pages copy-on-write — but CPython's
reference counting touches every object's refcount on *any* access, even
a read, which dirties the page and forces a private copy. With scene data
as a Python dict-of-dicts, every single hash/timestamp lookup during
scoring is exactly such a touch, so each of the N forked workers
gradually accumulates its own copies of pages it merely *read* — a slow
RSS creep over a run's lifetime, not a one-time cost (this fits the
observed incident's timeline far better than the chunking bug above,
which spikes once, upfront, before any pair is scored). `VideoScenes`
packs each video's hashes/timestamps into contiguous numpy arrays
instead; a vectorized op (`np.bitwise_count`, numpy 2.0+) reads the raw
buffer directly in C without creating a Python object per element, so it
never touches a per-hash refcount — those pages stay genuinely shared
across all forked workers for the buffer's entire lifetime, not just at
fork time. `score_scenes()` computes the full (n_preview, n_candidate, 3,
3) Hamming-distance tensor for an entire pair in a handful of numpy
calls instead of n_preview * n_candidate * 9 individual Python-level
comparisons — a speedup, but secondary to the memory/sharing fix above.
Tie-breaking matches the old nested-loop order exactly (see
`score_scenes`'s own docstring) — verified against the old implementation
across thousands of randomized trials, including deliberately-induced
ties, before it was replaced; `--workers 1` vs `--workers N` against
identical seeded DBs were also re-confirmed byte-identical after this
change, same as after every previous change to this stage.

Confirmed live against the real ~5000-video library (~11.7M pairs):
total time dropped from ~16.5min (pre-fix, 15 workers) to ~7.5min
(post-vectorization, 15 workers) — but `docker stats` showed total
memory climbing steadily throughout the run (~2.9GB shortly after
starting, ~5.4GB at 50% progress, ~8GB near completion), dropping to
baseline (~42MB) the instant workers exited. That drop-on-exit, plus no
code path here that accumulates state across pairs/chunks, points to
glibc/numpy allocator retention (freed-but-not-returned-to-the-OS memory
from millions of per-pair temporary numpy arrays and score_pair() result
dicts), not a Python-level reference leak — the vectorization fix above
addresses the *shared* base data staying shared (confirmed working: each
worker's individual RSS was close to the *total*, meaning the shared
portion was counted once across the whole pool, not duplicated per
worker), it just doesn't stop each worker's own per-task allocator
fragmentation from growing over a long enough run.

`--max-tasks-per-child` (DISABLED by default — do not enable, see below)
was a first attempt at fixing that fragmentation by having
`ProcessPoolExecutor` recycle a worker after N chunks. It deadlocked a
real production run instead: workers bled out to zero with no error in
any log, the run hung forever, and `docker top` showed 18 threads in the
main process where ~2 are expected. Root cause confirmed directly in
this Python version's own stdlib source
(`concurrent/futures/process.py`'s `_adjust_process_count`): replacing a
dead/recycled worker forks a new child *while the executor's own
background management thread (`_ExecutorManagerThread`) is still
running* — forking a multi-threaded parent is a classic deadlock hazard
(any lock held by a thread other than the forking one stays locked
forever in the child, since that thread doesn't exist there to release
it), and CPython's own source comment acknowledges this exact gap, with
a citation to a real, still-open issue
(https://github.com/python/cpython/issues/90622). This isn't a
configuration mistake on the caller's part — it's a property of the
default 'fork' multiprocessing start method (which this script always
uses, since switching to 'spawn' would mean re-pickling and re-sending
the entire preloaded scene dataset over IPC for every worker, including
the initial ones, defeating the whole point of the copy-on-write sharing
this stage relies on). Until that CPython issue is fixed, recycling
workers via this executor is unsafe here — leave `--max-tasks-per-child`
at 0. The underlying allocator-fragmentation problem this was trying to
fix is still real and still unaddressed by that abandoned attempt.

**Second attempt — `_trim_worker_memory()` (calls glibc's `malloc_trim(0)`
via `ctypes`, preceded by `gc.collect()`, at the end of every
`_score_chunk()`) — ran clean (no fork, no deadlock) but didn't fix the
problem.** Confirmed live: total runtime went *up* (~7.5min -> ~9.5min,
~27% slower) while memory growth and the final ceiling were essentially
unchanged (~2.7GB -> ~5.3GB at 50% -> ~8GB, same as before) — periodic
small drops were visible, proving the call does *something*, but it
didn't touch the dominant driver. That's a real, informative negative
result: it means the growth was never primarily about per-worker
allocator fragmentation at all. Now gated behind `--trim-worker-memory`
(off by default, opt-in) rather than removed outright, in case it's
worth re-testing once the real fix below is also in play.

**Third attempt — `results_by_preview`'s unbounded accumulation, found by
re-reading this function's own aggregation loop — ran clean and was a
real, separate bug, but confirmed live to *also* not be the dominant
driver.** It accumulated *every* pair that passed
`--min-visual-score`/`--min-matched-scenes`/`--min-match-spread` for the
entire run, in the main process, only sorting and trimming to `--top-n`
once at the very end — fixed by `_record_candidate()` (a bounded
(<=`--top-n` per preview) min-heap maintained as results stream in,
evicting the worst candidate immediately when a better one arrives).
Verified correct (byte-identical eviction behavior under `--workers 1`
vs `--workers 3` against a seeded DB built specifically to exercise
eviction) — but confirmed live against the real library that memory
growth was *unchanged*: same ~2.7GB -> ~5.5GB -> ~8GB curve as every
attempt before it. `_record_candidate` is still correct and worth
keeping (it's a real, if small, efficiency win and removes genuinely
unbounded growth), it just isn't the multi-GB story.

**Actual mechanism, found via `--debug-memory-objects` live on the real
library**: neither the main process's RSS *nor any individual worker's*
RSS grows over the run — confirmed across all 15 workers' full
lifetimes, each plateauing within its first 1-3 chunks (~5MB total) and
then sitting completely flat for the rest of its life (one worker
processed 816,149 more pairs after plateauing with zero further RSS
growth). `gc.get_objects()` per worker also stays flat throughout —
ruling out a Python-level reference leak. Yet the cgroup-wide total
climbs the entire run. The only mechanism that reconciles "every
process's own memory size is flat" with "the aggregate keeps climbing"
is copy-on-write divergence: pages that started out genuinely shared
across the main process and all 15 workers gradually become exclusively-
owned private copies, one worker at a time, as each one's
`scenes_by_video.get(id)` calls touch (and thus dirty the page of) more
of the ~4765 `VideoScenes` container objects over its lifetime. A
touched page's *size* doesn't change for the worker now privately
holding it — so no individual RSS reading moves — but the cgroup's count
of distinct physical pages goes up every time another worker's copy
diverges from the others. This also explains why `malloc_trim()`/
`gc.collect()` did nothing: there's no freed memory here to reclaim —
these are live, still-referenced pages simply losing shared status, not
garbage. (And it retroactively explains why `--max-tasks-per-child` was
conceptually the *right* idea — a fresh fork gets fresh sharing — just
implemented via a buggy mechanism.)

Fixed (untested in production as of this writing) by
`--pool-generation-chunks`: periodically does a full, clean
`shutdown(wait=True)` of the entire pool and constructs a brand new
`ProcessPoolExecutor`, re-forking fresh workers from the main process
and restoring full sharing — see `--pool-generation-chunks`'s own help
text for why this avoids `--max-tasks-per-child`'s deadlock (it goes
through `_launch_processes()`, only reachable when no manager thread is
alive, instead of `_adjust_process_count()`, which forks while one is).

Usage:
    python3 src/03_match.py --db data/library.db [--hash-threshold 8]
                             [--color-threshold 0.25] [--min-ratio 0.02]
                             [--max-ratio 0.95] [--top-n 5]
                             [--min-visual-score 0.15] [--min-matched-scenes 3]
                             [--min-scene-duration 2.0] [--min-match-spread 2.0]
                             [--min-candidate-match-spread 2.0]
                             [--workers N]
"""

import argparse
import ctypes
import gc
import heapq
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from db import connect, init_db, update_scan_run
import phash as ph

# Column order for VideoScenes.phash — must match the old p_variants/
# c_variants tuple order exactly (see VideoScenes docstring): tie-breaking
# in score_scenes() depends on this matching the historical iteration
# order bit-for-bit.
PHASH_VARIANTS = ("normal", "cropped", "flipped")


@dataclass
class VideoScenes:
    """Columnar, numpy-backed scene data for one video — replaces the old
    list of per-scene Python dicts (see module docstring's VECTORIZED
    SCORING section). Packs hashes/timestamps into contiguous numpy
    buffers instead of one Python int/float/dict object per field per
    scene: a 64-bit hash as a Python int is 36 bytes (measured via
    sys.getsizeof) vs. 8 bytes packed in a uint64 array with zero
    per-element object overhead, and a bare scene dict's own shell is
    ~270 bytes before counting any of its values. More importantly for a
    forked ProcessPoolExecutor: reading a numpy array during a vectorized
    op never touches a per-element Python refcount, so these buffers stay
    genuinely shared copy-on-write across worker processes instead of
    each worker privately duplicating pages as it touches them — see
    CHANGELOG for the production incident this addresses.

    `color_sig` is nullable per-scene in the DB (legacy data only — every
    scene `02_fingerprint.py` writes today always has one); `has_color_sig`
    tracks that per scene since a numpy array can't hold a ragged/None
    entry inline."""
    timestamp_sec: np.ndarray      # float64, shape (n,)
    duration_to_next: np.ndarray   # float64, shape (n,); NaN = unknown (last scene in its video)
    phash: np.ndarray              # uint64, shape (n, 3); columns in PHASH_VARIANTS order
    color_sig: np.ndarray          # uint8, shape (n, sig_len)
    has_color_sig: np.ndarray      # bool, shape (n,)

    def __len__(self):
        return len(self.timestamp_sec)


def load_dismissed_pairs(conn) -> set[tuple[int, int]]:
    """(preview_id, candidate_id) pairs a human has explicitly flagged as
    not a match — see module docstring's DISMISSED PAIRS section."""
    rows = conn.execute(
        "SELECT preview_id, candidate_id FROM match_feedback WHERE status = 'not_a_match'"
    ).fetchall()
    return {(r["preview_id"], r["candidate_id"]) for r in rows}


def load_all_scenes(conn, min_scene_duration: float = 0.0) -> dict[int, VideoScenes]:
    """Every video's scenes, preloaded once into columnar numpy arrays
    (`VideoScenes`) — see module docstring's PERFORMANCE/VECTORIZED
    SCORING sections for why this replaced a per-pair, per-side `SELECT`
    and then, later, a list of per-scene Python dicts. Hashes are parsed
    from hex to int here, once, so nothing downstream re-parses the same
    string across the many pairs that share a video.

    Each video also gets `duration_to_next` (gap to the next scene-cut in
    the same video, by `scene_index` order; NaN for a video's last scene
    — its true duration isn't knowable from `scenes` alone, see module
    docstring). Scenes with a known duration below `min_scene_duration`
    are dropped entirely (not just down-weighted) before any pair is
    scored — see module docstring's MINIMUM SCENE DURATION section."""
    rows = conn.execute(
        "SELECT video_id, scene_index, timestamp_sec, phash, phash_cropped, phash_flipped, color_sig "
        "FROM scenes ORDER BY video_id, scene_index"
    ).fetchall()
    raw_by_video: dict[int, list] = {}
    for r in rows:
        raw_by_video.setdefault(r["video_id"], []).append(r)

    by_video: dict[int, VideoScenes] = {}
    for video_id, scene_rows in raw_by_video.items():
        n = len(scene_rows)
        timestamps = np.array([r["timestamp_sec"] for r in scene_rows], dtype=np.float64)

        durations = np.full(n, np.nan, dtype=np.float64)
        durations[:-1] = timestamps[1:] - timestamps[:-1]  # no-op slice when n<=1

        keep = np.isnan(durations) | (durations >= min_scene_duration)
        if not keep.all():
            scene_rows = [r for r, k in zip(scene_rows, keep) if k]
            timestamps = timestamps[keep]
            durations = durations[keep]
            n = len(scene_rows)

        phash = np.empty((n, 3), dtype=np.uint64)
        has_color_sig = np.zeros(n, dtype=bool)
        sig_len = next((len(r["color_sig"]) for r in scene_rows if r["color_sig"]), 0)
        color_sig = np.zeros((n, sig_len), dtype=np.uint8)
        for i, r in enumerate(scene_rows):
            phash[i, 0] = int(r["phash"], 16)
            phash[i, 1] = int(r["phash_cropped"], 16)
            phash[i, 2] = int(r["phash_flipped"], 16)
            sig = r["color_sig"]
            if sig and len(sig) == sig_len:
                has_color_sig[i] = True
                color_sig[i] = [int(ch, 16) for ch in sig]

        by_video[video_id] = VideoScenes(
            timestamp_sec=timestamps, duration_to_next=durations,
            phash=phash, color_sig=color_sig, has_color_sig=has_color_sig,
        )
    return by_video


def load_all_audio(conn) -> dict[int, dict]:
    """Every video's audio fingerprint, preloaded once (same reasoning as
    load_all_scenes)."""
    rows = conn.execute("SELECT video_id, fingerprint, fp_ok FROM audio_fp").fetchall()
    return {r["video_id"]: {"fingerprint": r["fingerprint"], "fp_ok": r["fp_ok"]} for r in rows}


def score_scenes(preview: VideoScenes, candidate: VideoScenes, hash_threshold, color_threshold) -> list[dict]:
    """Vectorized replacement for the old per-scene best_scene_match()
    Python loop — see module docstring's VECTORIZED SCORING section.
    Computes the full (n_preview, n_candidate, 3, 3) Hamming-distance
    tensor in a handful of numpy calls instead of nested Python loops
    (n_preview * n_candidate * 9 individual comparisons, each with its
    own function-call/tuple-unpacking overhead), then reduces to one best
    match per preview scene.

    Tie-breaking (which (candidate_scene, p_variant, c_variant) wins when
    several are equally close) is bit-for-bit identical to the old "first
    strictly-lower distance wins" rule: flattening the trailing
    (candidate_scene, p_variant, c_variant) axes in that order — candidate
    scene slowest, p_variant middle, c_variant fastest — reproduces the
    old nested loop's exact iteration order, and numpy's argmin returns
    the first occurrence on ties just like the old `dist < best[0]`
    strict inequality did. Verified against the old implementation across
    thousands of randomized trials with deliberately-induced ties before
    this replaced it (see CHANGELOG).
    """
    n_p, n_c = len(preview), len(candidate)
    if n_p == 0 or n_c == 0:
        return []

    p_b = preview.phash[:, np.newaxis, :, np.newaxis]    # (n_p,1,3,1)
    c_b = candidate.phash[np.newaxis, :, np.newaxis, :]  # (1,n_c,1,3)
    dist = np.bitwise_count(p_b ^ c_b)                   # (n_p,n_c,3,3)

    flat = dist.reshape(n_p, n_c * 9)
    best_flat_idx = flat.argmin(axis=1)
    best_dist = flat[np.arange(n_p), best_flat_idx]
    j_idx, pv_idx, cv_idx = np.unravel_index(best_flat_idx, (n_c, 3, 3))

    matched = []
    for i in range(n_p):
        dist_i = int(best_dist[i])
        if dist_i > hash_threshold:
            continue
        j = int(j_idx[i])

        # Color-collision guard: only applied against the "normal"
        # comparison color signature (one value per scene, independent
        # of which crop/flip variant won), since crop changes histogram
        # proportions by design — same as the old best_scene_match().
        if preview.has_color_sig[i] and candidate.has_color_sig[j]:
            sig_len = preview.color_sig.shape[1]
            c_dist = float(np.abs(preview.color_sig[i].astype(np.float64)
                                   - candidate.color_sig[j].astype(np.float64)).sum() / (15.0 * sig_len))
        else:
            c_dist = 0.0
        if c_dist > color_threshold:
            continue

        p_dur = preview.duration_to_next[i]
        c_dur = candidate.duration_to_next[j]
        matched.append({
            "distance": dist_i,
            "candidate_ts": float(candidate.timestamp_sec[j]),
            "variant": f"{PHASH_VARIANTS[pv_idx[i]]}-vs-{PHASH_VARIANTS[cv_idx[i]]}",
            "preview_ts": float(preview.timestamp_sec[i]),
            "preview_scene_duration": None if np.isnan(p_dur) else float(p_dur),
            "candidate_scene_duration": None if np.isnan(c_dur) else float(c_dur),
        })
    return matched


def score_pair(scenes_by_video, audio_by_video, preview_id, candidate_id, hash_threshold, color_threshold):
    """Pure in-memory scoring — no DB access. `scenes_by_video`/
    `audio_by_video` are the dicts produced by load_all_scenes/
    load_all_audio, shared read-only across all pairs (and, in the
    parallel path, across all worker processes)."""
    preview_scenes = scenes_by_video.get(preview_id)
    candidate_scenes = scenes_by_video.get(candidate_id)
    if not preview_scenes or not candidate_scenes:
        return None

    matched = score_scenes(preview_scenes, candidate_scenes, hash_threshold, color_threshold)

    visual_score = len(matched) / len(preview_scenes)

    # How much of the preview's own timeline the matched scenes actually
    # span — see module docstring's MATCH SPREAD section. 0.0 for a single
    # match (nothing to spread across), not None, so it composes cleanly
    # with a >= threshold check in the caller.
    match_spread_sec = (
        max(m["preview_ts"] for m in matched) - min(m["preview_ts"] for m in matched)
        if len(matched) > 1 else 0.0
    )

    # Mirror of match_spread_sec on the *candidate* side — see module
    # docstring's CANDIDATE-SIDE MATCH SPREAD section. Several preview
    # scenes spread across the preview can still all be the same single
    # shared moment if they all best-match the same candidate timestamp;
    # this is the signal that actually catches that, where match_spread_sec
    # (preview-only) does not.
    candidate_match_spread_sec = (
        max(m["candidate_ts"] for m in matched) - min(m["candidate_ts"] for m in matched)
        if len(matched) > 1 else 0.0
    )

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
        "match_spread_sec": match_spread_sec,
        "candidate_match_spread_sec": candidate_match_spread_sec,
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


# Rough single-worker throughput estimate used to translate
# --progress-interval (seconds) into a pairs-per-chunk target — see
# _chunk_pairs's docstring. Derived from the one real measurement on
# record: ~11.7M pairs / ~16.5 min / 15 workers (homeserver, the run that
# preceded the 0.11.x chunking fix and the 0.12.0 vectorization rewrite)
# = ~788 pairs/sec/worker. That run predates vectorized scoring, so this
# is almost certainly conservative now (chunks will likely finish faster
# than requested, not slower) — recalibrate against a real vectorized
# run's measured throughput once one exists, rather than trusting this
# number indefinitely.
PAIRS_PER_WORKER_SEC = 788


def _chunk_pairs(pairs: list, workers: int, cost_fn=None, progress_interval_sec: float = None) -> list[list]:
    """Split `pairs` into chunks sized so there are comfortably more
    chunks than workers (for load balancing and incremental progress
    reporting) without going so fine-grained that per-task scheduling/IPC
    overhead dominates the actual scoring work. Aims for ~4x as many
    chunks as workers, floored at 40 chunks total so small workers counts
    still get reasonable progress granularity.

    `progress_interval_sec`, if given, additionally raises the chunk
    count so each chunk represents roughly this many seconds of work for
    *one* worker, using PAIRS_PER_WORKER_SEC's rough throughput estimate
    — found necessary because the count-based floor above doesn't scale
    with `len(pairs)` at all: a fixed ~60 chunks (15 workers * 4) means
    each chunk's *size*, and therefore duration, grows linearly with the
    pair count, which is exactly why a small test run feels responsive
    (chunks finish in seconds) while a real full-library run reports
    progress only every few minutes (each chunk takes minutes). This
    only ever raises the chunk count above the worker-based floor, never
    lowers it, so small runs are unaffected.

    `cost_fn(preview_id, candidate_id) -> float` estimates each pair's
    relative scoring cost (score_pair() is O(preview_scenes *
    candidate_scenes), not uniform per pair — see module docstring's
    PERFORMANCE section). When given, pairs are greedily bin-packed in
    their original order — each pair goes into whichever chunk currently
    has the lowest running cost total — so every chunk's *estimated
    total cost* ends up balanced, not just its pair count.

    This matters because `pairs` is built by a preview-outer loop (see
    main()), so every pair sharing one preview is contiguous — a single
    preview with an unusually high scene count produces a run of
    expensive pairs that, under plain equal-count slicing, can land in
    just one or two chunks. ProcessPoolExecutor already reassigns
    finished workers to the next queued chunk dynamically, but that only
    helps if no single chunk is disproportionately expensive — found
    live on homeserver (16 cores, --workers auto): the running worker
    count visibly drained over a match run as most chunks finished
    quickly and a couple of workers were left grinding through
    high-scene-count stragglers alone.

    Deliberately *not* sorted by descending cost first (textbook LPT
    scheduling) despite that giving a marginally tighter worst-case
    bound: at full-library scale (5000 videos, ~11.7M pairs) sorting
    means materializing a new (cost, preview_id, candidate_id) tuple per
    pair — measured at ~2.5-3GB of purely transient allocation, found
    live after it contributed to a real OOM/thrashing incident (system
    already had ~0 free RAM and fully-used swap from other concurrent
    work; this was the straw that broke it, server required a hard
    reboot — see CHANGELOG). Greedily filling the currently-lightest
    chunk in *arrival* order already spreads a contiguous run of
    expensive pairs across separate chunks just as well for this
    workload's actual failure mode (one or a few unusually scene-heavy
    previews, not an adversarial cost ordering) — the first chunk to
    receive an expensive pair stops being the lightest, so the next
    expensive pair (wherever it falls) goes to a different chunk. The
    only extra memory cost versus plain slicing is the n_chunks-sized
    heap; no per-pair allocation beyond what slicing already needed.
    Without `cost_fn`, falls back to plain equal-count slicing (used by
    callers that don't have per-video scene counts handy, e.g. tests)."""
    if not pairs:
        return []
    target_chunks = max(workers * 4, 40)
    if progress_interval_sec is not None:
        target_pairs_per_chunk = max(1, int(PAIRS_PER_WORKER_SEC * progress_interval_sec))
        target_chunks = max(target_chunks, -(-len(pairs) // target_pairs_per_chunk))  # ceil division
    n_chunks = min(target_chunks, len(pairs))

    if cost_fn is None:
        chunk_size = max(1, -(-len(pairs) // n_chunks))  # ceil division
        return [pairs[i:i + chunk_size] for i in range(0, len(pairs), chunk_size)]

    heap = [(0.0, i) for i in range(n_chunks)]  # (chunk's running total cost, chunk index)
    chunks: list[list] = [[] for _ in range(n_chunks)]
    for pc in pairs:
        total, idx = heapq.heappop(heap)
        chunks[idx].append(pc)  # reuse the existing tuple — no per-pair allocation
        heapq.heappush(heap, (total + cost_fn(*pc), idx))
    return chunks


# Populated once per worker process by _init_worker — see module docstring's
# PERFORMANCE section for why this is an initializer (run once per worker)
# rather than data passed with every task.
_WORKER_STATE: dict = {}

try:
    _LIBC = ctypes.CDLL("libc.so.6")
except OSError:
    _LIBC = None  # non-glibc platform (e.g. macOS dev box) — trimming becomes a no-op


def _trim_worker_memory():
    """Ask glibc to release freed-but-retained heap memory back to the OS.
    Gated behind --trim-worker-memory (off by default) — see module
    docstring's MEMORY GROWTH section: confirmed live to add ~27% to
    total runtime with *zero* measurable effect on the actual memory
    growth/ceiling, since the dominant driver turned out to be
    unbounded accumulation in the *main* process (`_record_candidate`),
    not per-worker allocator fragmentation. Kept available, not deleted,
    in case it's worth re-testing once that main-process fix is also in
    play — but do not enable by default again without new evidence.
    `gc.collect()` first gives CPython's own small-object allocator
    (pymalloc) its best chance to release any now-empty arenas back to
    glibc before asking glibc to release them to the OS. No-op on
    non-glibc platforms (`_LIBC` is None there)."""
    gc.collect()
    if _LIBC is not None:
        _LIBC.malloc_trim(0)


def _init_worker(scenes_by_video, audio_by_video, hash_threshold, color_threshold, trim_memory=False, debug_memory=False):
    _WORKER_STATE["scenes"] = scenes_by_video
    _WORKER_STATE["audio"] = audio_by_video
    _WORKER_STATE["hash_threshold"] = hash_threshold
    _WORKER_STATE["color_threshold"] = color_threshold
    _WORKER_STATE["trim_memory"] = trim_memory
    _WORKER_STATE["debug_memory"] = debug_memory
    _WORKER_STATE["chunks_done"] = 0
    _WORKER_STATE["pairs_done"] = 0


def _score_chunk(pairs_chunk: list) -> list:
    """Runs in a worker process. Returns (preview_id, candidate_id, result)
    for every pair in the chunk — filtering against --min-visual-score/
    --min-matched-scenes happens back in the main process, same as the
    sequential path, so both code paths apply identical thresholds.
    Trims the worker's heap before returning — see _trim_worker_memory's
    docstring; cheap relative to a chunk's actual scoring work."""
    scenes = _WORKER_STATE["scenes"]
    audio = _WORKER_STATE["audio"]
    hash_threshold = _WORKER_STATE["hash_threshold"]
    color_threshold = _WORKER_STATE["color_threshold"]
    result = [
        (preview_id, candidate_id, score_pair(scenes, audio, preview_id, candidate_id, hash_threshold, color_threshold))
        for preview_id, candidate_id in pairs_chunk
    ]
    if _WORKER_STATE.get("trim_memory"):
        _trim_worker_memory()
    if _WORKER_STATE.get("debug_memory"):
        _log_worker_memory_debug(len(pairs_chunk))
    return result


def _log_worker_memory_debug(chunk_pairs: int) -> None:
    """TEMPORARY diagnostic (not a permanent feature — see --debug-memory-objects'
    own help text) for tracking down the per-worker memory-growth incident:
    confirmed live that --trim-worker-memory's gc.collect()+malloc_trim()
    has zero effect, which rules out both a reference-cycle leak (gc.collect
    would have freed it) and simple allocator fragmentation that malloc_trim
    can reclaim. This logs gc.get_objects() counts (by type) and this
    worker's own peak RSS to stdout (-> docker logs) every 10 chunks, to see
    directly whether Python-visible object counts are actually growing
    (pointing at a genuine retained reference somewhere) or staying flat
    (pointing at something invisible to gc — e.g. heap fragmentation from
    many small interleaved live/dead allocations, which malloc_trim can't
    address since it only reclaims fully-free contiguous regions, not
    space fragmented between still-live objects)."""
    import resource

    _WORKER_STATE["chunks_done"] += 1
    _WORKER_STATE["pairs_done"] += chunk_pairs
    n = _WORKER_STATE["chunks_done"]
    if n % 10 != 0 and n > 3:
        return
    objs = gc.get_objects()
    counts = {}
    for o in objs:
        t = type(o).__name__
        counts[t] = counts.get(t, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"[debug-memory pid={os.getpid()}] chunks={n} pairs={_WORKER_STATE['pairs_done']} "
          f"maxrss_kb={rss_kb} total_gc_objects={len(objs)} top_types={top}", flush=True)


def _record_candidate(top_candidates: dict, preview_id: int, candidate_id: int, res: dict, top_n: int) -> None:
    """Maintain a bounded (<=top_n per preview) min-heap of the best
    candidates seen so far, instead of accumulating *every* passing
    candidate across the whole run before sorting and trimming once at
    the end. The old unbounded-accumulation approach was a real,
    multi-GB-scale memory driver in the main process — confirmed live:
    after `_trim_worker_memory` (a worker-side fix) measurably slowed the
    run down (~7.5min -> ~9.5min) with *zero* effect on the observed
    memory growth or ceiling, the only explanation left is that the
    dominant driver lives outside the workers entirely. A rough estimate
    backs this up: a (candidate_id, res) entry is ~2.5KB (scene_matches
    list + its dicts); at 11.7M pairs even a modest pass rate against
    --min-visual-score's fairly loose 0.15 default means millions of
    accumulated entries is plausible, squarely in the observed
    multi-GB range — see module docstring's MEMORY section.

    Heap entries are (combined_score, candidate_id, res) — heapq pops
    the smallest first, so the worst currently-kept candidate is always
    the one evicted when a better one arrives. On an exact tie at the
    eviction boundary, whichever arrived first is kept (matches the old
    stable-sort-then-slice behavior in the sequential path; the parallel
    path's arrival order was already non-deterministic run-to-run either
    way, since it depends on which worker's chunk happens to finish
    first)."""
    heap = top_candidates.setdefault(preview_id, [])
    entry = (res["combined_score"], candidate_id, res)
    if len(heap) < top_n:
        heapq.heappush(heap, entry)
    elif entry[0] > heap[0][0]:
        heapq.heapreplace(heap, entry)


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
    ap.add_argument("--min-scene-duration", type=float, default=2.0,
                     help="drop scenes shorter than this (seconds, gap to the next scene-cut in their own "
                          "video) before scoring — a rapid-cut intro/logo sting isn't an independently "
                          "identifiable scene; see module docstring's MINIMUM SCENE DURATION section")
    ap.add_argument("--min-match-spread", type=float, default=2.0,
                     help="skip storing matches whose matched scenes' preview timestamps span less than this "
                          "many seconds — several hits clustered in the same narrow moment aren't independent "
                          "corroboration; see module docstring's MATCH SPREAD section")
    ap.add_argument("--min-candidate-match-spread", type=float, default=2.0,
                     help="skip storing matches whose matched scenes' candidate timestamps span less than this "
                          "many seconds — several preview-side hits that all best-match the *same* candidate "
                          "moment aren't independent corroboration either, even if they're well spread out "
                          "in the preview; see module docstring's CANDIDATE-SIDE MATCH SPREAD section")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1),
                     help="parallel scoring processes (pure CPU-bound, no I/O contention — unlike "
                          "02_fingerprint.py's --workers, more cores should mostly just help here, but "
                          "this hasn't been benchmarked for real yet; see README's Tuning section). "
                          "1 = sequential, no process pool. Default: cpu count - 1")
    ap.add_argument("--progress-interval", type=float, default=10.0,
                     help="--workers > 1 only: target seconds of work per chunk, so progress updates "
                          "land roughly this often instead of the chunk count being fixed regardless "
                          "of pair count — see _chunk_pairs's docstring for the (rough, pre-vectorization) "
                          "throughput estimate this is translated through")
    ap.add_argument("--max-tasks-per-child", type=int, default=0,
                     help="--workers > 1 only: recycle a worker process after this many chunks (0 = "
                          "never recycle, the default). DISABLED BY DEFAULT — a real production run "
                          "deadlocked with this enabled: forking a replacement worker while "
                          "ProcessPoolExecutor's own management thread is alive is a known, unresolved "
                          "CPython bug (https://github.com/python/cpython/issues/90622, acknowledged in "
                          "_adjust_process_count's own source comment) when using the default 'fork' "
                          "start method, which this script always uses. Workers bled out to zero with no "
                          "error and the run hung forever. Only set this above 0 if that bug is fixed in "
                          "your Python version — see module docstring before touching this")
    ap.add_argument("--trim-worker-memory", action="store_true",
                     help="--workers > 1 only: call glibc's malloc_trim() at the end of every chunk "
                          "(_trim_worker_memory). OFF BY DEFAULT — measured live to add ~27%% to total "
                          "runtime (7.5min -> 9.5min) with *zero* effect on the actual memory growth/"
                          "ceiling. _record_candidate's bounded top-N heap (the next attempt) *also* had "
                          "zero effect when measured live — confirmed via live per-process monitoring that "
                          "the main process's own RSS stays completely flat for the whole run, so neither "
                          "guess was right. Still real, still unfixed as of this flag's last edit — see "
                          "--debug-memory-objects and module docstring before touching either flag")
    ap.add_argument("--debug-memory-objects", action="store_true",
                     help="--workers > 1 only: diagnostic, logs each worker's gc.get_objects() count (by "
                          "type) and peak RSS to stdout every 10 chunks. Already used live to settle the "
                          "memory-growth question: gc object counts and each worker's own maxrss both "
                          "stay completely flat for the worker's entire life, even as the cgroup-wide "
                          "total keeps climbing — ruling out a Python-level leak *and* per-process growth "
                          "of any kind, pointing at copy-on-write divergence instead (see "
                          "--pool-generation-chunks). Left available for re-validating that fix")
    ap.add_argument("--pool-generation-chunks", type=int, default=0,
                     help="--workers > 1 only: fully shut down and recreate the worker pool after this "
                          "many chunks (0 = one pool for the whole run, the default/old behavior). "
                          "Targets COW page-divergence memory growth that neither --max-tasks-per-child "
                          "(deadlocks) nor --trim-worker-memory (proven ineffective) could address — "
                          "confirmed live via --debug-memory-objects that no single process's own RSS "
                          "grows, yet the cgroup-wide total does, which only a sharing-status change (not "
                          "a leak, not fragmentation) explains. A full pool recreation re-forks fresh "
                          "workers from the main process, restoring full copy-on-write sharing. Unlike "
                          "--max-tasks-per-child's in-place worker replacement (forks a replacement while "
                          "the pool's own management thread is alive — the unresolved CPython bug), this "
                          "fully joins the old pool, including its management thread, via "
                          "shutdown(wait=True) before constructing a new one — going through "
                          "ProcessPoolExecutor's initial-launch code path instead of its dynamic-worker- "
                          "replacement one. UNTESTED IN PRODUCTION as of this flag's introduction — try "
                          "something like 20x your --workers count as a starting point")
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
        scenes_by_video = load_all_scenes(conn, min_scene_duration=args.min_scene_duration)
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

    print(f"{len(pairs):,} candidate pairs after duration prefilter (out of {len(videos)*(len(videos)-1):,} possible)")
    print(f"Scoring with {args.workers} worker{'s' if args.workers != 1 else ''}.")
    update_scan_run(args.db, args.run_id, stage="match", stage_total=len(pairs), stage_done=0,
                     stage_started_at=time.time(), message=f"{len(pairs):,} candidate pairs",
                     updated_at=time.time())

    results_by_preview = {}  # preview_id -> bounded min-heap, see _record_candidate
    t0 = time.time()
    done_pairs = 0

    if args.workers == 1:
        for i, (preview_id, candidate_id) in enumerate(pairs, 1):
            res = score_pair(scenes_by_video, audio_by_video, preview_id, candidate_id, args.hash_threshold, args.color_threshold)
            if (res and res["visual_score"] >= args.min_visual_score
                    and len(res["scene_matches"]) >= args.min_matched_scenes
                    and res["match_spread_sec"] >= args.min_match_spread
                    and res["candidate_match_spread_sec"] >= args.min_candidate_match_spread):
                _record_candidate(results_by_preview, preview_id, candidate_id, res, args.top_n)

            if i % 500 == 0:
                print(f"  ...{i:,}/{len(pairs):,} pairs scored ({(time.time()-t0):.1f}s elapsed)")
                update_scan_run(args.db, args.run_id, stage_done=i,
                                 message=f"{i:,}/{len(pairs):,} pairs scored", updated_at=time.time())
        done_pairs = len(pairs)
    else:
        def _pair_cost(preview_id, candidate_id):
            return len(scenes_by_video.get(preview_id, ())) * len(scenes_by_video.get(candidate_id, ()))

        chunks = _chunk_pairs(pairs, args.workers, cost_fn=_pair_cost, progress_interval_sec=args.progress_interval)
        gen_size = args.pool_generation_chunks or len(chunks)  # 0 -> one generation, the original behavior
        generations = [chunks[i:i + gen_size] for i in range(0, len(chunks), gen_size)]

        for gen_chunks in generations:
            # A fresh ProcessPoolExecutor per generation — see module
            # docstring's MEMORY GROWTH section. Each one forks brand new
            # workers from this (still-COW-shareable) main process, so
            # accumulated page-divergence from the previous generation's
            # workers is discarded when they exit; this constructor call
            # only runs once this generation's pool object exists, going
            # through _launch_processes() (manager thread is None at this
            # point) rather than _adjust_process_count()'s replace-a-
            # worker-while-the-pool-is-live path — see --pool-generation-
            # chunks' own help text for why that distinction matters.
            with ProcessPoolExecutor(
                max_workers=args.workers, initializer=_init_worker,
                initargs=(scenes_by_video, audio_by_video, args.hash_threshold, args.color_threshold,
                          args.trim_worker_memory, args.debug_memory_objects),
                max_tasks_per_child=(args.max_tasks_per_child or None),
            ) as pool:
                futures = {pool.submit(_score_chunk, chunk): len(chunk) for chunk in gen_chunks}
                for fut in as_completed(futures):
                    for preview_id, candidate_id, res in fut.result():
                        if (res and res["visual_score"] >= args.min_visual_score
                                and len(res["scene_matches"]) >= args.min_matched_scenes
                                and res["match_spread_sec"] >= args.min_match_spread
                                and res["candidate_match_spread_sec"] >= args.min_candidate_match_spread):
                            _record_candidate(results_by_preview, preview_id, candidate_id, res, args.top_n)
                    done_pairs += futures[fut]
                    print(f"  ...{done_pairs:,}/{len(pairs):,} pairs scored ({(time.time()-t0):.1f}s elapsed)")
                    update_scan_run(args.db, args.run_id, stage_done=done_pairs,
                                     message=f"{done_pairs:,}/{len(pairs):,} pairs scored", updated_at=time.time())
            # pool.shutdown(wait=True) already ran via __exit__ above —
            # the next generation's pool (if any) only gets constructed
            # after this one's manager thread and all workers have fully
            # joined, which is exactly the precondition _launch_processes
            # asserts.

    with connect(args.db) as conn:
        # Each preview's heap already holds at most top_n entries — see
        # _record_candidate — so this is just a final sort for write order.
        conn.execute("DELETE FROM matches")  # matching is fully re-derivable, safe to recompute fresh
        total_stored = 0
        for preview_id, heap in results_by_preview.items():
            for combined_score, candidate_id, res in sorted(heap, key=lambda t: t[0], reverse=True):
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
