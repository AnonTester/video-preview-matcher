"""
Tests that 03_match.py's dismissed-pair filtering (the match_feedback
table) correctly identifies previously-flagged "not a match" pairs, so a
dismissed false positive doesn't resurrect when thresholds change and the
matcher is re-run. Loads 03_match.py directly via importlib (its filename
starts with a digit, so it can't be `import`ed normally) and exercises the
real load_dismissed_pairs() function against a throwaway SQLite DB. Run
from project root:

    python3 tests/match_feedback_test.py
"""

import importlib.util
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from db import connect, init_db  # noqa: E402

spec = importlib.util.spec_from_file_location("match_mod", PROJECT_ROOT / "src" / "03_match.py")
match_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(match_mod)

TMP_DB = Path("/tmp/match_feedback_test.db")


def reset():
    TMP_DB.unlink(missing_ok=True)
    init_db(TMP_DB)


def test_dismissed_pair_is_excluded():
    reset()
    with connect(TMP_DB) as conn:
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (1, '/a.mp4', 'a.mp4')")
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (2, '/b.mp4', 'b.mp4')")
        conn.execute("INSERT INTO videos (id, path, filename) VALUES (3, '/c.mp4', 'c.mp4')")
        conn.execute(
            "INSERT INTO match_feedback (preview_id, candidate_id, status, decided_at) "
            "VALUES (1, 2, 'not_a_match', ?)",
            (time.time(),),
        )

    with connect(TMP_DB) as conn:
        dismissed = match_mod.load_dismissed_pairs(conn)

    assert (1, 2) in dismissed, "flagged pair should be in the dismissed set"
    assert (1, 3) not in dismissed, "unflagged pair should not be excluded"
    print("test_dismissed_pair_is_excluded: OK")


def test_empty_feedback_table_excludes_nothing():
    reset()
    with connect(TMP_DB) as conn:
        dismissed = match_mod.load_dismissed_pairs(conn)
    assert dismissed == set()
    print("test_empty_feedback_table_excludes_nothing: OK")


if __name__ == "__main__":
    test_dismissed_pair_is_excluded()
    test_empty_feedback_table_excludes_nothing()
    TMP_DB.unlink(missing_ok=True)
    print("\nAll match-feedback tests passed.")
