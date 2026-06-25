"""
Tests 01_inventory.py's scan_library() against multiple library roots,
since a typical deployment scans several separate directories at once
(e.g. full versions, previews, and a temp/staging folder, via the
LIBRARY_PATHS env var). Also covers select_files_to_probe(),
a regression test for a real reported bug: --limit used to cap the raw
candidate list *before* filtering out already-probed files, so since
scan_library() yields files in a stable filesystem order, repeated runs
with the same --limit kept re-selecting the same already-current files
at the front of that order and never made progress through the rest of
the library. Also covers reconcile_missing() and find_move_candidate() —
the pure DB-layer functions behind missing-file detection and size+
duration move recovery (see 01_inventory.py's module docstring). The
actual per-file probe loop that *uses* find_move_candidate() (deciding
whether a newly-discovered path is a move vs. a genuinely new file) isn't
exercised here, same reasoning as the rest of this test suite — it needs
a real ffprobe call, so it's covered live instead; see CLAUDE.md's
Validation status. Loads 01_inventory.py directly via importlib (its
filename starts with a digit, so it can't be `import`ed normally). Run
from project root:

    python3 tests/multi_dir_inventory_test.py
"""

import importlib.util
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("inventory_mod", PROJECT_ROOT / "src" / "01_inventory.py")
inventory_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory_mod)

TMP = Path("/tmp/inventory_multi_dir_test")


def reset():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "root_a").mkdir(parents=True)
    (TMP / "root_b").mkdir(parents=True)


def test_scans_across_multiple_roots():
    reset()
    (TMP / "root_a" / "one.mp4").write_text("x")
    (TMP / "root_a" / "skip.txt").write_text("x")
    (TMP / "root_b" / "two.mkv").write_text("x")

    found = sorted(p.name for p in inventory_mod.scan_library([TMP / "root_a", TMP / "root_b"]))
    assert found == ["one.mp4", "two.mkv"], found
    print("test_scans_across_multiple_roots: OK")


def test_dedupes_overlapping_roots():
    reset()
    (TMP / "root_a" / "one.mp4").write_text("x")

    # same directory passed twice (e.g. a misconfigured LIBRARY_PATHS) should not double-count
    found = list(inventory_mod.scan_library([TMP / "root_a", TMP / "root_a"]))
    assert len(found) == 1, found
    print("test_dedupes_overlapping_roots: OK")


def test_limit_skips_already_current_files_not_just_caps_the_list():
    """The actual bug: 5 already-probed ("old") files sit at the front of
    the candidate list (matching real filesystem traversal order), 3 new
    ones come after. With --limit 2, the old code would just take the
    first 2 candidates (both old, both no-ops) and never reach the new
    ones. select_files_to_probe() must select from the *new* files
    instead, since those are what --limit is meant to bound."""
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)

    old_files = []
    for i in range(5):
        p = TMP / "root_a" / f"old_{i}.mp4"
        p.write_text("x")
        old_files.append(p)

    with inventory_mod.connect(db_path) as conn:
        for p in old_files:
            st = p.stat()
            conn.execute(
                "INSERT INTO videos (path, filename, size_bytes, mtime, probed_at) VALUES (?, ?, ?, ?, ?)",
                (str(p), p.name, st.st_size, st.st_mtime, 0),
            )

    new_files = []
    for i in range(3):
        p = TMP / "root_a" / f"new_{i}.mp4"
        p.write_text("y")
        new_files.append(p)

    all_files = old_files + new_files  # old ones first, as in real traversal order
    with inventory_mod.connect(db_path) as conn:
        todo, deferred = inventory_mod.select_files_to_probe(conn, all_files, limit=2)

    todo_paths = {p for p, _ in todo}
    assert todo_paths.issubset(set(new_files)), f"selected an already-current file: {todo_paths}"
    assert len(todo) == 2, todo
    assert deferred == 1, deferred  # 3 new files need probing, capped to 2, 1 left for next run
    print("test_limit_skips_already_current_files_not_just_caps_the_list: OK")


def test_no_limit_returns_everything_needing_reprobe():
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    files = []
    for i in range(4):
        p = TMP / "root_a" / f"f_{i}.mp4"
        p.write_text("x")
        files.append(p)

    with inventory_mod.connect(db_path) as conn:
        todo, deferred = inventory_mod.select_files_to_probe(conn, files, limit=None)

    assert len(todo) == 4, todo
    assert deferred == 0, deferred
    print("test_no_limit_returns_everything_needing_reprobe: OK")


def _insert_video(conn, path, size_bytes=1000, duration_sec=42.0, missing_since=None):
    cur = conn.execute(
        "INSERT INTO videos (path, filename, size_bytes, duration_sec, mtime, probed_at, missing_since) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(path), Path(path).name, size_bytes, duration_sec, 0, 0, missing_since),
    )
    return cur.lastrowid


def test_reconcile_missing_marks_absent_file_as_missing():
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    p = TMP / "root_a" / "gone.mp4"

    with inventory_mod.connect(db_path) as conn:
        _insert_video(conn, p)
        n_missing, n_recovered = inventory_mod.reconcile_missing(conn, [TMP / "root_a"], present_paths=set())
        row = conn.execute("SELECT missing_since FROM videos WHERE path = ?", (str(p),)).fetchone()

    assert (n_missing, n_recovered) == (1, 0), (n_missing, n_recovered)
    assert row["missing_since"] is not None
    print("test_reconcile_missing_marks_absent_file_as_missing: OK")


def test_reconcile_missing_ignores_paths_outside_scanned_roots():
    """The actual safety property this exists for: a partial scan (e.g.
    one root checked in the web UI) must never flag files under a
    *different*, unscanned root as missing just because this particular
    invocation didn't look at them."""
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    p = TMP / "root_b" / "untouched.mp4"

    with inventory_mod.connect(db_path) as conn:
        _insert_video(conn, p)
        # Only root_a is scanned this run — root_b (where p lives) isn't.
        n_missing, n_recovered = inventory_mod.reconcile_missing(conn, [TMP / "root_a"], present_paths=set())
        row = conn.execute("SELECT missing_since FROM videos WHERE path = ?", (str(p),)).fetchone()

    assert (n_missing, n_recovered) == (0, 0), (n_missing, n_recovered)
    assert row["missing_since"] is None, "file outside the scanned roots must not be flagged missing"
    print("test_reconcile_missing_ignores_paths_outside_scanned_roots: OK")


def test_reconcile_missing_clears_flag_on_reappearance():
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    p = TMP / "root_a" / "back.mp4"

    with inventory_mod.connect(db_path) as conn:
        _insert_video(conn, p, missing_since=12345.0)
        n_missing, n_recovered = inventory_mod.reconcile_missing(conn, [TMP / "root_a"], present_paths={str(p)})
        row = conn.execute("SELECT missing_since FROM videos WHERE path = ?", (str(p),)).fetchone()

    assert (n_missing, n_recovered) == (0, 1), (n_missing, n_recovered)
    assert row["missing_since"] is None
    print("test_reconcile_missing_clears_flag_on_reappearance: OK")


def test_reconcile_missing_leaves_present_unflagged_files_alone():
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    p = TMP / "root_a" / "fine.mp4"

    with inventory_mod.connect(db_path) as conn:
        _insert_video(conn, p, missing_since=None)
        n_missing, n_recovered = inventory_mod.reconcile_missing(conn, [TMP / "root_a"], present_paths={str(p)})
        row = conn.execute("SELECT missing_since FROM videos WHERE path = ?", (str(p),)).fetchone()

    assert (n_missing, n_recovered) == (0, 0), (n_missing, n_recovered)
    assert row["missing_since"] is None
    print("test_reconcile_missing_leaves_present_unflagged_files_alone: OK")


def test_reconcile_missing_never_flags_a_staged_preview():
    """The actual incident this guards against: approving a preview
    moves its file into the (unscanned) staging folder, so its original
    path genuinely won't be in present_paths. Without this exclusion,
    the next inventory run flags it missing, which hides it from the
    queue and exposes its decision row to /api/missing-files/prune —
    losing the only record of where to undo it back to."""
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    p = TMP / "root_a" / "staged.mp4"

    with inventory_mod.connect(db_path) as conn:
        vid = _insert_video(conn, p)
        conn.execute(
            "INSERT INTO decisions (preview_id, status, decided_at) VALUES (?, 'staged', ?)",
            (vid, 12345.0),
        )
        # The file is gone from present_paths, exactly as if it had been
        # moved to the staging folder and this run never saw it.
        n_missing, n_recovered = inventory_mod.reconcile_missing(conn, [TMP / "root_a"], present_paths=set())
        row = conn.execute("SELECT missing_since FROM videos WHERE path = ?", (str(p),)).fetchone()

    assert n_missing == 0, n_missing
    assert row["missing_since"] is None, "a staged preview must never be flagged missing"
    print("test_reconcile_missing_never_flags_a_staged_preview: OK")


def test_reconcile_missing_self_heals_an_already_mis_flagged_staged_preview():
    """Covers upgrading from a version without the above exclusion: a
    row that's already (wrongly) flagged missing from a past run must
    get healed the moment a staged decision is found for it, without
    needing a manual DB repair."""
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    p = TMP / "root_a" / "already_flagged.mp4"

    with inventory_mod.connect(db_path) as conn:
        vid = _insert_video(conn, p, missing_since=99999.0)
        conn.execute(
            "INSERT INTO decisions (preview_id, status, decided_at) VALUES (?, 'staged', ?)",
            (vid, 12345.0),
        )
        n_missing, n_recovered = inventory_mod.reconcile_missing(conn, [TMP / "root_a"], present_paths=set())
        row = conn.execute("SELECT missing_since FROM videos WHERE path = ?", (str(p),)).fetchone()

    assert n_recovered == 1, n_recovered
    assert row["missing_since"] is None
    print("test_reconcile_missing_self_heals_an_already_mis_flagged_staged_preview: OK")


def test_reconcile_missing_still_flags_a_rejected_preview():
    """A 'rejected' decision never moves a file, so a rejected preview
    going missing is still real signal — only 'staged' is exempt."""
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)
    p = TMP / "root_a" / "rejected.mp4"

    with inventory_mod.connect(db_path) as conn:
        vid = _insert_video(conn, p)
        conn.execute(
            "INSERT INTO decisions (preview_id, status, decided_at) VALUES (?, 'rejected', ?)",
            (vid, 12345.0),
        )
        n_missing, n_recovered = inventory_mod.reconcile_missing(conn, [TMP / "root_a"], present_paths=set())
        row = conn.execute("SELECT missing_since FROM videos WHERE path = ?", (str(p),)).fetchone()

    assert n_missing == 1, n_missing
    assert row["missing_since"] is not None
    print("test_reconcile_missing_still_flags_a_rejected_preview: OK")


def test_find_move_candidate_matches_unique_missing_row():
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)

    with inventory_mod.connect(db_path) as conn:
        vid = _insert_video(conn, TMP / "root_a" / "old_name.mp4", size_bytes=555, duration_sec=33.3, missing_since=1.0)
        found = inventory_mod.find_move_candidate(conn, size_bytes=555, duration_sec=33.3)

    assert found == vid, (found, vid)
    print("test_find_move_candidate_matches_unique_missing_row: OK")


def test_find_move_candidate_returns_none_when_no_match():
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)

    with inventory_mod.connect(db_path) as conn:
        _insert_video(conn, TMP / "root_a" / "old_name.mp4", size_bytes=555, duration_sec=33.3, missing_since=1.0)
        found = inventory_mod.find_move_candidate(conn, size_bytes=999, duration_sec=10.0)

    assert found is None, found
    print("test_find_move_candidate_returns_none_when_no_match: OK")


def test_find_move_candidate_returns_none_when_ambiguous():
    """Two missing rows sharing the same size+duration must not be
    guessed at — attaching one file's fingerprints/review history to the
    wrong file is worse than just re-fingerprinting an actual new file."""
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)

    with inventory_mod.connect(db_path) as conn:
        _insert_video(conn, TMP / "root_a" / "a.mp4", size_bytes=555, duration_sec=33.3, missing_since=1.0)
        _insert_video(conn, TMP / "root_a" / "b.mp4", size_bytes=555, duration_sec=33.3, missing_since=2.0)
        found = inventory_mod.find_move_candidate(conn, size_bytes=555, duration_sec=33.3)

    assert found is None, found
    print("test_find_move_candidate_returns_none_when_ambiguous: OK")


def test_find_move_candidate_ignores_non_missing_rows():
    """A row with matching size+duration that isn't actually flagged
    missing can't be a move source — it's just a coincidence, or
    (more likely in a real library) two unrelated files of the same
    length."""
    reset()
    db_path = TMP / "test.db"
    inventory_mod.init_db(db_path)

    with inventory_mod.connect(db_path) as conn:
        _insert_video(conn, TMP / "root_a" / "unrelated.mp4", size_bytes=555, duration_sec=33.3, missing_since=None)
        found = inventory_mod.find_move_candidate(conn, size_bytes=555, duration_sec=33.3)

    assert found is None, found
    print("test_find_move_candidate_ignores_non_missing_rows: OK")


if __name__ == "__main__":
    test_scans_across_multiple_roots()
    test_dedupes_overlapping_roots()
    test_limit_skips_already_current_files_not_just_caps_the_list()
    test_no_limit_returns_everything_needing_reprobe()
    test_reconcile_missing_marks_absent_file_as_missing()
    test_reconcile_missing_ignores_paths_outside_scanned_roots()
    test_reconcile_missing_clears_flag_on_reappearance()
    test_reconcile_missing_leaves_present_unflagged_files_alone()
    test_reconcile_missing_never_flags_a_staged_preview()
    test_reconcile_missing_self_heals_an_already_mis_flagged_staged_preview()
    test_reconcile_missing_still_flags_a_rejected_preview()
    test_find_move_candidate_matches_unique_missing_row()
    test_find_move_candidate_returns_none_when_no_match()
    test_find_move_candidate_returns_none_when_ambiguous()
    test_find_move_candidate_ignores_non_missing_rows()
    shutil.rmtree(TMP)
    print("\nAll multi-directory inventory tests passed.")
