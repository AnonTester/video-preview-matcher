"""
02_fingerprint.py — Extract scene-cut frames + perceptual hashes + audio
fingerprints for every video that hasn't been fingerprinted yet.

OVERLAY HANDLING (logos / corner bumpers / bottom bars / top scrollers):
    Previews frequently burn in a logo (usually a corner), a bottom info
    bar, or a scrolling notice/ticker along the top. These occupy a
    *fixed* pixel region across the whole preview and will corrupt a
    naive full-frame pHash: either two unrelated scenes look similar
    (because they share the same logo) or a true match scores low
    (because the overlay differs from the clean source frame).

    Fix: hash a SAFE CENTRAL REGION only, with margins inset from each
    edge to exclude the most common overlay zones. Default margins:
    top 12% (scrollers/tickers), bottom 12% (info bars/lower-thirds),
    left/right 6% (corner logos/bugs). This is a heuristic, not a
    detector — it trades a bit of resolution for robustness, which is
    the right trade given the scale of this library. The margins are
    CLI-configurable per the actual preview style observed.

SCENE DETECTION (not fixed-interval sampling):
    Previews splice together partial scenes in arbitrary order. Sampling
    every N seconds would often land mid-scene in one video and on a
    completely different moment in the matching scene of another. Using
    ffmpeg's scene-change detection to grab one frame per shot boundary
    instead aligns naturally with how the preview was edited — each
    extracted frame represents the start of a discrete shot, which is
    the unit previews actually get cut along.

CROP / FLIP VARIANTS:
    For every extracted (safe-region) frame we store three hashes:
      - phash:          the safe region as-is
      - phash_cropped:  a further center-cropped version of the safe
                         region (catches previews that are cropped in,
                         e.g. zoomed/punched-in compared to source)
      - phash_flipped:  horizontal mirror of the safe region
    Matching checks a preview frame's hash against all three variants
    of every candidate frame, so any single combination of crop+flip
    is still caught without needing 3x the comparisons on both sides.

BLANK-FRAME GUARD:
    Fade-to-black transitions and blank intro/logo cards collapse to a
    DEGENERATE pHash: the DCT of a constant-color image is zero
    everywhere, so the median-threshold step is False for every bit —
    every uniform frame in the whole library hashes to the same all-zero
    bitstring. Confirmed against real library data: two unrelated movies'
    blank intro frames "matched" with Hamming distance 0.
    --blank-std-threshold (default 4.0, grayscale std-dev on a 0-255
    scale) skips storing a frame as a scene at all once it's this flat —
    see phash.is_blank().

HARDWARE ACCELERATION (AMD GPU):
    Scene extraction requires a full decode pass per video, which is the
    expensive part of this stage. If you have an AMD GPU (radeonsi
    driver — check with `vainfo` that it lists H.264/HEVC VAAPI decode
    entrypoints), `--hwaccel vaapi` offloads decode to it instead of the
    CPU. The `select`
    scene-detection filter itself still runs in software (it needs raw
    frame data), so the vaapi path decodes on the GPU then downloads frames
    back to system memory (`hwdownload`) before filtering — still a real
    win since decode, not filtering, is the bottleneck. Default is `none`
    (pure software decode) since it works everywhere without /dev/dri
    passthrough; opt into vaapi explicitly and validate with --limit first,
    same as every other tuning knob in this stage. With vaapi, multiple
    --workers processes share the same physical decode hardware (one
    /dev/dri/renderD128 device), so worker count should be tuned against
    GPU throughput, not just CPU core count.

DOWNSCALE-BEFORE-EXTRACT SAFETY NET (`--extract-max-dim`, default 1920):
    Real-world experience from outside this project (4K+/HEVC/3D files
    causing OOM under full-resolution decode elsewhere) prompted adding
    this — this library's own files are all at or below 1080p except one
    real 3840x2160 outlier, so the default is a deliberate no-op for
    everything on record, existing purely as a safety net for whatever
    shows up later. A genuine no-op, not just a negligible one: when
    01_inventory.py's own probed width/height show a video already fits
    (the overwhelmingly common case here), the scale filter is skipped
    from the ffmpeg command entirely rather than invoked and trusted to
    decide there's nothing to do — confirmed live that even a same-
    dimensions scale call still routes the frame through libswscale and
    isn't byte-identical to never calling it (a few bits of difference
    in the derived crop/flip variants, harmless relative to
    --hash-threshold's default of 8, but avoidable, so it's avoided).
    Applied *after* scene selection on videos that do need it (only the
    few kept frames are scaled, not every decoded frame) — scaling
    before selection was tried first and measured live to make a full
    run ~6x *slower*, not faster (every frame has to be evaluated by
    `select` regardless of whether it survives, so scaling beforehand
    resizes the whole video's worth of frames just to throw most of
    them away immediately after). Verified live on the one real 4K video on
    record: downscaling to 1280px cut that video's own fingerprint pass
    peak memory from 1159MB to 1030MB under `--hwaccel none` (real but
    modest — ffmpeg's own decode buffers are still native-resolution
    regardless of any filter downstream of decode) and cut runtime ~13%
    under `--hwaccel vaapi` (a genuine win there, not just modest:
    `scale_vaapi` runs *before* `hwdownload`, shrinking an already-
    mandatory per-frame GPU-to-host transfer rather than adding a new
    cost). Verified accuracy-preserving on the same video: resulting
    pHash values differed by Hamming distance 0-2 from the unscaled
    version, far under `--hash-threshold`'s default of 8. Set to 0 to
    disable entirely and always use full source resolution.

LIVE-ADJUSTABLE WORKER COUNT (web-UI-triggered runs only, i.e. --run-id set):
    --workers sets the *starting* concurrency (default 4, unchanged). While
    running under the web UI, the scan panel can raise or lower it on the
    fly — e.g. to claw back CPU/GPU headroom for something else, or push
    harder once you've confirmed the system has room — via
    `/api/scan/workers`, which just writes `scan_runs.target_workers`.
    This process polls that column roughly every 2s and adjusts how many
    fingerprinting tasks it keeps in flight to match, with one hard rule:
    **an already-running worker is never killed to shrink concurrency.**
    Lowering the target just stops backfilling new work into the pool
    until in-flight count drains down to it naturally; raising it
    immediately submits more. The underlying `ProcessPoolExecutor` is
    constructed with a ceiling of `max(--workers, cpu_count)` so it can
    always lazily spawn enough processes to satisfy a later increase —
    those processes simply sit idle (not killed) while concurrency is
    below their count, ready for reuse the moment it's raised again.
    Adjustment is clamped to [2, cpu_count] at write time (the API) and
    re-clamped at read time here (defense in depth — a stale/bad DB value
    should never starve the loop down to 0 and hang). Without --run-id
    (plain CLI usage), there's no channel to poll from, so --workers stays
    exactly what was passed on the command line, same as before this
    feature existed.

Usage:
    python3 src/02_fingerprint.py --db data/library.db [--scene-threshold 0.3]
                                   [--top-margin 0.12] [--bottom-margin 0.12]
                                   [--side-margin 0.06] [--max-scenes 60]
                                   [--workers 4] [--hwaccel none|vaapi]
                                   [--hwaccel-device /dev/dri/renderD128]
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait

sys.path.insert(0, str(Path(__file__).parent))
from db import connect, init_db, update_scan_run
from procutil import run_with_hard_timeout

try:
    import imagehash
    from PIL import Image
    HAVE_IMAGEHASH = True
except ImportError:
    HAVE_IMAGEHASH = False

from PIL import Image as PILImage
import phash as fallback_phash


def compute_phash(img: "PILImage.Image") -> str:
    if HAVE_IMAGEHASH:
        return str(imagehash.phash(img))
    return fallback_phash.phash(img)


def safe_region_crop(img: "PILImage.Image", top: float, bottom: float, side: float) -> "PILImage.Image":
    """Crop out the overlay-prone margins, return the safe central region."""
    w, h = img.size
    left = int(w * side)
    right = int(w * (1 - side))
    top_px = int(h * top)
    bottom_px = int(h * (1 - bottom))
    return img.crop((left, top_px, right, bottom_px))


def center_crop(img: "PILImage.Image", fraction: float = 0.8) -> "PILImage.Image":
    """Further center-crop (simulates/matches a zoomed-in preview variant)."""
    w, h = img.size
    nw, nh = int(w * fraction), int(h * fraction)
    left = (w - nw) // 2
    top = (h - nh) // 2
    return img.crop((left, top, left + nw, top + nh))


def scene_extraction_timeout(duration_sec: float | None) -> float:
    """Floor of 300s for short clips, scales up to 1x the video's own
    duration after that, capped at 3600s so a single pathological file
    can't hold a worker for more than an hour. A flat timeout isn't
    enough for every video in a real library — confirmed live, two
    ~45-minute 720p files genuinely exceeded a flat 300s under software
    decode (no hwaccel)."""
    return max(300, min(duration_sec or 0, 3600))


def extract_scene_frames(video_path: Path, out_dir: Path, scene_threshold: float, max_scenes: int,
                          hwaccel: str = "none", hwaccel_device: str = "/dev/dri/renderD128",
                          log_path: Path | None = None, duration_sec: float | None = None,
                          scene_detection_mode: str = "filter", extract_max_dim: int = 0) -> list[float]:
    """
    Use ffmpeg scene-detection to dump one JPEG per detected scene change,
    plus a forced frame at t=0. Returns list of timestamps (seconds) in
    the same order frames were written, parsed from the showinfo log.
    See scene_extraction_timeout() for why the timeout scales with
    duration instead of being a flat value.

    scene_detection_mode:
      - "filter" (default, unchanged from before this parameter existed):
        ffmpeg's select='gt(scene,threshold)' — a genuine visual
        scene-cut signal, computed by comparing successive *decoded*
        frames, so every frame still has to be decoded even though only
        a few are kept.
      - "keyframe" (EXPERIMENTAL — see --scene-detection-mode's own help
        text and BENCHMARKS.md before trusting this for anything but a
        speed comparison): uses -skip_frame nokey to decode *only* the
        source's own encoder-placed keyframes, skipping every other
        frame's decode entirely. Much cheaper, but keyframe placement is
        an encoder/GOP-interval property, not a content-aware scene-cut
        signal — it only loosely tracks real scene cuts (some encoders
        force one at a detected cut, but also place them at a fixed
        interval regardless of content) and was never validated against
        this project's accumulated false-positive-prevention history
        (--min-matched-scenes/--min-scene-duration/--min-match-spread
        etc. — see 03_match.py's module docstring) — those guards were
        all tuned against the "filter" signal's actual cut density and
        timing, not this one.

    extract_max_dim: if > 0, downscale so neither dimension exceeds this
      many pixels (aspect ratio preserved) before the JPEG is written —
      0 here disables it (full source resolution); the CLI default is
      1920, not 0 (see --extract-max-dim's own help text for why: a
      no-op for this library's actual content, a safety net for any
      4K+ outlier). pHash only ever looks at a small thumbnail internally
      regardless of source size, so this targets JPEG encode/decode and
      PIL crop overhead, not the dominant per-video decode-pass cost —
      see BENCHMARKS.md for what was actually measured before trusting
      this to matter much in practice.
    """
    timeout = scene_extraction_timeout(duration_sec)
    out_pattern = str(out_dir / "scene_%04d.jpg")

    downscale_chain = f"scale=w={extract_max_dim}:h={extract_max_dim}:force_original_aspect_ratio=decrease" \
        if extract_max_dim else None

    filters = []
    if hwaccel == "vaapi":
        if extract_max_dim:
            # Downscale while still a VAAPI surface, before hwdownload.
            # Unlike the software path below, this isn't "scale every
            # frame just to throw most away" waste: hwdownload itself
            # already has to run on *every* decoded frame regardless of
            # scene_detection_mode (select needs raw pixel data to
            # evaluate every frame, not just the ones that survive), so
            # shrinking the frame first reduces that already-mandatory
            # per-frame transfer's data volume instead of adding a new
            # per-frame cost that wasn't already there.
            filters.append(f"scale_vaapi=w={extract_max_dim}:h={extract_max_dim}:force_original_aspect_ratio=decrease")
        filters += ["hwdownload", "format=nv12"]

    skip_frame_args = []
    if scene_detection_mode == "keyframe":
        # -skip_frame nokey already discards every non-keyframe at decode
        # time, before any filter runs — every frame that reaches this
        # chain is being kept, so there's no "scale frames that get
        # discarded anyway" risk here; ordering relative to showinfo
        # doesn't matter for cost or correctness.
        skip_frame_args = ["-skip_frame", "nokey"]
        if hwaccel != "vaapi" and downscale_chain:
            filters.append(downscale_chain)
        filters.append("showinfo")
    else:
        # select scene changes above threshold OR the very first frame;
        # showinfo prints pts_time for each frame that passes, which we
        # parse to recover real timestamps (frame numbering alone isn't
        # enough).
        filters.append(f"select='gt(scene\\,{scene_threshold})+eq(n\\,0)'")
        if hwaccel != "vaapi" and downscale_chain:
            # Downscale *after* select, not before: select has to
            # evaluate every decoded frame regardless of whether it
            # survives, so scaling beforehand resizes every frame in the
            # whole video just to throw most of them away immediately
            # after. Measured live: this made a full 80-video run ~6x
            # *slower* (46.2min vs the 7.8min unscaled baseline), not
            # faster. Only the handful of frames that
            # actually pass select should ever pay the scale cost.
            filters.append(downscale_chain)
        filters.append("showinfo")
    vf = ",".join(filters)

    hwaccel_args = ["-hwaccel", "vaapi", "-hwaccel_device", hwaccel_device, "-hwaccel_output_format", "vaapi"] \
        if hwaccel == "vaapi" else []
    cmd = [
        "ffmpeg", "-y", "-loglevel", "info", *skip_frame_args, *hwaccel_args, "-i", str(video_path),
        "-vf", vf, "-fps_mode", "vfr", "-frame_pts", "0",
        "-q:v", "3",
        out_pattern,
    ]
    proc = run_with_hard_timeout(cmd, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  log_path=log_path, log_label=f"ffmpeg(scenes): {video_path.name}")
    if proc is None:
        print(f"  [TIMEOUT extracting scenes] {video_path}", file=sys.stderr)
        return []

    timestamps = []
    for line in proc.stderr.splitlines():
        if "pts_time:" in line:
            try:
                ts = float(line.split("pts_time:")[1].split()[0])
                timestamps.append(ts)
            except (IndexError, ValueError):
                continue

    if max_scenes and len(timestamps) > max_scenes:
        # Evenly subsample to cap cost on very long / very cutty sources
        step = len(timestamps) / max_scenes
        keep_idx = {int(i * step) for i in range(max_scenes)}
        all_frames = sorted(out_dir.glob("scene_*.jpg"))
        for i, f in enumerate(all_frames):
            if i not in keep_idx:
                f.unlink(missing_ok=True)
        timestamps = [t for i, t in enumerate(timestamps) if i in keep_idx]

    return timestamps


def fingerprint_video(video_id: int, path: str, top: float, bottom: float, side: float, scene_threshold: float,
                       max_scenes: int, hwaccel: str = "none", hwaccel_device: str = "/dev/dri/renderD128",
                       blank_std_threshold: float = 4.0, db_path: str | None = None, run_id: int | None = None,
                       log_path: str | None = None, duration_sec: float | None = None,
                       scene_detection_mode: str = "filter", extract_max_dim: int = 0,
                       width: int | None = None, height: int | None = None,
                       force_vaapi_above_max_dim: bool = True) -> dict:
    """Runs in a worker process. No DB *connection* crosses the process
    boundary (sqlite3 connections aren't picklable) — db_path/run_id let
    this worker open its own short-lived connection just to report "now
    processing X" before starting the expensive decode pass, since
    otherwise stage_done only advances once per *completed* video and the
    UI shows nothing changing for however long that takes (each video is
    a full decode pass — minutes, not seconds, for feature-length files).
    """
    video_path = Path(path)
    result = {"video_id": video_id, "scenes": [], "audio": None, "error": None, "blank_skipped": 0}
    update_scan_run(db_path, run_id, message=f"processing: {video_path.name}")

    # Skip the downscale filter entirely when 01_inventory.py's own probed
    # width/height already show it can't matter — not just relying on
    # ffmpeg's scale filter to decide "nothing to do" at runtime. Confirmed
    # live that even a no-op-dimensions scale call isn't byte-identical to
    # not calling it at all (still routes the frame through libswscale,
    # producing single-bit-level pHash differences in the cropped/flipped
    # variants — harmless relative to --hash-threshold's default of 8, but
    # avoidable, and "known not needed" should mean "doesn't run" for
    # already-fingerprinted content, not "runs but the output should be
    # close enough").
    effective_max_dim = extract_max_dim
    if extract_max_dim and width and height and max(width, height) <= extract_max_dim:
        effective_max_dim = 0

    # Force VAAPI for this one video, regardless of the run's overall
    # --hwaccel setting, when it's actually oversized enough to need
    # downscaling — see --force-vaapi-above-max-dim's own help text.
    # VAAPI *alone* measured slower than CPU even on the one real 4K
    # video on record (281.4s vs 206.5s) — this override only pays off
    # combined with the downscale above, since scale_vaapi shrinks the
    # frame *before* hwdownload, cutting an otherwise-huge per-frame
    # GPU-to-host transfer. Confirmed live: VAAPI+downscale on that same
    # video was the fastest and lowest-memory combination of all four
    # tested (135.3s/838MB vs CPU+downscale's 207.0s/1030MB) — clearly
    # not true for ordinary sub-threshold content, where plain CPU
    # decode already wins outright (see BENCHMARKS.md's per-video breakdown).
    effective_hwaccel = hwaccel
    if (force_vaapi_above_max_dim and hwaccel != "vaapi" and extract_max_dim
            and width and height and max(width, height) > extract_max_dim
            and Path(hwaccel_device).exists()):
        effective_hwaccel = "vaapi"

    with tempfile.TemporaryDirectory(prefix="vfp_") as tmpdir:
        tmp = Path(tmpdir)
        try:
            timestamps = extract_scene_frames(video_path, tmp, scene_threshold, max_scenes, effective_hwaccel,
                                               hwaccel_device, log_path, duration_sec, scene_detection_mode,
                                               effective_max_dim)
        except Exception as e:
            result["error"] = f"scene extraction failed: {e}"
            return result

        frame_files = sorted(tmp.glob("scene_*.jpg"))
        for idx, (frame_file, ts) in enumerate(zip(frame_files, timestamps)):
            try:
                img = PILImage.open(frame_file)
                safe = safe_region_crop(img, top, bottom, side)
                if fallback_phash.is_blank(safe, blank_std_threshold):
                    # Near-uniform frame (fade to/from black, blank intro
                    # card) — see is_blank()'s docstring: these hash to a
                    # degenerate all-zero pHash regardless of backend, so
                    # skip rather than store a frame that can't actually
                    # discriminate anything.
                    result["blank_skipped"] += 1
                    continue
                h_normal = compute_phash(safe)
                h_cropped = compute_phash(center_crop(safe))
                h_flipped = compute_phash(safe.transpose(PILImage.FLIP_LEFT_RIGHT))
                c_sig = fallback_phash.color_signature(safe)
                result["scenes"].append({
                    "scene_index": idx,
                    "timestamp_sec": ts,
                    "phash": h_normal,
                    "phash_cropped": h_cropped,
                    "phash_flipped": h_flipped,
                    "color_sig": c_sig,
                })
            except Exception as e:
                print(f"  [FRAME HASH FAIL] {video_path} @ {ts}: {e}", file=sys.stderr)
                continue

    # Audio fingerprint via fpcalc (chromaprint), independent of frame extraction
    fpcalc_path = shutil.which("fpcalc")
    if fpcalc_path:
        try:
            proc = run_with_hard_timeout(
                [fpcalc_path, "-raw", "-length", "120", str(video_path)],
                timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                log_path=log_path, log_label=f"fpcalc: {video_path.name}",
            )
            if proc is not None:
                fp_line = next((l for l in proc.stdout.splitlines() if l.startswith("FINGERPRINT=")), None)
                dur_line = next((l for l in proc.stdout.splitlines() if l.startswith("DURATION=")), None)
                if fp_line:
                    result["audio"] = {
                        "fingerprint": fp_line.split("=", 1)[1],
                        "duration_sec": float(dur_line.split("=", 1)[1]) if dur_line else None,
                        "fp_ok": 1,
                    }
        except Exception as e:
            print(f"  [AUDIO FP FAIL] {video_path}: {e}", file=sys.stderr)

    if result["audio"] is None:
        result["audio"] = {"fingerprint": None, "duration_sec": None, "fp_ok": 0}

    return result


def write_fingerprint_result(conn, vid: int, res: dict) -> None:
    """Writes one completed fingerprint_video() result for video `vid` —
    scenes, audio fingerprint, and the fingerprinted_at timestamp.
    Doesn't commit (callers control transaction boundaries — see the
    main loop's comment on committing after every video).

    The DELETE before the scene inserts matters: a re-fingerprint (file
    replaced/re-encoded at the same path — see 01_inventory.py's
    needs_reprobe) can produce *fewer* scenes than the previous version
    did. The insert is keyed on UNIQUE(video_id, scene_index), so without
    this delete, old scene rows at indices beyond the new count would
    silently survive and get matched against alongside the new content's
    hashes — a real bug, not just leftover clutter. Cheap to always do
    (most videos are being fingerprinted for the first time anyway,
    where this is just a no-op delete)."""
    conn.execute("DELETE FROM scenes WHERE video_id = ?", (vid,))
    for s in res["scenes"]:
        conn.execute(
            """INSERT INTO scenes
               (video_id, scene_index, timestamp_sec, phash, phash_cropped, phash_flipped, color_sig)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (vid, s["scene_index"], s["timestamp_sec"], s["phash"], s["phash_cropped"], s["phash_flipped"], s["color_sig"]),
        )

    a = res["audio"]
    conn.execute(
        """INSERT OR REPLACE INTO audio_fp (video_id, duration_sec, fingerprint, fp_ok)
           VALUES (?, ?, ?, ?)""",
        (vid, a["duration_sec"], a["fingerprint"], a["fp_ok"]),
    )

    conn.execute("UPDATE videos SET fingerprinted_at = ? WHERE id = ?", (time.time(), vid))


def _read_target_workers(db_path: str, run_id: int | None, current: int, hard_max: int) -> int:
    """Live-poll scan_runs.target_workers (see module docstring's
    LIVE-ADJUSTABLE WORKER COUNT section). Falls back to `current`
    unchanged on any read hiccup (no run_id, no row yet, column unset, a
    busy/locked DB) — a polling glitch must never suddenly snap the
    worker count back to some default value out from under an active
    run; the only thing that should ever change `current` is a real,
    deliberate value written via /api/scan/workers. Clamps whatever it
    does read to [2, hard_max] as defense in depth, independent of the
    API endpoint's own validation — a stray bad value here must never be
    able to starve the scheduling loop down to 0 in-flight and hang."""
    if run_id is None:
        return current
    try:
        with connect(db_path) as conn:
            row = conn.execute("SELECT target_workers FROM scan_runs WHERE id = ?", (run_id,)).fetchone()
    except Exception:
        return current
    if row is None or row["target_workers"] is None:
        return current
    return max(2, min(hard_max, row["target_workers"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "data/library.db"), help="SQLite DB path (env: DB_PATH)")
    ap.add_argument("--scene-threshold", type=float, default=0.3,
                     help="ffmpeg scene-change sensitivity (lower = more scenes detected)")
    ap.add_argument("--top-margin", type=float, default=0.12, help="fraction of height to exclude from top (scrollers/tickers)")
    ap.add_argument("--bottom-margin", type=float, default=0.12, help="fraction of height to exclude from bottom (info bars)")
    ap.add_argument("--side-margin", type=float, default=0.06, help="fraction of width to exclude from each side (corner logos)")
    ap.add_argument("--max-scenes", type=int, default=60, help="cap scenes per video to bound cost")
    ap.add_argument("--blank-std-threshold", type=float, default=4.0,
                     help="skip near-uniform frames (grayscale std-dev below this, 0-255 scale) — "
                          "blank/fade-to-black frames hash to a degenerate, indiscriminate pHash (see phash.is_blank)")
    ap.add_argument("--workers", type=int, default=4,
                     help="starting concurrency (default 4). Under the web UI (--run-id set), this "
                          "can be raised/lowered live via the scan panel without restarting — see "
                          "module docstring's LIVE-ADJUSTABLE WORKER COUNT section")
    ap.add_argument("--hwaccel", choices=["none", "vaapi"], default="none",
                     help="hardware-accelerated decode for scene extraction (default: software)")
    ap.add_argument("--hwaccel-device", default="/dev/dri/renderD128",
                     help="VAAPI render device path (default: /dev/dri/renderD128)")
    ap.add_argument("--scene-detection-mode", choices=["filter", "keyframe"], default="filter",
                     help="EXPERIMENTAL, default 'filter' (unchanged): ffmpeg's select='gt(scene,...)', a "
                          "genuine visual scene-cut signal. 'keyframe' uses -skip_frame nokey to decode only "
                          "the source's own encoder-placed keyframes — much cheaper (skips decoding every "
                          "other frame entirely) but keyframe placement is a GOP-interval/encoder property, "
                          "not a content-aware cut signal, and was never validated against this project's "
                          "false-positive-prevention guards in 03_match.py (those were tuned against the "
                          "'filter' signal's actual cut density/timing) — see module docstring and "
                          "BENCHMARKS.md before trusting this for anything but a speed comparison")
    ap.add_argument("--extract-max-dim", type=int, default=1920,
                     help="downscale extracted frames so neither dimension exceeds this many pixels "
                          "(aspect ratio preserved) before the JPEG is written, applied *after* scene "
                          "selection (only the few kept frames are scaled, not every decoded frame — "
                          "scaling before selection was measured live to make a run ~6x slower, not "
                          "faster). 0 disables this entirely (full source resolution, pre-this-flag "
                          "behavior). Default 1920 is a no-op for this library's actual content (everything "
                          "on record is at or below 1080p except one 4K outlier) — it exists specifically "
                          "as a safety net for any 4K+/HEVC/3D file that does show up: measured live on the "
                          "one real 4K (3840x2160) video on record, downscaling to 1280 cut peak memory for "
                          "that single video's fingerprint pass from 1159MB to 1030MB under --hwaccel none "
                          "(modest — ffmpeg's own decode buffers are still native-resolution regardless of "
                          "any filter downstream of decode) and cut runtime ~13%% under --hwaccel vaapi "
                          "(real, not just modest — scale_vaapi runs *before* hwdownload, shrinking an "
                          "already-mandatory per-frame GPU-to-host transfer, not adding a new cost). "
                          "Verified accuracy-preserving on that same video: resulting pHash values differed "
                          "by Hamming distance 0-2 from the unscaled version, far under --hash-threshold's "
                          "default of 8 — see BENCHMARKS.md for the full writeup before changing this default "
                          "again, especially if you ever want to lower it below 1080p territory")
    ap.add_argument("--force-vaapi-above-max-dim", action=argparse.BooleanOptionalAction, default=True,
                     help="force --hwaccel vaapi for any single video whose known width/height (from "
                          "01_inventory.py) exceeds --extract-max-dim, regardless of this run's own "
                          "--hwaccel setting — default on. VAAPI *alone* measured slower than CPU decode "
                          "across this entire library, including the one real 4K video on record (281.4s "
                          "vs CPU's 206.5s) — see BENCHMARKS.md for the full "
                          "per-video breakdown — so --hwaccel itself still defaults to 'none' and the web "
                          "UI's GPU-decode checkbox now defaults unticked. But VAAPI *combined* with the "
                          "downscale above is a different story specifically for oversized video: "
                          "scale_vaapi shrinks the frame before hwdownload, cutting a transfer whose cost "
                          "scales with the *original* (huge) frame size — measured live on that same 4K "
                          "video as the fastest and lowest-memory of all four combinations tried (135.3s/"
                          "838MB, vs CPU+downscale's 207.0s/1030MB). Requires --hwaccel-device to actually "
                          "exist — silently falls back to whatever --hwaccel already says otherwise")
    ap.add_argument("--limit", type=int, default=None, help="process at most N videos (testing)")
    ap.add_argument("--run-id", type=int, default=None,
                     help="internal: scan_runs row to report progress to (set by the web UI's scan orchestrator)")
    ap.add_argument("--debug-log", default=None,
                     help="append one entry per ffmpeg/fpcalc call (filename, duration, exit status, output) to this file")
    args = ap.parse_args()

    if args.workers < 1:
        ap.error("--workers must be >= 1")

    if not HAVE_IMAGEHASH:
        print("[info] 'imagehash' package not found, using vendored fallback in phash.py", file=sys.stderr)
    if shutil.which("fpcalc") is None:
        print("[info] 'fpcalc' (chromaprint) not found on PATH — audio fingerprints will be skipped. "
              "Install via 'apt install chromaprint' / 'brew install chromaprint' for audio scoring.", file=sys.stderr)

    init_db(args.db)

    with connect(args.db) as conn:
        rows = conn.execute(
            "SELECT id, path, duration_sec, width, height FROM videos WHERE fingerprinted_at IS NULL"
        ).fetchall()
    todo = [(r["id"], r["path"], r["duration_sec"], r["width"], r["height"]) for r in rows]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(todo)} videos to fingerprint (workers={args.workers})")
    update_scan_run(args.db, args.run_id, stage="fingerprint", stage_total=len(todo), stage_done=0,
                     stage_started_at=time.time(), message=f"{len(todo)} videos to fingerprint",
                     target_workers=args.workers, updated_at=time.time())
    if not todo:
        update_scan_run(args.db, args.run_id, stage_done=0, message="nothing to fingerprint", updated_at=time.time())
        print("Nothing to do. Run 01_inventory.py first, or all videos already fingerprinted.")
        return

    done = 0
    processed = 0
    blank_skipped_total = 0
    t0 = time.time()

    # The pool's own ceiling is the most --workers could ever be raised to
    # live (see module docstring's LIVE-ADJUSTABLE WORKER COUNT section) —
    # always at least the starting --workers value, so an explicit CLI
    # request is never capped below what was asked for. Processes beyond
    # the *current* target are spawned lazily by the pool only once enough
    # tasks are actually submitted concurrently to need them, and they sit
    # idle (never killed) if the target is later lowered again.
    hard_max = max(args.workers, os.cpu_count() or 4)
    current_workers = args.workers
    last_worker_poll = 0.0

    pending = deque(todo)
    in_flight: set = set()

    with ProcessPoolExecutor(max_workers=hard_max) as pool, connect(args.db) as conn:
        while pending or in_flight:
            now = time.time()
            if now - last_worker_poll >= 2.0:
                current_workers = _read_target_workers(args.db, args.run_id, current_workers, hard_max)
                last_worker_poll = now

            while pending and len(in_flight) < current_workers:
                vid, path, duration_sec, width, height = pending.popleft()
                fut = pool.submit(
                    fingerprint_video, vid, path,
                    args.top_margin, args.bottom_margin, args.side_margin,
                    args.scene_threshold, args.max_scenes,
                    args.hwaccel, args.hwaccel_device, args.blank_std_threshold,
                    args.db, args.run_id, args.debug_log, duration_sec,
                    args.scene_detection_mode, args.extract_max_dim, width, height,
                    args.force_vaapi_above_max_dim,
                )
                in_flight.add(fut)

            if not in_flight:
                # Only reachable if current_workers was polled down before
                # anything was ever submitted — shouldn't happen given the
                # [2, hard_max] clamp in _read_target_workers, but avoid a
                # busy-spin on an empty wait() set just in case.
                time.sleep(0.5)
                continue

            finished, _ = wait(in_flight, timeout=2.0, return_when=FIRST_COMPLETED)
            for fut in finished:
                in_flight.discard(fut)
                res = fut.result()
                vid = res["video_id"]
                processed += 1
                if res["error"]:
                    print(f"  [ERROR] video_id={vid}: {res['error']}", file=sys.stderr)
                    # Deliberately not touching `message` here: a worker that
                    # just started the *next* video has likely already
                    # written "processing: <name>" there, and overwriting it
                    # with a numeric-only string is a real race that leaves
                    # the UI showing no filename for however long that next
                    # video takes (reported live — see CHANGELOG). stage_done
                    # alone is enough; it already renders as "N / total".
                    update_scan_run(args.db, args.run_id, stage_done=processed, updated_at=time.time())
                    continue

                write_fingerprint_result(conn, vid, res)

                # Commit after every video, not just periodically — this
                # connection's transaction would otherwise stay open
                # (holding the write lock) across many completions,
                # starving update_scan_run()'s own short-lived
                # connections (including the ones worker *processes*
                # call to report "processing: X") past their
                # busy_timeout. See 01_inventory.py for the same fix and
                # db.py's update_scan_run docstring for how this was found.
                conn.commit()

                blank_skipped_total += res.get("blank_skipped", 0)
                done += 1
                # See the error branch above for why `message` is left alone here.
                update_scan_run(args.db, args.run_id, stage_done=processed, updated_at=time.time())
                if done % 50 == 0:
                    elapsed = time.time() - t0
                    rate = done / elapsed
                    eta = (len(todo) - done) / rate if rate > 0 else 0
                    print(f"  ...{done}/{len(todo)} fingerprinted ({rate:.2f}/s, ETA {eta/60:.1f}m)")

    update_scan_run(args.db, args.run_id, stage_done=len(todo), message=f"done ({done}/{len(todo)} ok)",
                     updated_at=time.time())
    print(f"\nDone in {(time.time()-t0)/60:.1f}m. {blank_skipped_total} near-blank frame(s) skipped "
          f"(see --blank-std-threshold). Next: run 03_match.py to score preview/full candidates.")


if __name__ == "__main__":
    main()
