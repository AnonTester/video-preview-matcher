"""
01_inventory.py — Walk the library, ffprobe every video, populate `videos` table.

Usage:
    python3 src/01_inventory.py /path/to/library [/path/to/another ...] --db data/library.db

MULTIPLE LIBRARY ROOTS:
    Any number of root directories can be scanned in one run — pass them as
    multiple positional args. If none are given on the command line, roots
    are read from the LIBRARY_PATHS env var (':'-separated absolute paths),
    which is how the Docker deployment configures this (see
    docker-compose.yml) without needing to override the container command —
    e.g. a deployment with multiple separate library directories (full
    versions, previews, temp/staging) can list every root in one go.

This is intentionally cheap and idempotent: re-running only (re-)probes files
that are new or whose mtime/size changed since last probe, so you can re-scan
a library after adding files without redoing 6000 ffprobe calls.

--LIMIT SEMANTICS:
    --limit caps how many files actually get (re-)probed this run, NOT
    how many candidate files get looked at. It's applied AFTER filtering
    out files that are already up to date, not before — otherwise, since
    scan_library() yields files in a stable filesystem order, repeated
    runs with the same --limit would keep re-selecting the exact same
    already-probed files at the front of that order and never reach
    anything past them. With this filter-then-limit order, re-running
    with --limit N repeatedly walks forward through the library N files
    at a time.

Duration is stored because it drives the cheapest possible prefilter in the
matching stage: a preview can't match a full video shorter than itself, and
in practice previews are a small fraction of their candidate's length, so a
duration-ratio bucket cuts the O(n*m) comparison matrix dramatically before
any frame hashing comparison is needed.

MISSING-FILE DETECTION (deleted, or moved somewhere not yet re-discovered):
    Every run compares its filesystem walk against the DB and marks any
    previously-known path that's no longer there as missing
    (`videos.missing_since`, see db.py), and clears that flag the moment
    a path reappears. This is intentionally NOT a delete: this library
    lives on an NFS mount (see CLAUDE.md), and a transient mount hiccup
    mid-scan would make thousands of legitimate files look "deleted" for
    a few seconds — auto-deleting on that signal would cascade away real
    scenes/matches/decisions on a false alarm. Missing rows just get
    hidden from the active review queue (04_serve.py) until a human
    explicitly prunes them via `/api/missing-files/prune` (confirm-gated,
    same pattern as `/api/purge-staging`), or they reappear on their own.

    **Scoped to the roots actually passed this run** — this is the part
    that matters for correctness, not just a nice-to-have: the web UI
    lets a human select a subset of `LIBRARY_PATHS` for a partial scan
    (e.g. just the one directory they added files to). A file living
    under a root that *wasn't* selected this run was never walked, and
    must never be flagged missing just because this particular invocation
    didn't look at it — `reconcile_missing()` only ever marks a path
    missing if it falls under one of `paths` (this run's roots); the
    "recovered" direction needs no such check, since anything found in
    `present_paths` was by definition found under a scanned root.

MOVE DETECTION (size + duration heuristic):
    A renamed/moved file has a new path, so by path-identity alone it's
    indistinguishable from a brand-new file — which would mean a full
    re-fingerprint from scratch, and (until this feature) an orphaned
    dead row left behind at the old path forever. Instead: when a
    genuinely new path is probed (no existing `videos` row for it) and
    its `(size_bytes, duration_sec)` matches *exactly one* currently-
    missing row, that row is remapped to the new path instead of a new
    row being inserted — preserving its `id`, `fingerprinted_at`,
    `scenes`, `matches`, and `decisions` untouched, so a moved file's
    review history and fingerprints survive the move for free.

    This is a heuristic, not a true identity check (no content hash) —
    a deliberate, accepted tradeoff: two genuinely different files
    coincidentally sharing both the exact same byte size and the exact
    same ffprobe-reported duration is rare enough in a real video
    library to be an acceptable risk, and the alternative (hashing whole
    multi-GB files to be sure) would be far more expensive than the
    re-fingerprint it's trying to avoid. If more than one missing row
    matches, this deliberately does NOT guess — it falls back to
    treating the file as brand new, since attaching one file's history
    to the wrong file is worse than redundantly re-fingerprinting an
    actual new file.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import connect, init_db, update_scan_run
from procutil import run_with_hard_timeout

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".mpg", ".mpeg"}


def ffprobe_file(path: Path, log_path: Path | None = None) -> dict | None:
    """Run ffprobe and extract the fields we care about. Returns None on
    failure or timeout — see procutil.py for why a plain subprocess.run
    timeout isn't safe here. log_path: see procutil.py's DEBUG LOGGING —
    records every ffprobe call (filename, duration, exit status, output)
    to a file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height",
        "-of", "json",
        str(path),
    ]
    out = run_with_hard_timeout(cmd, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 log_path=log_path, log_label=f"ffprobe: {path.name}")
    if out is None:
        print(f"  [TIMEOUT] {path}", file=sys.stderr)
        return None

    if out.returncode != 0:
        print(f"  [PROBE FAIL] {path}: {out.stderr.strip()[:200]}", file=sys.stderr)
        return None

    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if video_stream is None:
        return None

    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "duration_sec": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
        "has_audio": 1 if has_audio else 0,
    }


def scan_library(roots: list[Path]):
    """Walk one or more root directories, de-duping files reachable from more
    than one root (e.g. a misconfigured LIBRARY_PATHS listing the same
    directory twice, or one root nested inside another)."""
    seen = set()
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                resolved = p.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield p


def needs_reprobe(conn, path: Path, size: int, mtime: float) -> bool:
    row = conn.execute(
        "SELECT size_bytes, mtime FROM videos WHERE path = ?", (str(path),)
    ).fetchone()
    if row is None:
        return True
    return row["size_bytes"] != size or row["mtime"] != mtime


def select_files_to_probe(conn, all_files: list[Path], limit: int | None) -> tuple[list, int]:
    """Returns ([(path, stat), ...], deferred_count) for files that need
    (re)probing, capped at `limit` if given. See --LIMIT SEMANTICS in the
    module docstring for why the cap is applied here, after filtering out
    already-current files, rather than to the raw candidate list."""
    todo = []
    for path in all_files:
        stat = path.stat()
        if needs_reprobe(conn, path, stat.st_size, stat.st_mtime):
            todo.append((path, stat))
    deferred = 0
    if limit and len(todo) > limit:
        deferred = len(todo) - limit
        todo = todo[:limit]
    return todo, deferred


def _path_under_any_root(path: str, roots: list[str]) -> bool:
    for root in roots:
        root = root.rstrip("/")
        if path == root or path.startswith(root + "/"):
            return True
    return False


def reconcile_missing(conn, roots: list[Path], present_paths: set[str]) -> tuple[int, int]:
    """Compares this run's filesystem walk (`present_paths`, the full
    walk — not the --limit-bounded todo list, since this is cheap and
    shouldn't be gated by the probe budget) against the DB, scoped to
    `roots` — see module docstring's MISSING-FILE DETECTION section for
    why scoping matters (a partial scan must never flag files under an
    unscanned root as missing). Returns (newly_missing, recovered)
    counts."""
    root_strs = [str(r) for r in roots]

    not_missing = conn.execute("SELECT id, path FROM videos WHERE missing_since IS NULL").fetchall()
    newly_missing_ids = [
        r["id"] for r in not_missing
        if r["path"] not in present_paths and _path_under_any_root(r["path"], root_strs)
    ]
    if newly_missing_ids:
        now = time.time()
        conn.executemany(
            "UPDATE videos SET missing_since = ? WHERE id = ?",
            [(now, vid) for vid in newly_missing_ids],
        )

    currently_missing = conn.execute("SELECT id, path FROM videos WHERE missing_since IS NOT NULL").fetchall()
    recovered_ids = [r["id"] for r in currently_missing if r["path"] in present_paths]
    if recovered_ids:
        conn.executemany(
            "UPDATE videos SET missing_since = NULL WHERE id = ?",
            [(vid,) for vid in recovered_ids],
        )

    return len(newly_missing_ids), len(recovered_ids)


def find_move_candidate(conn, size_bytes: int, duration_sec: float) -> int | None:
    """A single currently-missing row with matching (size_bytes,
    duration_sec) is treated as "this is that file, moved" — see module
    docstring's MOVE DETECTION section for the heuristic and its
    tradeoffs. Returns None (caller inserts a fresh row instead) on zero
    or ambiguous (more than one) matches — guessing wrong here would
    attach one file's fingerprints/review history to a different file,
    which is worse than redundantly re-fingerprinting an actual new
    file."""
    rows = conn.execute(
        "SELECT id FROM videos WHERE missing_since IS NOT NULL AND size_bytes = ? AND duration_sec = ?",
        (size_bytes, duration_sec),
    ).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library_paths", nargs="*", type=Path,
                     help="Root directories of the video library (space-separated). "
                          "If omitted, read from the LIBRARY_PATHS env var (':'-separated).")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "data/library.db"), help="SQLite DB path (env: DB_PATH)")
    ap.add_argument("--limit", type=int, default=None, help="probe at most N files (testing)")
    ap.add_argument("--run-id", type=int, default=None,
                     help="internal: scan_runs row to report progress to (set by the web UI's scan orchestrator)")
    ap.add_argument("--debug-log", default=None,
                     help="append one entry per ffprobe call (filename, duration, exit status, output) to this file")
    args = ap.parse_args()

    paths = args.library_paths
    if not paths:
        env_val = os.environ.get("LIBRARY_PATHS", "")
        paths = [Path(p) for p in env_val.split(":") if p.strip()]
    if not paths:
        print("No library paths given on the command line or via the LIBRARY_PATHS env var.", file=sys.stderr)
        sys.exit(1)

    missing = [p for p in paths if not p.is_dir()]
    if missing:
        print(f"Not a directory: {missing[0]}", file=sys.stderr)
        sys.exit(1)

    init_db(args.db)

    all_files = list(scan_library(paths))
    roots_desc = ", ".join(str(p) for p in paths)
    print(f"Found {len(all_files)} candidate video files under {len(paths)} root(s): {roots_desc}")

    present_paths = {str(p) for p in all_files}
    with connect(args.db) as conn:
        n_newly_missing, n_recovered = reconcile_missing(conn, paths, present_paths)
        todo, deferred = select_files_to_probe(conn, all_files, args.limit)

    if n_newly_missing or n_recovered:
        print(f"{n_newly_missing} file(s) newly missing (not found under a scanned root), "
              f"{n_recovered} file(s) recovered (path reappeared).")

    already_current = len(all_files) - len(todo) - deferred
    deferred_msg = f", {deferred} deferred to a future run (--limit)" if deferred else ""
    print(f"{already_current} already up to date, {len(todo)} to (re)probe this run{deferred_msg}.")

    update_scan_run(args.db, args.run_id, stage="inventory", stage_total=len(todo), stage_done=0,
                     stage_started_at=time.time(), message=f"{len(todo)} files to probe", updated_at=time.time())

    new_or_changed = 0
    failed = 0
    moved = 0

    with connect(args.db) as conn:
        for i, (path, stat) in enumerate(todo, 1):
            update_scan_run(args.db, args.run_id, message=f"probing: {path.name}", updated_at=time.time())
            info = ffprobe_file(path, log_path=args.debug_log)
            if info is None:
                failed += 1
            else:
                existing = conn.execute("SELECT id FROM videos WHERE path = ?", (str(path),)).fetchone()
                # A path with no existing row is either a genuinely new
                # file, or a moved/renamed one — see module docstring's
                # MOVE DETECTION section. A path that already has a row
                # is neither: it's the same file we already know about,
                # possibly with changed content (handled below exactly
                # as before this feature — fingerprinted_at reset forces
                # a re-fingerprint).
                move_from = None if existing else find_move_candidate(conn, stat.st_size, info["duration_sec"])

                if move_from is not None:
                    conn.execute(
                        """UPDATE videos SET
                               path=?, filename=?, size_bytes=?, duration_sec=?, width=?, height=?,
                               codec=?, container=?, has_audio=?, mtime=?, probed_at=?, missing_since=NULL
                           WHERE id=?""",
                        (
                            str(path), path.name, stat.st_size, info["duration_sec"],
                            info["width"], info["height"], info["codec"],
                            path.suffix.lower().lstrip("."), info["has_audio"],
                            stat.st_mtime, time.time(), move_from,
                        ),
                    )
                    # Deliberately NOT touching fingerprinted_at — the
                    # whole point of move detection is that this is the
                    # same content, so existing fingerprints/scenes/
                    # matches/decisions stay valid and don't need redoing.
                    moved += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO videos (path, filename, size_bytes, duration_sec, width, height,
                                             codec, container, has_audio, mtime, probed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            size_bytes=excluded.size_bytes, duration_sec=excluded.duration_sec,
                            width=excluded.width, height=excluded.height, codec=excluded.codec,
                            container=excluded.container, has_audio=excluded.has_audio,
                            mtime=excluded.mtime, probed_at=excluded.probed_at,
                            fingerprinted_at=NULL, missing_since=NULL
                        """,
                        (
                            str(path), path.name, stat.st_size, info["duration_sec"],
                            info["width"], info["height"], info["codec"],
                            path.suffix.lower().lstrip("."), info["has_audio"],
                            stat.st_mtime, time.time(),
                        ),
                    )
                    new_or_changed += 1

            # Commit after every file, not just periodically: this
            # connection's transaction would otherwise stay open (holding
            # the write lock) for the whole loop on a small/limited run,
            # starving update_scan_run()'s own short-lived connections
            # past their busy_timeout — which silently freezes the
            # *displayed* progress for the run's whole duration even
            # though the actual probing keeps working fine underneath
            # (a real bug, found by exactly that symptom — see db.py's
            # update_scan_run docstring).
            conn.commit()

            update_scan_run(args.db, args.run_id, stage_done=i,
                             message=f"{i}/{len(todo)} probed", updated_at=time.time())
            if i % 200 == 0:
                print(f"  ...{i}/{len(todo)} processed")

    deferred_summary = f" {deferred} more file(s) deferred to a future run due to --limit." if deferred else ""
    moved_msg = f", {moved} moved" if moved else ""
    update_scan_run(args.db, args.run_id, stage_done=len(todo),
                     message=f"{new_or_changed} new/updated{moved_msg}, {failed} failed{deferred_msg}",
                     updated_at=time.time())
    print(f"\nDone. {new_or_changed} new/updated, {moved} moved (matched by size+duration "
          f"to a missing row, fingerprints preserved), {failed} failed to probe.{deferred_summary}")
    print("Next: run 02_fingerprint.py to extract scene hashes + audio fingerprints.")


if __name__ == "__main__":
    main()
