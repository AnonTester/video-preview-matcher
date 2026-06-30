"""
Tests the pure/non-subprocess pieces of the scan orchestration added to
04_serve.py: update_scan_run() (db.py) and _build_cmd() (04_serve.py).
Also covers queue_rows() — the query behind the "/" review queue,
specifically its missing_since filtering (a preview or candidate not
found on disk during the last inventory scan gets hidden from the active
queue without anything being deleted; see db.py's missing_since
docstring and 01_inventory.py's MISSING-FILE DETECTION section). Also
covers _resume_plan() and _progress_with_baseline() — the fix for three
real bugs found live in a --limit 1000 fingerprint run that got
interrupted and resumed: a live-adjusted worker count reverting to the
scan's original starting value, --limit overshooting the user's actual
intended total, and progress/elapsed/ETA resetting to 0 instead of
continuing from where the interrupted attempt left off (see
_resume_plan()'s docstring in 04_serve.py for the full writeup).
Doesn't spin up any subprocess, thread, or HTTP server — that part is
exercised live (start/cancel/pause/resume against the running container),
not here. Loads 04_serve.py directly via importlib (its filename starts
with a digit, so it can't be `import`ed normally; this also requires
fastapi/starlette to be installed, same as render_templates_test.py would
if it imported 04_serve.py). Run from project root:

    python3 tests/scan_orchestration_test.py
"""

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from db import connect, init_db, update_scan_run  # noqa: E402

spec = importlib.util.spec_from_file_location("serve_mod", PROJECT_ROOT / "src" / "04_serve.py")
serve_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve_mod)

TMP_DB = Path("/tmp/scan_orchestration_test.db")


def reset():
    TMP_DB.unlink(missing_ok=True)
    init_db(TMP_DB)


def test_update_scan_run_writes_fields():
    reset()
    with connect(TMP_DB) as conn:
        conn.execute(
            "INSERT INTO scan_runs (id, params_json, status, started_at, updated_at) "
            "VALUES (1, '{}', 'running', ?, ?)",
            (time.time(), time.time()),
        )

    update_scan_run(TMP_DB, 1, stage="inventory", stage_total=10, stage_done=3, message="working")

    with connect(TMP_DB) as conn:
        row = conn.execute("SELECT * FROM scan_runs WHERE id = 1").fetchone()
    assert row["stage"] == "inventory"
    assert row["stage_total"] == 10
    assert row["stage_done"] == 3
    assert row["message"] == "working"
    print("test_update_scan_run_writes_fields: OK")


def test_update_scan_run_noop_without_run_id():
    reset()
    # Should not raise even though scan_runs is empty and run_id is None —
    # a progress-reporting no-op must never be the thing that crashes a
    # pipeline stage invoked without a --run-id (i.e. run by hand).
    update_scan_run(TMP_DB, None, stage="inventory", stage_done=1)
    with connect(TMP_DB) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM scan_runs").fetchone()["n"]
    assert count == 0
    print("test_update_scan_run_noop_without_run_id: OK")


def test_target_workers_column_exists_after_init_db():
    """Migration check: target_workers was added to scan_runs after the
    table already existed in deployed DBs, via an explicit ALTER TABLE in
    init_db() (CREATE TABLE IF NOT EXISTS is a no-op against an existing
    table, so the column needs its own migration step)."""
    reset()
    with connect(TMP_DB) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(scan_runs)")}
    assert "target_workers" in cols
    print("test_target_workers_column_exists_after_init_db: OK")


def test_init_db_migration_is_idempotent():
    """Calling init_db() again (e.g. on every process start) must not
    raise just because target_workers already exists from a prior call."""
    reset()
    init_db(TMP_DB)  # second call against the already-migrated DB
    init_db(TMP_DB)  # and a third, for good measure
    print("test_init_db_migration_is_idempotent: OK")


def _matched_candidate_on_delete(conn):
    fk_info = conn.execute("PRAGMA foreign_key_list(decisions)").fetchall()
    row = next(r for r in fk_info if r["from"] == "matched_candidate_id")
    return row["on_delete"]


def test_fresh_db_has_set_null_on_matched_candidate_id():
    """A fresh DB (the SCHEMA string, not a migration) must define
    matched_candidate_id with ON DELETE SET NULL — see db.py's comment
    on this column for the prune_missing_files() bug this guards
    against."""
    reset()
    with connect(TMP_DB) as conn:
        assert _matched_candidate_on_delete(conn) == "SET NULL"
    print("test_fresh_db_has_set_null_on_matched_candidate_id: OK")


def test_init_db_migrates_stale_matched_candidate_id_fk():
    """The actual incident: DBs created before this fix have
    matched_candidate_id with no ON DELETE clause (SQLite default NO
    ACTION) — pruning a missing video referenced there fails outright
    with a FOREIGN KEY constraint error. init_db() must detect and
    rebuild the table on an already-existing, stale-schema DB, without
    losing any existing decisions rows."""
    TMP_DB.unlink(missing_ok=True)
    with connect(TMP_DB) as conn:
        conn.executescript(
            """
            CREATE TABLE videos (id INTEGER PRIMARY KEY, path TEXT, filename TEXT,
                duration_sec REAL, missing_since REAL);
            CREATE TABLE decisions (
                preview_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                matched_candidate_id INTEGER REFERENCES videos(id),
                decided_at REAL,
                note TEXT
            );
            """
        )
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (1, '/v/1.mp4', '1.mp4')")
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (2, '/v/2.mp4', '2.mp4')")
        conn.execute(
            "INSERT INTO decisions (preview_id, status, matched_candidate_id, decided_at) "
            "VALUES (1, 'staged', 2, 5.0)"
        )

    init_db(TMP_DB)  # this is the migration under test

    with connect(TMP_DB) as conn:
        assert _matched_candidate_on_delete(conn) == "SET NULL"
        row = conn.execute("SELECT * FROM decisions WHERE preview_id = 1").fetchone()
        assert row["status"] == "staged"
        assert row["matched_candidate_id"] == 2
        assert row["decided_at"] == 5.0
    print("test_init_db_migrates_stale_matched_candidate_id_fk: OK")


def _seed_video(conn, vid, missing_since=None, duration_sec=10.0):
    conn.execute(
        "INSERT INTO videos (id, path, filename, duration_sec, missing_since) VALUES (?, ?, ?, ?, ?)",
        (vid, f"/v/{vid}.mp4", f"{vid}.mp4", duration_sec, missing_since),
    )


def _seed_match(conn, preview_id, candidate_id, combined_score):
    conn.execute(
        "INSERT INTO matches (preview_id, candidate_id, visual_score, combined_score) VALUES (?, ?, ?, ?)",
        (preview_id, candidate_id, combined_score, combined_score),
    )


def _seed_decision(conn, preview_id, status, matched_candidate_id=None, decided_at=0.0):
    conn.execute(
        "INSERT INTO decisions (preview_id, status, matched_candidate_id, decided_at) VALUES (?, ?, ?, ?)",
        (preview_id, status, matched_candidate_id, decided_at),
    )


def test_queue_rows_includes_normal_match():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2)
        _seed_match(conn, 1, 2, 0.9)
        rows, total = serve_mod.queue_rows(conn)
    assert [r["preview_id"] for r in rows] == [1]
    assert rows[0]["candidate_id"] == 2
    assert total == 1
    print("test_queue_rows_includes_normal_match: OK")


def test_queue_rows_excludes_missing_preview():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1, missing_since=123.0)
        _seed_video(conn, 2)
        _seed_match(conn, 1, 2, 0.9)
        rows, total = serve_mod.queue_rows(conn)
    assert rows == [], rows
    assert total == 0
    print("test_queue_rows_excludes_missing_preview: OK")


def test_queue_rows_falls_back_to_second_best_when_top_candidate_missing():
    """A preview's #1-scoring candidate vanishing shouldn't blank the
    whole row — the next-best *non-missing* candidate should surface
    instead."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2, missing_since=123.0)  # higher score, but missing
        _seed_video(conn, 3)                         # lower score, present
        _seed_match(conn, 1, 2, 0.95)
        _seed_match(conn, 1, 3, 0.50)
        rows, total = serve_mod.queue_rows(conn)
    assert len(rows) == 1, rows
    assert rows[0]["candidate_id"] == 3, rows[0]
    assert total == 1
    print("test_queue_rows_falls_back_to_second_best_when_top_candidate_missing: OK")


def test_queue_rows_excludes_preview_when_all_candidates_missing():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2, missing_since=123.0)
        _seed_video(conn, 3, missing_since=456.0)
        _seed_match(conn, 1, 2, 0.95)
        _seed_match(conn, 1, 3, 0.50)
        rows, total = serve_mod.queue_rows(conn)
    assert rows == [], rows
    assert total == 0
    print("test_queue_rows_excludes_preview_when_all_candidates_missing: OK")


def test_queue_rows_excludes_staged_preview():
    """A staged preview belongs in staged_queue_rows()'s bucket, not
    here — this is the pending-tab/staged-tab split."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2)
        _seed_match(conn, 1, 2, 0.9)
        _seed_decision(conn, 1, "staged")
        rows, total = serve_mod.queue_rows(conn)
    assert rows == [], rows
    assert total == 0
    print("test_queue_rows_excludes_staged_preview: OK")


def test_queue_rows_excludes_rejected_preview():
    """A rejected preview belongs in rejected_queue_rows()'s bucket, not
    here — same pending-tab/staged-tab/rejected-tab three-way split."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2)
        _seed_match(conn, 1, 2, 0.9)
        _seed_decision(conn, 1, "rejected")
        rows, total = serve_mod.queue_rows(conn)
    assert rows == [], rows
    assert total == 0
    print("test_queue_rows_excludes_rejected_preview: OK")


def test_queue_rows_paginates():
    reset()
    with connect(TMP_DB) as conn:
        for i in range(1, 6):
            _seed_video(conn, i * 2 - 1)
            _seed_video(conn, i * 2)
            _seed_match(conn, i * 2 - 1, i * 2, 1.0 - i * 0.01)  # descending scores
        page1, total = serve_mod.queue_rows(conn, page=1, page_size=2)
        page2, total2 = serve_mod.queue_rows(conn, page=2, page_size=2)
    assert total == 5 and total2 == 5
    assert [r["preview_id"] for r in page1] == [1, 3]
    assert [r["preview_id"] for r in page2] == [5, 7]
    print("test_queue_rows_paginates: OK")


def test_staged_queue_rows_includes_staged_preview_independent_of_matches():
    """The actual incident this exists for: a staged preview must stay
    visible even when it has no `matches` row at all (e.g. dropped by a
    later 03_match.py re-score) — staged_queue_rows() reads straight from
    `decisions`, never joining through `matches`."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2)
        # Deliberately no _seed_match() call — no matches row exists.
        _seed_decision(conn, 1, "staged", matched_candidate_id=2, decided_at=5.0)
        rows, total = serve_mod.staged_queue_rows(conn)
    assert total == 1
    assert rows[0]["preview_id"] == 1
    assert rows[0]["candidate_id"] == 2
    assert rows[0]["candidate_filename"] == "2.mp4"
    print("test_staged_queue_rows_includes_staged_preview_independent_of_matches: OK")


def test_staged_queue_rows_includes_staged_preview_even_when_missing():
    """Independently of the matches-table fix: staged_queue_rows() must
    not filter on missing_since either, so a preview wrongly (or
    legitimately, post-purge) flagged missing while still staged stays
    reachable for undo."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1, missing_since=999.0)
        _seed_decision(conn, 1, "staged")
        rows, total = serve_mod.staged_queue_rows(conn)
    assert total == 1
    assert rows[0]["preview_id"] == 1
    print("test_staged_queue_rows_includes_staged_preview_even_when_missing: OK")


def test_staged_queue_rows_excludes_non_staged_decisions():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_decision(conn, 1, "rejected")
        rows, total = serve_mod.staged_queue_rows(conn)
    assert rows == [] and total == 0
    print("test_staged_queue_rows_excludes_non_staged_decisions: OK")


def test_staged_queue_rows_orders_most_recently_staged_first():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2)
        _seed_decision(conn, 1, "staged", decided_at=1.0)
        _seed_decision(conn, 2, "staged", decided_at=2.0)
        rows, total = serve_mod.staged_queue_rows(conn)
    assert total == 2
    assert [r["preview_id"] for r in rows] == [2, 1]
    print("test_staged_queue_rows_orders_most_recently_staged_first: OK")


def test_rejected_queue_rows_includes_rejected_preview_independent_of_matches():
    """Same reasoning as staged_queue_rows (see its docstring), applied
    to rejected: a rejected preview must stay visible/undoable even with
    no `matches` row at all."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2)
        # Deliberately no _seed_match() call — no matches row exists.
        _seed_decision(conn, 1, "rejected", matched_candidate_id=2, decided_at=5.0)
        rows, total = serve_mod.rejected_queue_rows(conn)
    assert total == 1
    assert rows[0]["preview_id"] == 1
    assert rows[0]["candidate_id"] == 2
    assert rows[0]["candidate_filename"] == "2.mp4"
    print("test_rejected_queue_rows_includes_rejected_preview_independent_of_matches: OK")


def test_rejected_queue_rows_excludes_non_rejected_decisions():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_decision(conn, 1, "staged")
        rows, total = serve_mod.rejected_queue_rows(conn)
    assert rows == [] and total == 0
    print("test_rejected_queue_rows_excludes_non_rejected_decisions: OK")


def test_rejected_queue_rows_orders_most_recently_rejected_first():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_video(conn, 2)
        _seed_decision(conn, 1, "rejected", decided_at=1.0)
        _seed_decision(conn, 2, "rejected", decided_at=2.0)
        rows, total = serve_mod.rejected_queue_rows(conn)
    assert total == 2
    assert [r["preview_id"] for r in rows] == [2, 1]
    print("test_rejected_queue_rows_orders_most_recently_rejected_first: OK")


def _seed_three_tab_fixture(conn):
    """One preview in each bucket: #1 truly pending, #3 staged, #5
    rejected — shared by the three _queue_page_data tests below so
    their counts can't accidentally agree by coincidence."""
    _seed_video(conn, 1)
    _seed_video(conn, 2)
    _seed_match(conn, 1, 2, 0.9)
    _seed_video(conn, 3)
    _seed_decision(conn, 3, "staged")
    _seed_video(conn, 5)
    _seed_decision(conn, 5, "rejected")


def test_queue_page_data_pending_tab():
    """_queue_page_data is what both index() (HTML) and /api/queue
    (JSON, used by the queue page's AJAX tab/pagination switching — see
    index.html's loadQueue()) build their response from — must stay in
    sync with queue_rows()/staged_queue_rows()/rejected_queue_rows()'s
    own counts."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_three_tab_fixture(conn)
        data = serve_mod._queue_page_data(conn, "pending", 1)
    assert data["tab"] == "pending"
    assert data["page"] == 1
    assert data["total_count"] == 1
    assert data["pending_count"] == 1
    assert data["staged_count"] == 1
    assert data["rejected_count"] == 1
    assert data["total_pages"] == 1
    assert [m["preview_id"] for m in data["matches"]] == [1]
    print("test_queue_page_data_pending_tab: OK")


def test_queue_page_data_staged_tab():
    reset()
    with connect(TMP_DB) as conn:
        _seed_three_tab_fixture(conn)
        data = serve_mod._queue_page_data(conn, "staged", 1)
    assert data["tab"] == "staged"
    assert data["total_count"] == 1
    assert data["pending_count"] == 1
    assert data["staged_count"] == 1
    assert data["rejected_count"] == 1
    assert [m["preview_id"] for m in data["matches"]] == [3]
    print("test_queue_page_data_staged_tab: OK")


def test_queue_page_data_rejected_tab():
    reset()
    with connect(TMP_DB) as conn:
        _seed_three_tab_fixture(conn)
        data = serve_mod._queue_page_data(conn, "rejected", 1)
    assert data["tab"] == "rejected"
    assert data["total_count"] == 1
    assert data["pending_count"] == 1
    assert data["staged_count"] == 1
    assert data["rejected_count"] == 1
    assert [m["preview_id"] for m in data["matches"]] == [5]
    print("test_queue_page_data_rejected_tab: OK")


def test_build_cmd_inventory_includes_roots_and_limit():
    cmd = serve_mod._build_cmd("01_inventory.py", 7, "/data/library.db", {"roots": ["/a", "/b"], "limit": 50})
    assert "/a" in cmd and "/b" in cmd
    assert "--limit" in cmd and cmd[cmd.index("--limit") + 1] == "50"
    assert "--run-id" in cmd and cmd[cmd.index("--run-id") + 1] == "7"
    assert "--db" in cmd and cmd[cmd.index("--db") + 1] == "/data/library.db"
    print("test_build_cmd_inventory_includes_roots_and_limit: OK")


def test_build_cmd_includes_debug_log_for_inventory_and_fingerprint():
    for stage_file in ("01_inventory.py", "02_fingerprint.py"):
        cmd = serve_mod._build_cmd(stage_file, 7, "/data/library.db", {"roots": ["/a"], "limit": None})
        assert "--debug-log" in cmd, (stage_file, cmd)
        assert cmd[cmd.index("--debug-log") + 1] == "/data/subprocess.log"
    # 03_match.py doesn't shell out to ffmpeg/ffprobe, so no debug log needed
    cmd = serve_mod._build_cmd("03_match.py", 7, "/data/library.db", {"roots": ["/a"], "limit": None})
    assert "--debug-log" not in cmd
    print("test_build_cmd_includes_debug_log_for_inventory_and_fingerprint: OK")


def test_build_cmd_inventory_omits_limit_when_not_set():
    cmd = serve_mod._build_cmd("01_inventory.py", 7, "/data/library.db", {"roots": ["/a"], "limit": None})
    assert "--limit" not in cmd
    print("test_build_cmd_inventory_omits_limit_when_not_set: OK")


def test_build_cmd_match_has_no_roots_or_limit():
    cmd = serve_mod._build_cmd("03_match.py", 7, "/data/library.db", {"roots": ["/a"], "limit": 50})
    assert "/a" not in cmd
    assert "--limit" not in cmd
    print("test_build_cmd_match_has_no_roots_or_limit: OK")


def test_build_cmd_fingerprint_includes_workers_when_set():
    cmd = serve_mod._build_cmd("02_fingerprint.py", 7, "/data/library.db", {"fp_workers": 6})
    assert "--workers" in cmd and cmd[cmd.index("--workers") + 1] == "6"
    print("test_build_cmd_fingerprint_includes_workers_when_set: OK")


def test_build_cmd_fingerprint_omits_workers_when_not_set():
    cmd = serve_mod._build_cmd("02_fingerprint.py", 7, "/data/library.db", {})
    assert "--workers" not in cmd
    print("test_build_cmd_fingerprint_omits_workers_when_not_set: OK")


def test_build_cmd_match_includes_workers_when_set():
    cmd = serve_mod._build_cmd("03_match.py", 7, "/data/library.db", {"match_workers": 3})
    assert "--workers" in cmd and cmd[cmd.index("--workers") + 1] == "3"
    print("test_build_cmd_match_includes_workers_when_set: OK")


def test_build_cmd_match_omits_workers_when_not_set():
    cmd = serve_mod._build_cmd("03_match.py", 7, "/data/library.db", {})
    assert "--workers" not in cmd
    print("test_build_cmd_match_omits_workers_when_not_set: OK")


def test_stages_from_none_runs_everything():
    stages = serve_mod._stages_from(serve_mod.ALL_STAGES, None)
    assert [n for n, _ in stages] == ["inventory", "fingerprint", "match"]
    print("test_stages_from_none_runs_everything: OK")


def test_stages_from_fingerprint_skips_inventory():
    """The actual bug: resuming a run that was interrupted mid-fingerprint
    used to always restart at inventory, which re-walks the library and
    can pull in a fresh batch of new-to-probe files — crowding out the
    specific files the fingerprint stage still had left once --limit is
    re-applied to the now-larger backlog."""
    stages = serve_mod._stages_from(serve_mod.ALL_STAGES, "fingerprint")
    assert [n for n, _ in stages] == ["fingerprint", "match"]
    print("test_stages_from_fingerprint_skips_inventory: OK")


def test_stages_from_match_skips_inventory_and_fingerprint():
    stages = serve_mod._stages_from(serve_mod.ALL_STAGES, "match")
    assert [n for n, _ in stages] == ["match"]
    print("test_stages_from_match_skips_inventory_and_fingerprint: OK")


def test_stages_from_unknown_stage_runs_everything():
    stages = serve_mod._stages_from(serve_mod.ALL_STAGES, "not-a-real-stage")
    assert [n for n, _ in stages] == ["inventory", "fingerprint", "match"]
    print("test_stages_from_unknown_stage_runs_everything: OK")


def test_selected_stages_defaults_to_all():
    stages = serve_mod._selected_stages({})
    assert [n for n, _ in stages] == ["inventory", "fingerprint", "match"]
    print("test_selected_stages_defaults_to_all: OK")


def test_selected_stages_honors_explicit_subset():
    stages = serve_mod._selected_stages({"stages": ["fingerprint"]})
    assert [n for n, _ in stages] == ["fingerprint"]
    print("test_selected_stages_honors_explicit_subset: OK")


def test_selected_stages_keeps_canonical_order_regardless_of_input_order():
    stages = serve_mod._selected_stages({"stages": ["match", "inventory"]})
    assert [n for n, _ in stages] == ["inventory", "match"]
    print("test_selected_stages_keeps_canonical_order_regardless_of_input_order: OK")


def test_resume_skip_composes_with_stage_selection():
    """A run that only selected ["fingerprint", "match"] and was
    interrupted during "match" should resume at "match" only — not fall
    back to fingerprint (already done) just because start_stage isn't
    the first entry in ALL_STAGES."""
    selected = serve_mod._selected_stages({"stages": ["fingerprint", "match"]})
    resumed = serve_mod._stages_from(selected, "match")
    assert [n for n, _ in resumed] == ["match"]
    print("test_resume_skip_composes_with_stage_selection: OK")


def _row(**overrides):
    base = {
        "params_json": '{"limit": null, "stages": ["fingerprint"]}',
        "status": "interrupted",
        "stage": "fingerprint",
        "stage_total": None,
        "stage_done": 0,
        "stage_started_at": 1000.0,
        "finished_at": 1100.0,
        "target_workers": None,
        "resume_baseline_done": 0,
        "resume_baseline_elapsed": 0,
        "started_at": 1000.0,
    }
    base.update(overrides)
    return base


def test_resume_plan_carries_forward_live_adjusted_workers():
    """The actual bug: /api/scan/workers only ever updates the live row's
    target_workers, never params_json — a naive resume replaying
    params_json unmodified silently reverts to the scan's *original*
    starting worker count, discarding a live adjustment made mid-run."""
    plan = serve_mod._resume_plan(_row(
        params_json='{"limit": null, "fp_workers": null, "stages": ["fingerprint"]}',
        target_workers=8,
    ))
    assert plan["params"]["fp_workers"] == 8
    print("test_resume_plan_carries_forward_live_adjusted_workers: OK")


def test_resume_plan_reduces_limit_by_stage_done():
    """The actual bug: --limit applies to whatever's currently
    outstanding, which after a partial run already excludes the
    completed items — replaying the original --limit unmodified starts a
    *fresh* batch of that size on top, overshooting the user's actual
    intended total (134 done + a fresh 1000 = 1134, not 1000)."""
    plan = serve_mod._resume_plan(_row(
        params_json='{"limit": 1000, "stages": ["fingerprint"]}',
        stage_done=134,
    ))
    assert plan["first_stage_limit"] == 866, plan["first_stage_limit"]
    print("test_resume_plan_reduces_limit_by_stage_done: OK")


def test_resume_plan_limit_unset_when_no_limit_in_params():
    plan = serve_mod._resume_plan(_row(
        params_json='{"limit": null, "stages": ["fingerprint"]}',
        stage_done=134,
    ))
    assert plan["first_stage_limit"] is None
    print("test_resume_plan_limit_unset_when_no_limit_in_params: OK")


def test_resume_plan_limit_unset_when_nothing_done_yet():
    # Interrupted before any progress — the original limit is still
    # exactly right, no reduction needed.
    plan = serve_mod._resume_plan(_row(
        params_json='{"limit": 1000, "stages": ["fingerprint"]}',
        stage_done=0,
    ))
    assert plan["first_stage_limit"] is None
    print("test_resume_plan_limit_unset_when_nothing_done_yet: OK")


def test_resume_plan_baseline_accumulates_across_chained_resumes():
    """A second resume of an already-once-resumed run must carry forward
    *both* the original attempt's progress (resume_baseline_done, from
    the first resume) *and* the first resume's own progress before it
    was interrupted again (stage_done) — not just the latter."""
    plan = serve_mod._resume_plan(_row(stage_done=84, resume_baseline_done=50))
    assert plan["baseline_done"] == 134, plan["baseline_done"]
    print("test_resume_plan_baseline_accumulates_across_chained_resumes: OK")


def test_resume_plan_limit_reduction_uses_cumulative_done_not_just_this_row():
    """The actual bug this guards against: a *second* resume of an
    already-once-resumed run computed its --limit reduction from just
    this row's own stage_done (84), undercounting by exactly the first
    resume's own baseline contribution (50) — letting the run overshoot
    by 50 items. The reduction must use the full cumulative total (134),
    same number as plan["baseline_done"]."""
    plan = serve_mod._resume_plan(_row(
        params_json='{"limit": 1000, "stages": ["fingerprint"]}',
        stage_done=84, resume_baseline_done=50,
    ))
    assert plan["first_stage_limit"] == 1000 - 134, plan["first_stage_limit"]
    print("test_resume_plan_limit_reduction_uses_cumulative_done_not_just_this_row: OK")


def test_resume_plan_preserves_started_at_and_start_stage():
    # Non-match stage: started_at carries forward unmodified (point 3 in
    # _resume_plan's docstring) — see the dedicated match tests below for
    # why "match" specifically is the one exception to this.
    plan = serve_mod._resume_plan(_row(started_at=12345.0, stage="fingerprint"))
    assert plan["started_at"] == 12345.0
    assert plan["start_stage"] == "fingerprint"
    print("test_resume_plan_preserves_started_at_and_start_stage: OK")


def test_resume_plan_match_stage_zeroes_baseline_and_resets_started_at():
    """The actual bug, found live: 03_match.py has no incremental concept
    at all (every invocation rescoring every pair from scratch) — a match
    run that failed at stage_done == stage_total (it scored every pair,
    then crashed writing the results) had that entire already-finished
    count folded in as resume_baseline_done on resume, doubling the
    displayed total (~16.4M + a fresh ~16.4M = ~32.8M) and inflating
    "running for" with the fully-discarded prior attempt's own duration.
    A match resume must be a clean restart: zero baseline_done/
    baseline_elapsed, no --limit reduction, and started_at reset to None
    so _start_scan's own "now" default applies instead of carrying
    forward a stale timestamp from a run whose progress isn't actually
    being continued."""
    plan = serve_mod._resume_plan(_row(
        stage="match", started_at=12345.0, stage_done=16_418_461,
        stage_started_at=1000.0, finished_at=1932.3,
        params_json='{"limit": null, "stages": ["match"]}',
    ))
    assert plan["baseline_done"] == 0, plan["baseline_done"]
    assert plan["baseline_elapsed"] == 0.0, plan["baseline_elapsed"]
    assert plan["started_at"] is None, plan["started_at"]
    assert plan["start_stage"] == "match"
    print("test_resume_plan_match_stage_zeroes_baseline_and_resets_started_at: OK")


def test_resume_plan_match_stage_ignores_chained_resume_baselines_too():
    """Same as above, but also confirms a match resume zeroes baseline_done
    even when resume_baseline_done/resume_baseline_elapsed are already
    nonzero from an earlier (non-match) resume hop earlier in the same
    multi-stage run — match must never inherit *any* carried-forward
    baseline, not just its own immediately-prior attempt's."""
    plan = serve_mod._resume_plan(_row(
        stage="match", stage_done=500, resume_baseline_done=200,
        resume_baseline_elapsed=300.0,
    ))
    assert plan["baseline_done"] == 0, plan["baseline_done"]
    assert plan["baseline_elapsed"] == 0.0, plan["baseline_elapsed"]
    print("test_resume_plan_match_stage_ignores_chained_resume_baselines_too: OK")


def test_resume_plan_computes_baseline_elapsed_from_active_processing_time():
    """baseline_elapsed is *active processing* time (finished_at minus
    stage_started_at for the row being resumed from), not wall-clock
    time since the job originally started — a long pause between
    interruption and resume must not inflate it."""
    plan = serve_mod._resume_plan(_row(stage_started_at=1000.0, finished_at=1090.0))
    assert plan["baseline_elapsed"] == 90.0, plan["baseline_elapsed"]
    print("test_resume_plan_computes_baseline_elapsed_from_active_processing_time: OK")


def test_resume_plan_baseline_elapsed_accumulates_across_chained_resumes():
    """Same accumulation pattern as baseline_done — a second resume must
    add this row's own active time on top of whatever was already
    carried into it from an even earlier resume."""
    plan = serve_mod._resume_plan(_row(
        stage_started_at=2000.0, finished_at=2050.0,  # 50s active this attempt
        resume_baseline_elapsed=90.0,  # carried in from an earlier resume
    ))
    assert plan["baseline_elapsed"] == 140.0, plan["baseline_elapsed"]
    print("test_resume_plan_baseline_elapsed_accumulates_across_chained_resumes: OK")


def test_progress_with_baseline_adds_baseline_to_done_and_total():
    """No separate "baseline total" exists — folding the same
    baseline_done into both done and total is what reconstructs the
    *original* intended total (12 done + 188 of this invocation's own
    --limit-reduced total = 200, not 12 + 200 = 212)."""
    done, total, eta = serve_mod._progress_with_baseline(
        stage_done=188, stage_total=188, stage_started_at=1000.0,
        baseline_done=12, baseline_elapsed=0, now=1000.0,
    )
    assert (done, total) == (200, 200), (done, total)
    print("test_progress_with_baseline_adds_baseline_to_done_and_total: OK")


def test_progress_with_baseline_eta_available_immediately_on_resume():
    """The actual bug this guards against: right after a resume, before
    this invocation has completed anything of its own (stage_done=0,
    stage_elapsed=0), the old rate calculation had nothing to divide by
    and showed no ETA at all — despite plenty of measured throughput
    already existing from before the pause. baseline_elapsed is what
    that rate calculation now has to work with even at stage_elapsed=0:
    134 done over 134s of *prior* active time = 1/s, applied to the 100
    remaining (stage_total=100) for a 100s ETA, with zero data from this
    invocation itself yet."""
    done, total, eta = serve_mod._progress_with_baseline(
        stage_done=0, stage_total=100, stage_started_at=5000.0,
        baseline_done=134, baseline_elapsed=134.0, now=5000.0,
    )
    assert (done, total) == (134, 234), (done, total)
    assert eta == 100.0, eta  # 100 remaining / (134 done / 134s active = 1/s)
    print("test_progress_with_baseline_eta_available_immediately_on_resume: OK")


def test_progress_with_baseline_rate_blends_baseline_and_fresh_progress():
    """As this invocation accumulates its own progress, the rate is
    cumulative done over cumulative *active* elapsed (not wall-clock,
    and not baseline-only or fresh-only) — 100 total done over 100s
    total active time here, blending a 90/90s prior rate with a 10/10s
    fresh one (both happen to be 1/s, chosen so the blended result is
    unambiguous)."""
    done, total, eta = serve_mod._progress_with_baseline(
        stage_done=10, stage_total=100, stage_started_at=1000.0,
        baseline_done=90, baseline_elapsed=90.0, now=1010.0,
    )
    assert (done, total) == (100, 190), (done, total)
    assert eta == 90.0, eta  # (190-100) remaining / (100/100s = 1/s) rate
    print("test_progress_with_baseline_rate_blends_baseline_and_fresh_progress: OK")


def test_progress_with_baseline_no_baseline_matches_original_behavior():
    done, total, eta = serve_mod._progress_with_baseline(
        stage_done=50, stage_total=200, stage_started_at=1000.0,
        baseline_done=0, baseline_elapsed=0, now=1050.0,
    )
    assert (done, total) == (50, 200)
    assert eta == 150.0, eta  # (200-50) / (50/50s = 1/s)
    print("test_progress_with_baseline_no_baseline_matches_original_behavior: OK")


def test_progress_with_baseline_none_stage_total_passes_through():
    done, total, eta = serve_mod._progress_with_baseline(
        stage_done=0, stage_total=None, stage_started_at=None,
        baseline_done=0, baseline_elapsed=0, now=100.0,
    )
    assert total is None
    assert eta is None
    print("test_progress_with_baseline_none_stage_total_passes_through: OK")


def test_all_descendant_pids_finds_grandchildren():
    """The actual bug this guards against: procutil.run_with_hard_timeout
    starts ffmpeg with start_new_session=True, so it's in its own process
    group — an upstream killpg() on the orchestrator's subprocess never
    reaches it. _all_descendant_pids walks /proc instead, which doesn't
    care about process-group membership. Spawns a real bash -> sleep
    pair (sleep is bash's child, not the test's direct child) and
    confirms the grandchild is found."""
    proc = subprocess.Popen(["bash", "-c", "sleep 30 & wait"])
    time.sleep(0.3)  # let bash fork its sleep child
    try:
        descendants = serve_mod._all_descendant_pids(proc.pid)
        assert len(descendants) >= 1, f"expected to find the sleep grandchild, got {descendants}"
    finally:
        for pid in [proc.pid, *serve_mod._all_descendant_pids(proc.pid)]:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
    print("test_all_descendant_pids_finds_grandchildren: OK")


def test_list_missing_files_excludes_staged():
    """The destructive incident this guards against: a staged preview
    must never show up in the missing-files list, no matter how
    missing_since ended up set on it — this list is exactly what feeds
    the prune confirmation dialog."""
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1, missing_since=100.0)
        _seed_video(conn, 2, missing_since=200.0)
        _seed_decision(conn, 1, "staged")
        files = serve_mod.list_missing_files(conn)
    assert [f["id"] for f in files] == [2], files
    print("test_list_missing_files_excludes_staged: OK")


def test_list_missing_files_includes_rejected_and_undecided():
    reset()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1, missing_since=100.0)
        _seed_video(conn, 2, missing_since=200.0)
        _seed_decision(conn, 1, "rejected")
        files = serve_mod.list_missing_files(conn)
    assert {f["id"] for f in files} == {1, 2}, files
    print("test_list_missing_files_includes_rejected_and_undecided: OK")


REMUX_TEST_DIR = Path("/tmp/scan_orch_test_remux_cache")


def _reset_remux_dir():
    import shutil
    if REMUX_TEST_DIR.exists():
        shutil.rmtree(REMUX_TEST_DIR)
    REMUX_TEST_DIR.mkdir(parents=True)


def test_prune_missing_files_never_deletes_a_staged_video():
    """The actual data-loss incident: pruning must skip a staged video
    even though it's flagged missing, since its `decisions` row
    (cascade-deleted otherwise) is the only record of where to undo it
    back to."""
    reset()
    _reset_remux_dir()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1, missing_since=100.0)
        _seed_decision(conn, 1, "staged")
        pruned_ids = serve_mod.prune_missing_files(conn, REMUX_TEST_DIR)
        remaining = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
        decisions_remaining = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    assert pruned_ids == [], pruned_ids
    assert remaining == 1, remaining
    assert decisions_remaining == 1, decisions_remaining
    print("test_prune_missing_files_never_deletes_a_staged_video: OK")


def test_prune_missing_files_deletes_genuinely_missing_videos():
    reset()
    _reset_remux_dir()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1, missing_since=100.0)
        _seed_video(conn, 2, missing_since=200.0)
        _seed_decision(conn, 1, "staged")
        pruned_ids = serve_mod.prune_missing_files(conn, REMUX_TEST_DIR)
        remaining_ids = {r["id"] for r in conn.execute("SELECT id FROM videos").fetchall()}
    assert pruned_ids == [2], pruned_ids
    assert remaining_ids == {1}, remaining_ids
    print("test_prune_missing_files_deletes_genuinely_missing_videos: OK")


def test_prune_missing_files_handles_video_referenced_as_matched_candidate():
    """The actual reported bug: pruning silently did nothing in the UI.
    Root cause: a missing video can be recorded as some *other*
    preview's decisions.matched_candidate_id (the candidate it was
    staged/rejected against) without itself being staged. Before this
    was a real DB found in the wild, this combination wasn't covered by
    any prior test — the bulk DELETE FROM videos raised
    sqlite3.IntegrityError outright, which the route never caught (500),
    and the frontend never checked res.ok before parsing JSON, so the
    failure was completely invisible. The fix is the ON DELETE SET NULL
    migration in db.py — this test seeds exactly this combination and
    asserts pruning now succeeds, with the dangling reference cleared
    but the decision itself intact."""
    reset()
    _reset_remux_dir()
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)  # preview, not itself missing
        _seed_video(conn, 2, missing_since=200.0)  # candidate, now missing
        _seed_decision(conn, 1, "staged", matched_candidate_id=2, decided_at=5.0)
        pruned_ids = serve_mod.prune_missing_files(conn, REMUX_TEST_DIR)
        remaining_ids = {r["id"] for r in conn.execute("SELECT id FROM videos").fetchall()}
        decision = conn.execute("SELECT * FROM decisions WHERE preview_id = 1").fetchone()
    assert pruned_ids == [2], pruned_ids
    assert remaining_ids == {1}, remaining_ids
    assert decision["status"] == "staged"
    assert decision["matched_candidate_id"] is None, decision["matched_candidate_id"]
    print("test_prune_missing_files_handles_video_referenced_as_matched_candidate: OK")


def test_prune_missing_files_returns_empty_list_when_nothing_to_prune():
    reset()
    _reset_remux_dir()
    with connect(TMP_DB) as conn:
        pruned_ids = serve_mod.prune_missing_files(conn, REMUX_TEST_DIR)
    assert pruned_ids == [], pruned_ids
    print("test_prune_missing_files_returns_empty_list_when_nothing_to_prune: OK")


def test_prune_missing_files_deletes_orphaned_remux_cache_entry():
    """The actual gap this guards against: a missing video that was
    ever played through the remux fallback leaves a permanent orphaned
    remux_cache/{id}.mp4 behind once its row is pruned, unless pruning
    itself cleans it up — nothing else ever revisits remux_cache/."""
    reset()
    _reset_remux_dir()
    (REMUX_TEST_DIR / "2.mp4").write_text("cached remux content")
    with connect(TMP_DB) as conn:
        _seed_video(conn, 2, missing_since=200.0)
        serve_mod.prune_missing_files(conn, REMUX_TEST_DIR)
    assert not (REMUX_TEST_DIR / "2.mp4").exists()
    print("test_prune_missing_files_deletes_orphaned_remux_cache_entry: OK")


def test_prune_missing_files_leaves_other_remux_cache_entries_alone():
    reset()
    _reset_remux_dir()
    (REMUX_TEST_DIR / "1.mp4").write_text("still a live video, never pruned")
    (REMUX_TEST_DIR / "2.mp4").write_text("genuinely missing, gets pruned")
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)  # not missing
        _seed_video(conn, 2, missing_since=200.0)
        serve_mod.prune_missing_files(conn, REMUX_TEST_DIR)
    assert (REMUX_TEST_DIR / "1.mp4").exists()
    assert not (REMUX_TEST_DIR / "2.mp4").exists()
    print("test_prune_missing_files_leaves_other_remux_cache_entries_alone: OK")


STAGE_TEST_DIR = Path("/tmp/scan_orch_test_stage_dir")


def _reset_stage_dir():
    import shutil
    if STAGE_TEST_DIR.exists():
        shutil.rmtree(STAGE_TEST_DIR)
    STAGE_TEST_DIR.mkdir(parents=True)


def test_purge_staging_files_deletes_staged_files_and_flips_decisions():
    reset()
    _reset_stage_dir()
    _reset_remux_dir()
    (STAGE_TEST_DIR / "preview_a.mp4").write_text("staged content")
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_decision(conn, 1, "staged")
        deleted = serve_mod.purge_staging_files(conn, STAGE_TEST_DIR, REMUX_TEST_DIR)
        status = conn.execute("SELECT status FROM decisions WHERE preview_id = 1").fetchone()["status"]
    assert deleted == 1, deleted
    assert not (STAGE_TEST_DIR / "preview_a.mp4").exists()
    assert status == "deleted"
    print("test_purge_staging_files_deletes_staged_files_and_flips_decisions: OK")


def test_purge_staging_files_deletes_orphaned_remux_cache_entries_for_staged_videos():
    """The actual gap this guards against: a staged video that was ever
    played through the remux fallback leaves a permanent orphaned
    remux_cache/{id}.mp4 behind once it's purged, unless purging itself
    cleans it up."""
    reset()
    _reset_stage_dir()
    _reset_remux_dir()
    (STAGE_TEST_DIR / "preview_a.mp4").write_text("staged content")
    (REMUX_TEST_DIR / "1.mp4").write_text("cached remux of the staged video")
    with connect(TMP_DB) as conn:
        _seed_video(conn, 1)
        _seed_decision(conn, 1, "staged")
        serve_mod.purge_staging_files(conn, STAGE_TEST_DIR, REMUX_TEST_DIR)
    assert not (REMUX_TEST_DIR / "1.mp4").exists()
    print("test_purge_staging_files_deletes_orphaned_remux_cache_entries_for_staged_videos: OK")


def test_purge_staging_files_leaves_other_remux_cache_entries_alone():
    reset()
    _reset_stage_dir()
    _reset_remux_dir()
    (REMUX_TEST_DIR / "5.mp4").write_text("unrelated, never staged, must survive")
    with connect(TMP_DB) as conn:
        _seed_video(conn, 5)  # never staged
        serve_mod.purge_staging_files(conn, STAGE_TEST_DIR, REMUX_TEST_DIR)
    assert (REMUX_TEST_DIR / "5.mp4").exists()
    print("test_purge_staging_files_leaves_other_remux_cache_entries_alone: OK")


if __name__ == "__main__":
    test_update_scan_run_writes_fields()
    test_update_scan_run_noop_without_run_id()
    test_target_workers_column_exists_after_init_db()
    test_init_db_migration_is_idempotent()
    test_fresh_db_has_set_null_on_matched_candidate_id()
    test_init_db_migrates_stale_matched_candidate_id_fk()
    test_queue_rows_includes_normal_match()
    test_queue_rows_excludes_missing_preview()
    test_queue_rows_falls_back_to_second_best_when_top_candidate_missing()
    test_queue_rows_excludes_preview_when_all_candidates_missing()
    test_queue_rows_excludes_staged_preview()
    test_queue_rows_excludes_rejected_preview()
    test_queue_rows_paginates()
    test_staged_queue_rows_includes_staged_preview_independent_of_matches()
    test_staged_queue_rows_includes_staged_preview_even_when_missing()
    test_staged_queue_rows_excludes_non_staged_decisions()
    test_staged_queue_rows_orders_most_recently_staged_first()
    test_rejected_queue_rows_includes_rejected_preview_independent_of_matches()
    test_rejected_queue_rows_excludes_non_rejected_decisions()
    test_rejected_queue_rows_orders_most_recently_rejected_first()
    test_list_missing_files_excludes_staged()
    test_list_missing_files_includes_rejected_and_undecided()
    test_prune_missing_files_never_deletes_a_staged_video()
    test_prune_missing_files_deletes_genuinely_missing_videos()
    test_prune_missing_files_handles_video_referenced_as_matched_candidate()
    test_prune_missing_files_returns_empty_list_when_nothing_to_prune()
    test_prune_missing_files_deletes_orphaned_remux_cache_entry()
    test_prune_missing_files_leaves_other_remux_cache_entries_alone()
    test_purge_staging_files_deletes_staged_files_and_flips_decisions()
    test_purge_staging_files_deletes_orphaned_remux_cache_entries_for_staged_videos()
    test_purge_staging_files_leaves_other_remux_cache_entries_alone()
    test_queue_page_data_pending_tab()
    test_queue_page_data_staged_tab()
    test_queue_page_data_rejected_tab()
    test_build_cmd_inventory_includes_roots_and_limit()
    test_build_cmd_includes_debug_log_for_inventory_and_fingerprint()
    test_build_cmd_inventory_omits_limit_when_not_set()
    test_build_cmd_match_has_no_roots_or_limit()
    test_build_cmd_fingerprint_includes_workers_when_set()
    test_build_cmd_fingerprint_omits_workers_when_not_set()
    test_build_cmd_match_includes_workers_when_set()
    test_build_cmd_match_omits_workers_when_not_set()
    test_stages_from_none_runs_everything()
    test_stages_from_fingerprint_skips_inventory()
    test_stages_from_match_skips_inventory_and_fingerprint()
    test_stages_from_unknown_stage_runs_everything()
    test_selected_stages_defaults_to_all()
    test_selected_stages_honors_explicit_subset()
    test_selected_stages_keeps_canonical_order_regardless_of_input_order()
    test_resume_skip_composes_with_stage_selection()
    test_resume_plan_carries_forward_live_adjusted_workers()
    test_resume_plan_reduces_limit_by_stage_done()
    test_resume_plan_limit_unset_when_no_limit_in_params()
    test_resume_plan_limit_unset_when_nothing_done_yet()
    test_resume_plan_baseline_accumulates_across_chained_resumes()
    test_resume_plan_limit_reduction_uses_cumulative_done_not_just_this_row()
    test_resume_plan_preserves_started_at_and_start_stage()
    test_resume_plan_match_stage_zeroes_baseline_and_resets_started_at()
    test_resume_plan_match_stage_ignores_chained_resume_baselines_too()
    test_resume_plan_computes_baseline_elapsed_from_active_processing_time()
    test_resume_plan_baseline_elapsed_accumulates_across_chained_resumes()
    test_progress_with_baseline_adds_baseline_to_done_and_total()
    test_progress_with_baseline_eta_available_immediately_on_resume()
    test_progress_with_baseline_rate_blends_baseline_and_fresh_progress()
    test_progress_with_baseline_no_baseline_matches_original_behavior()
    test_progress_with_baseline_none_stage_total_passes_through()
    test_all_descendant_pids_finds_grandchildren()
    TMP_DB.unlink(missing_ok=True)
    print("\nAll scan-orchestration tests passed.")
