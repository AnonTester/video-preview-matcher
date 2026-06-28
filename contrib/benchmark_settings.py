#!/usr/bin/env python3
"""
contrib/benchmark_settings.py — find good --workers / --hwaccel /
--executor / --pool-generation-chunks settings for *this* machine
against *your* library, instead of trusting numbers measured on
someone else's hardware (see ../BENCHMARKS.md — every number there is
from one specific machine, explicitly "a useful reference point, not a
guarantee for your hardware").

Never touches your real database — every test runs against a throwaway
copy under --tmp-dir (default: alongside --db), deleted when done.
Needs a populated data/library.db:
  - The fingerprinting benchmark needs 01_inventory.py to have already
    run (videos.duration_sec/width/height populated) — it doesn't need
    anything fingerprinted yet.
  - The matching benchmark needs at least some videos already
    fingerprinted (videos.fingerprinted_at IS NOT NULL) — it's
    read-only against scenes/audio_fp, no fingerprinting happens here.
Either benchmark is skipped, with a clear message, if there isn't
enough data for it yet — never errors out just because you haven't run
the whole pipeline first.

Usage:
    python3 contrib/benchmark_settings.py --db data/library.db
    # Inside Docker, to also exercise VAAPI decode (needs /dev/dri
    # passthrough, only available there — see README's Docker
    # deployment section):
    docker compose run --rm app python3 contrib/benchmark_settings.py --db /data/library.db

The default worker-count sweep is deliberately small (1, half, all
cores - 1) so a first run finishes in a reasonable time — pass
--fp-workers/--match-workers explicitly for a more thorough sweep, e.g.
--fp-workers 1 4 8 12 15 16 (this is exactly what was used to produce
this project's own README benchmarks).

Run from the project root (it locates src/ relative to this file's own
path, the same pattern the test suite uses for files that can't be
imported normally).
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from db import connect  # noqa: E402


def default_worker_counts():
    """A small, fast-by-default spread from 1 to all cores - 1 — generalizes
    the specific 1/4/8/12/cpu-1/cpu sweep used to produce this
    project's own README numbers to whatever hardware this runs on,
    without forcing that full (much slower) sweep on every invocation."""
    cpu = os.cpu_count() or 4
    return sorted({1, max(1, cpu // 2), max(1, cpu - 1)})


def stratified_sample(rows, n, key):
    """n rows spread evenly across key's full sorted range, rather than
    a pure random sample — so even a small sample covers short/long or
    low/high-resolution files instead of risking an unlucky cluster (the
    same reasoning behind how this project's own benchmark sample was
    picked by hand)."""
    rows = sorted(rows, key=key)
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def run_timed(cmd, cwd, track_memory=False):
    """Runs cmd to completion. Returns (elapsed_sec, peak_rss_mb or
    None, returncode, combined stdout/stderr). peak_rss_mb sums the
    whole process tree's RSS (the subprocess plus any worker processes
    it spawns), sampled every 0.5s — the AVAILABLE/RSS distinction
    doesn't matter here the way it does for the live --min-available-
    ram-percent safety check in 03_match.py itself; this is measuring
    *this process tree's own* footprint, the same thing `docker stats`
    measured for the real benchmark runs this script's defaults are
    modeled on."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak_rss = 0
    t0 = time.time()
    top = psutil.Process(proc.pid) if track_memory else None
    while proc.poll() is None:
        if top is not None:
            try:
                procs = [top, *top.children(recursive=True)]
                peak_rss = max(peak_rss, sum(p.memory_info().rss for p in procs if p.is_running()))
            except psutil.NoSuchProcess:
                pass
        time.sleep(0.5)
    out, _ = proc.communicate()
    elapsed = time.time() - t0
    return elapsed, (peak_rss / (1024 ** 2)) if track_memory else None, proc.returncode, out


def bench_fingerprint(args):
    print("\n=== Fingerprinting benchmark (02_fingerprint.py) ===")
    with connect(args.db) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, duration_sec, width, height FROM videos "
            "WHERE duration_sec IS NOT NULL AND width IS NOT NULL"
        ).fetchall()]
    if len(rows) < args.fp_sample_size:
        print(f"  Skipped: only {len(rows)} probed videos on record, need at least "
              f"{args.fp_sample_size} (run 01_inventory.py first, or pass a smaller "
              f"--fp-sample-size).")
        return []

    sample = stratified_sample(rows, args.fp_sample_size, key=lambda r: r["duration_sec"])
    sample_ids = [r["id"] for r in sample]
    total_dur = sum(r["duration_sec"] for r in sample)
    print(f"  Sample: {len(sample)} videos, {total_dur / 3600:.1f}h of content total, "
          f"spread across the full duration range on record.")

    hwaccels = ["none"]
    if Path(args.hwaccel_device).exists():
        hwaccels.append("vaapi")
    else:
        print(f"  {args.hwaccel_device} not found — skipping VAAPI (only reachable inside "
              f"the deployed container with GPU passthrough; see README's Docker deployment "
              f"section, or pass --hwaccel-device if yours lives elsewhere).")

    workers_list = args.fp_workers or default_worker_counts()
    tmp_db = Path(args.tmp_dir) / "benchmark_settings_fp.db"
    results = []
    for hwaccel in hwaccels:
        for workers in workers_list:
            shutil.copy(args.db, tmp_db)
            with connect(tmp_db) as conn:
                conn.execute(
                    f"UPDATE videos SET fingerprinted_at = NULL WHERE id IN "
                    f"({','.join('?' * len(sample_ids))})", sample_ids)
            cmd = [sys.executable, str(PROJECT_ROOT / "src" / "02_fingerprint.py"),
                   "--db", str(tmp_db), "--hwaccel", hwaccel, "--workers", str(workers),
                   "--limit", str(len(sample_ids))]
            print(f"  --hwaccel {hwaccel:<6} --workers {workers:<3} ...", end=" ", flush=True)
            elapsed, _, rc, out = run_timed(cmd, PROJECT_ROOT)
            if rc != 0:
                tail = out.strip().splitlines()[-1] if out.strip() else f"exit {rc}"
                print(f"FAILED ({tail})")
                continue
            print(f"{elapsed:.1f}s")
            results.append({"hwaccel": hwaccel, "workers": workers, "elapsed_sec": elapsed})
    tmp_db.unlink(missing_ok=True)
    return results


def bench_match(args):
    print("\n=== Matching benchmark (03_match.py) ===")
    with connect(args.db) as conn:
        fp_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM videos WHERE fingerprinted_at IS NOT NULL").fetchall()]
    if len(fp_ids) < args.match_min_videos:
        print(f"  Skipped: only {len(fp_ids)} fingerprinted videos on record, need at least "
              f"{args.match_min_videos} for a meaningful pair count (run 02_fingerprint.py "
              f"first, or pass a smaller --match-min-videos).")
        return []

    capped_note = ""
    if len(fp_ids) > args.match_max_videos:
        capped_note = (f" (capped from {len(fp_ids)} — pass --match-max-videos to raise this; "
                        f"the point is bounding worst-case runtime, not testing the absolute "
                        f"largest pair count your library can produce)")
        with connect(args.db) as conn:
            fp_rows = [dict(r) for r in conn.execute(
                f"SELECT id, duration_sec FROM videos WHERE id IN ({','.join('?' * len(fp_ids))})",
                fp_ids).fetchall()]
        fp_ids = [r["id"] for r in stratified_sample(
            fp_rows, args.match_max_videos, key=lambda r: r["duration_sec"] or 0)]
    print(f"  Using {len(fp_ids)} already-fingerprinted videos{capped_note} — read-only "
          f"against scenes/audio_fp, no fingerprinting happens here.")

    try:
        import loky  # noqa: F401
        have_loky = True
    except ImportError:
        have_loky = False
        print("  loky not installed — only --executor fork will be tested "
              "(pip install loky for the full picture, see requirements.txt).")

    executors = args.executors or (["fork", "loky"] if have_loky else ["fork"])
    workers_list = args.match_workers or default_worker_counts()
    gen_chunks_list = args.pool_generation_chunks if args.pool_generation_chunks is not None else [0, 50]

    tmp_db = Path(args.tmp_dir) / "benchmark_settings_match.db"
    results = []
    for executor in executors:
        for workers in workers_list:
            # --pool-generation-chunks is meaningless at --workers 1 (sequential path,
            # no pool at all) — testing it more than once there would just be the same
            # run repeated under a different label.
            pgc_variants = gen_chunks_list if workers > 1 else gen_chunks_list[:1]
            for pgc in pgc_variants:
                shutil.copy(args.db, tmp_db)
                _trim_to_sample(tmp_db, fp_ids)
                cmd = [sys.executable, str(PROJECT_ROOT / "src" / "03_match.py"),
                       "--db", str(tmp_db), "--executor", executor, "--workers", str(workers)]
                label = f"--executor {executor:<5} --workers {workers:<3}"
                if workers > 1:
                    cmd += ["--pool-generation-chunks", str(pgc)]
                    label += f" --pool-generation-chunks {pgc}"
                print(f"  {label} ...", end=" ", flush=True)
                elapsed, peak_mb, rc, out = run_timed(cmd, PROJECT_ROOT, track_memory=True)
                if rc != 0:
                    tail = out.strip().splitlines()[-1] if out.strip() else f"exit {rc}"
                    print(f"FAILED ({tail})")
                    continue
                print(f"{elapsed:.1f}s, peak {peak_mb:.0f}MB")
                results.append({"executor": executor, "workers": workers,
                                 "pool_generation_chunks": pgc if workers > 1 else None,
                                 "elapsed_sec": elapsed, "peak_rss_mb": peak_mb})
    tmp_db.unlink(missing_ok=True)
    return results


def _trim_to_sample(db_path, keep_ids):
    """Deletes every video NOT in keep_ids from the throwaway copy —
    cascades to scenes/audio_fp/matches via the schema's existing
    ON DELETE CASCADE foreign keys (see db.py), so this is just a plain
    DELETE, not a hand-rolled cleanup of each table."""
    with connect(db_path) as conn:
        conn.execute(f"DELETE FROM videos WHERE id NOT IN ({','.join('?' * len(keep_ids))})", keep_ids)


def print_recommendation(fp_results, match_results):
    print("\n=== Recommended settings for this machine ===")
    if fp_results:
        best = min(fp_results, key=lambda r: r["elapsed_sec"])
        print(f"Fingerprinting: --hwaccel {best['hwaccel']} --workers {best['workers']} "
              f"(fastest on the sample: {best['elapsed_sec']:.1f}s)")
        vaapi_results = [r for r in fp_results if r["hwaccel"] == "vaapi"]
        if vaapi_results and best["hwaccel"] == "none":
            vaapi_best = min(vaapi_results, key=lambda r: r["elapsed_sec"])
            print(f"  (VAAPI's best was {vaapi_best['elapsed_sec']:.1f}s at --workers "
                  f"{vaapi_best['workers']} — slower here. GPU decode is not a given win; "
                  f"this project's own real-hardware benchmark found the same thing.)")
    else:
        print("Fingerprinting: not enough data to benchmark — see message above.")

    if match_results:
        # This project's own stated priority for the matching stage: bounded memory
        # matters more than speed — a slower run that finishes safely beats a fast one
        # that risks taking the whole host down. Recommend accordingly by default.
        loky_results = [r for r in match_results if r["executor"] == "loky"]
        pool = loky_results or match_results
        best = min(pool, key=lambda r: r["peak_rss_mb"])
        flag = f"--executor {best['executor']} --workers {best['workers']}"
        if best["pool_generation_chunks"] is not None:
            flag += f" --pool-generation-chunks {best['pool_generation_chunks']}"
        print(f"Matching (prioritizing bounded memory — see ../TUNING.md for why): {flag} "
              f"(peak {best['peak_rss_mb']:.0f}MB, {best['elapsed_sec']:.1f}s)")
        def _setting(r):
            return (r["executor"], r["workers"], r["pool_generation_chunks"])

        fastest = min(match_results, key=lambda r: r["elapsed_sec"])
        if _setting(fastest) != _setting(best):
            fflag = f"--executor {fastest['executor']} --workers {fastest['workers']}"
            if fastest["pool_generation_chunks"] is not None:
                fflag += f" --pool-generation-chunks {fastest['pool_generation_chunks']}"
            print(f"  (Fastest observed was {fflag}: {fastest['elapsed_sec']:.1f}s, peak "
                  f"{fastest['peak_rss_mb']:.0f}MB — only prefer this if your host has memory "
                  f"to spare and finishing fast matters more than staying bounded.)")
    else:
        print("Matching: not enough data to benchmark — see message above.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "data/library.db"),
                     help="SQLite DB path (env: DB_PATH) — never modified, only copied from")
    ap.add_argument("--tmp-dir", default=None,
                     help="where throwaway DB copies are made (default: same directory as --db)")
    ap.add_argument("--skip-fingerprint", action="store_true", help="skip the 02_fingerprint.py benchmark")
    ap.add_argument("--skip-match", action="store_true", help="skip the 03_match.py benchmark")
    ap.add_argument("--fp-sample-size", type=int, default=40,
                     help="how many probed videos to sample for the fingerprinting benchmark")
    ap.add_argument("--fp-workers", type=int, nargs="+", default=None,
                     help="worker counts to test (default: 1, half, and all cores — pass e.g. "
                          "1 4 8 12 15 16 for a more thorough sweep)")
    ap.add_argument("--hwaccel-device", default="/dev/dri/renderD128",
                     help="VAAPI device to check for/use (skips VAAPI entirely if not present)")
    ap.add_argument("--match-min-videos", type=int, default=200,
                     help="minimum already-fingerprinted videos required to run the matching benchmark")
    ap.add_argument("--match-max-videos", type=int, default=1500,
                     help="caps how many already-fingerprinted videos the matching benchmark uses, "
                          "to bound worst-case runtime on a very large library")
    ap.add_argument("--match-workers", type=int, nargs="+", default=None,
                     help="worker counts to test (default: 1, half, and all cores)")
    ap.add_argument("--executors", nargs="+", choices=["fork", "loky"], default=None,
                     help="default: both, if loky is installed; fork only otherwise")
    ap.add_argument("--pool-generation-chunks", type=int, nargs="+", default=None,
                     help="values to test (default: 0 and 50 — 0 means no forced recycling)")
    args = ap.parse_args()
    args.tmp_dir = args.tmp_dir or str(Path(args.db).resolve().parent)

    if not Path(args.db).exists():
        ap.error(f"{args.db} does not exist — run 01_inventory.py first")

    fp_results = [] if args.skip_fingerprint else bench_fingerprint(args)
    match_results = [] if args.skip_match else bench_match(args)

    print("\n=== All results ===")
    for r in fp_results:
        print(f"  fingerprint  hwaccel={r['hwaccel']:<6} workers={r['workers']:<3} {r['elapsed_sec']:.1f}s")
    for r in match_results:
        pgc = r["pool_generation_chunks"]
        pgc_str = str(pgc) if pgc is not None else "-"
        print(f"  match        executor={r['executor']:<5} workers={r['workers']:<3} "
              f"pool_generation_chunks={pgc_str:<4} {r['elapsed_sec']:.1f}s peak={r['peak_rss_mb']:.0f}MB")

    print_recommendation(fp_results, match_results)


if __name__ == "__main__":
    main()
