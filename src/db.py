"""
db.py — SQLite schema and access layer for preview-matcher.

Design notes:
- One row per file in `videos`, with probe metadata + a `role` guess
  (full/preview/unknown) that the user can override. `missing_since` is
  set by `01_inventory.py` when a previously-probed path is no longer
  found under a scanned library root (deleted, or moved somewhere not
  yet re-discovered) — rows are never auto-deleted just because a file
  went missing (an NFS hiccup mid-scan would otherwise look identical to
  "thousands of files deleted," and this codebase doesn't destroy data
  on an inference like that); see `01_inventory.py`'s module docstring
  for detection + move-recovery, and `04_serve.py`'s
  `/api/missing-files`/`/api/missing-files/prune` for the explicit,
  human-confirmed cleanup action.
- `scenes` holds one row per detected scene-cut frame per video, with
  three pHash variants (normal, cropped-center, h-flipped) so matching
  can check all orientations without re-hashing at query time.
- `audio_fp` holds the raw chromaprint fingerprint (as int array / blob)
  per video, nullable — narration-replaced previews may have no usable
  match here, and that's expected, not an error state.
- `matches` is the output of the scoring stage: ranked preview->full
  candidates with separate visual/audio sub-scores, so the UI can show
  *why* something scored the way it did.
- `decisions` records the human review outcome, kept separate from
  `matches` so re-running matching never clobbers a human decision.
- `match_feedback` records "this specific pairing is wrong" at the
  (preview, candidate) granularity — distinct from `decisions`, which
  is preview-level (a preview can have several candidates; dismissing
  one shouldn't affect the others). `matches` is fully recomputed by
  `03_match.py` on every run, so this table is what makes a dismissal
  stick across re-runs/threshold changes.
- `scan_runs` holds one row per scan triggered from the web UI (04_serve.py
  orchestrates 01/02/03 as subprocesses so nothing has to be run by hand).
  One mutable row per run, updated in place as it moves through stages —
  `stage`/`stage_total`/`stage_done` track live progress, written by the
  stage scripts themselves via their `--run-id` flag and update_scan_run()
  below. `params_json` (the selected roots/limit) is kept so a paused,
  interrupted, or failed run can be resumed with the same scope.
  `target_workers` is the one field in this table written from *outside*
  the running stage subprocess (by 04_serve.py's `/api/scan/workers`,
  in response to the scan panel's worker slider) — 02_fingerprint.py
  polls it periodically and adjusts how many fingerprinting tasks it
  keeps in flight, without ever killing an already-running one. See
  02_fingerprint.py's module docstring for the scheduling design.
"""

import sqlite3
import sys
from pathlib import Path
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT UNIQUE NOT NULL,
    filename        TEXT NOT NULL,
    size_bytes      INTEGER,
    duration_sec    REAL,
    width           INTEGER,
    height          INTEGER,
    codec           TEXT,
    container       TEXT,
    has_audio       INTEGER DEFAULT 1,
    mtime           REAL,
    role_guess      TEXT DEFAULT 'unknown',   -- 'full' | 'preview' | 'unknown'
    probed_at       REAL,
    fingerprinted_at REAL,
    missing_since   REAL                       -- set by 01_inventory.py when this path is no
                                                -- longer found under a scanned root; NULL means
                                                -- present (or simply never checked — see
                                                -- 01_inventory.py's root-scoping note). Cleared
                                                -- the moment the path is seen again. Rows are
                                                -- never auto-deleted just because this is set —
                                                -- see /api/missing-files/prune for the explicit,
                                                -- human-confirmed cleanup action.
);

CREATE TABLE IF NOT EXISTS scenes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    scene_index     INTEGER NOT NULL,     -- order within the source video
    timestamp_sec   REAL NOT NULL,
    phash           TEXT NOT NULL,        -- hex string, full safe-region frame
    phash_cropped   TEXT NOT NULL,        -- hex string, center-cropped variant
    phash_flipped   TEXT NOT NULL,        -- hex string, horizontally mirrored
    color_sig       TEXT,                 -- coarse color histogram, color-collision guard
    UNIQUE(video_id, scene_index)
);
CREATE INDEX IF NOT EXISTS idx_scenes_video ON scenes(video_id);

CREATE TABLE IF NOT EXISTS audio_fp (
    video_id        INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    duration_sec    REAL,
    fingerprint     TEXT,                 -- raw fpcalc fingerprint string
    fp_ok           INTEGER DEFAULT 1     -- 0 if no audio track / fpcalc failed
);

CREATE TABLE IF NOT EXISTS matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    preview_id      INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    candidate_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    visual_score    REAL NOT NULL,        -- 0..1, fraction of preview scenes matched
    audio_score     REAL,                 -- 0..1 or NULL if no usable audio fp
    combined_score  REAL NOT NULL,
    scene_matches_json TEXT,              -- list of {preview_ts, candidate_ts, dist, variant}
    computed_at     REAL,
    UNIQUE(preview_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_preview ON matches(preview_id, combined_score DESC);

CREATE TABLE IF NOT EXISTS decisions (
    preview_id      INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,        -- 'approved_delete' | 'rejected' | 'staged' | 'deleted'
    -- ON DELETE SET NULL, not CASCADE: matched_candidate_id is just a
    -- historical reference to which candidate this preview was matched
    -- against. If that candidate video is later pruned (e.g. it went
    -- missing independently), the decision about *this* preview is
    -- still valid and must survive — only the now-dangling reference
    -- should clear. Without this, prune_missing_files()'s bulk DELETE
    -- FROM videos fails outright with a FOREIGN KEY constraint error
    -- the moment any missing video happens to be someone else's
    -- matched_candidate_id — see CLAUDE.md's prune-missing-files
    -- writeup for the real incident this caused (silent no-op in the
    -- UI, since the frontend never checked the response status).
    matched_candidate_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    decided_at      REAL,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS match_feedback (
    preview_id      INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    candidate_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'not_a_match',
    decided_at      REAL,
    PRIMARY KEY (preview_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    params_json     TEXT NOT NULL,        -- {"roots": [...], "limit": int|null}
    status          TEXT NOT NULL,        -- 'running'|'completed'|'failed'|'cancelled'|'paused'|'interrupted'
    stage           TEXT,                 -- 'inventory' | 'fingerprint' | 'match' | null
    stage_total     INTEGER,
    stage_done      INTEGER DEFAULT 0,
    stage_started_at REAL,                -- reset each time `stage` changes; basis for per-stage ETA
                                           -- (a whole-run rate would conflate fast/slow stages)
    message         TEXT,
    started_at      REAL,
    updated_at      REAL,
    finished_at     REAL,
    target_workers  INTEGER,               -- live-adjustable fingerprint worker count, polled by
                                           -- 02_fingerprint.py's own scheduling loop (see its module
                                           -- docstring) — lets the web UI raise/lower concurrency
                                           -- mid-run without restarting the process or killing any
                                           -- in-flight worker.
    resume_baseline_done  INTEGER DEFAULT 0,  -- count already completed in a *prior* interrupted/
                                               -- paused/failed attempt at the *same* stage, carried
                                               -- forward on resume so progress display, --limit, and
                                               -- ETA are continuous instead of restarting at 0 — see
                                               -- 04_serve.py's scan_resume()/_resume_plan() and
                                               -- scan_status()'s cumulative math. No separate "baseline
                                               -- total" exists because every already-completed item
                                               -- contributes exactly 1 to both the done *and* total
                                               -- tallies — this one count is both. Reset to 0 on every
                                               -- stage transition that ISN'T a resume (a genuine
                                               -- inventory->fingerprint handoff has no prior attempt at
                                               -- fingerprint to carry forward).
    resume_baseline_elapsed REAL DEFAULT 0    -- wall-clock seconds actually spent *processing* (not
                                               -- paused) in prior attempt(s) at the same stage —
                                               -- (finished_at - stage_started_at) of the row being
                                               -- resumed from, accumulated across however many resume
                                               -- hops occurred, same pattern as resume_baseline_done.
                                               -- Lets scan_status() compute a real ETA immediately on
                                               -- resume (rate = cumulative done / cumulative *active*
                                               -- elapsed) instead of showing "estimating..." until this
                                               -- invocation's own first completion — without this, the
                                               -- rate calculation had no data to work with right after
                                               -- a resume even though plenty already existed from
                                               -- before the pause. Reset to 0 alongside
                                               -- resume_baseline_done on a non-resume stage transition.
);
"""


def _column_exists(conn, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


@contextmanager
def connect(db_path: str | Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The scan orchestrator (04_serve.py) polls/updates scan_runs from a
    # separate connection while a pipeline stage's own connection is open
    # and periodically committing — without a busy timeout, that momentary
    # overlap raises "database is locked" instead of just waiting it out.
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # One-off migration for DBs created before target_workers existed
        # (CREATE TABLE IF NOT EXISTS above is a no-op against an existing
        # table, so the column has to be added explicitly). Idempotent —
        # safe to call init_db() repeatedly, e.g. on every process start.
        if not _column_exists(conn, "scan_runs", "target_workers"):
            conn.execute("ALTER TABLE scan_runs ADD COLUMN target_workers INTEGER")
        if not _column_exists(conn, "videos", "missing_since"):
            conn.execute("ALTER TABLE videos ADD COLUMN missing_since REAL")
        if not _column_exists(conn, "scan_runs", "resume_baseline_done"):
            conn.execute("ALTER TABLE scan_runs ADD COLUMN resume_baseline_done INTEGER DEFAULT 0")
        if not _column_exists(conn, "scan_runs", "resume_baseline_elapsed"):
            conn.execute("ALTER TABLE scan_runs ADD COLUMN resume_baseline_elapsed REAL DEFAULT 0")
        # One-off migration: decisions.matched_candidate_id used to have no
        # ON DELETE clause (SQLite default NO ACTION) on DBs created before
        # this was fixed — see the column's comment in SCHEMA above for why
        # that broke prune_missing_files(). SQLite has no ALTER TABLE to
        # change an existing FK's ON DELETE clause, so detect the stale
        # definition via PRAGMA foreign_key_list and rebuild the table.
        # Nothing else REFERENCES decisions, so this rebuild is self-
        # contained. Idempotent: re-checks the live FK definition every
        # call, not just a version flag, so it's safe to call repeatedly.
        fk_info = conn.execute("PRAGMA foreign_key_list(decisions)").fetchall()
        matched_candidate_fk = next(
            (r for r in fk_info if r["from"] == "matched_candidate_id"), None
        )
        if matched_candidate_fk and matched_candidate_fk["on_delete"] != "SET NULL":
            conn.execute("ALTER TABLE decisions RENAME TO decisions_old")
            conn.executescript(
                """
                CREATE TABLE decisions (
                    preview_id      INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                    status          TEXT NOT NULL,
                    matched_candidate_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
                    decided_at      REAL,
                    note            TEXT
                )
                """
            )
            conn.execute("INSERT INTO decisions SELECT * FROM decisions_old")
            conn.execute("DROP TABLE decisions_old")


def update_scan_run(db_path: str | Path, run_id: int | None, **fields):
    """Best-effort progress update for a running scan stage (see scan_runs
    above). Swallows sqlite errors deliberately (never raises): a
    progress-reporting hiccup must never abort the actual
    inventory/fingerprint/match work, which is the expensive,
    hard-to-redo part. Does log the failure to stderr though — a past
    real bug (the caller holding a long-lived uncommitted transaction,
    which blocked this function's own short-lived connection past
    busy_timeout) silently froze the displayed progress for minutes
    while the actual work kept going underneath, and a swallowed
    exception with literally no trace made that needlessly hard to
    track down. Callers that hold a transaction open across many
    iterations should commit it frequently — not just periodically — to
    avoid starving this function's writes; see 01_inventory.py /
    02_fingerprint.py's main loops for the fixed pattern."""
    if run_id is None or not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    try:
        with connect(db_path) as conn:
            conn.execute(f"UPDATE scan_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
    except sqlite3.Error as e:
        print(f"  [update_scan_run failed, progress display may lag] {e}", file=sys.stderr)


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "data/library.db"
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    init_db(target)
    print(f"Initialized schema at {target}")
