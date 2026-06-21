"""
Tests 02_fingerprint.py's _read_target_workers() — the polling function
behind the live-adjustable worker count (see its module docstring's
LIVE-ADJUSTABLE WORKER COUNT section). Doesn't run the actual scheduling
loop or spin up a ProcessPoolExecutor — that part (raising/lowering
concurrency mid-run without killing an in-flight worker) can only be
exercised live, against a real fingerprint run; see README's Tuning
section and CLAUDE.md's Validation status. This covers the pure polling/
clamping logic that loop depends on. Loads 02_fingerprint.py directly via
importlib (its filename starts with a digit, so it can't be `import`ed
normally). Run from project root:

    python3 tests/fingerprint_worker_scaling_test.py
"""

import importlib.util
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from db import connect, init_db  # noqa: E402

spec = importlib.util.spec_from_file_location("fp_worker_mod", PROJECT_ROOT / "src" / "02_fingerprint.py")
fp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp_mod)

TMP_DB = Path("/tmp/fingerprint_worker_scaling_test.db")


def reset():
    TMP_DB.unlink(missing_ok=True)
    init_db(TMP_DB)


def insert_run(run_id=1, target_workers=None):
    with connect(TMP_DB) as conn:
        conn.execute(
            "INSERT INTO scan_runs (id, params_json, status, started_at, updated_at, target_workers) "
            "VALUES (?, '{}', 'running', ?, ?, ?)",
            (run_id, time.time(), time.time(), target_workers),
        )


def test_no_run_id_returns_current_unchanged():
    reset()
    # No scan_runs row at all, and no run_id — must not raise, must not
    # touch `current` (this is the plain-CLI path with no live-adjustment
    # channel at all).
    result = fp_mod._read_target_workers(TMP_DB, None, current=4, hard_max=8)
    assert result == 4
    print("test_no_run_id_returns_current_unchanged: OK")


def test_no_matching_row_returns_current_unchanged():
    reset()
    result = fp_mod._read_target_workers(TMP_DB, 999, current=4, hard_max=8)
    assert result == 4
    print("test_no_matching_row_returns_current_unchanged: OK")


def test_null_target_workers_returns_current_unchanged():
    reset()
    insert_run(run_id=1, target_workers=None)
    result = fp_mod._read_target_workers(TMP_DB, 1, current=4, hard_max=8)
    assert result == 4
    print("test_null_target_workers_returns_current_unchanged: OK")


def test_valid_value_overrides_current():
    reset()
    insert_run(run_id=1, target_workers=6)
    result = fp_mod._read_target_workers(TMP_DB, 1, current=4, hard_max=8)
    assert result == 6
    print("test_valid_value_overrides_current: OK")


def test_value_above_hard_max_is_clamped_down():
    # The actual scenario this guards: --workers 4 default, but a bad/stale
    # DB value tries to push concurrency past what the pool was built for.
    reset()
    insert_run(run_id=1, target_workers=999)
    result = fp_mod._read_target_workers(TMP_DB, 1, current=4, hard_max=8)
    assert result == 8
    print("test_value_above_hard_max_is_clamped_down: OK")


def test_value_below_floor_is_clamped_up():
    # The minimum-2 floor (user-specified requirement) must hold even
    # against a 0/negative value somehow ending up in the DB.
    reset()
    insert_run(run_id=1, target_workers=0)
    result = fp_mod._read_target_workers(TMP_DB, 1, current=4, hard_max=8)
    assert result == 2
    print("test_value_below_floor_is_clamped_up: OK")


if __name__ == "__main__":
    test_no_run_id_returns_current_unchanged()
    test_no_matching_row_returns_current_unchanged()
    test_null_target_workers_returns_current_unchanged()
    test_valid_value_overrides_current()
    test_value_above_hard_max_is_clamped_down()
    test_value_below_floor_is_clamped_up()
    TMP_DB.unlink(missing_ok=True)
    print("\nAll fingerprint-worker-scaling tests passed.")
