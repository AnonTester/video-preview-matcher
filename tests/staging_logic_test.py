"""
Tests the stage/undo file-move logic from 04_serve.py's decide()/undo()
handlers in isolation, without needing FastAPI loaded. Verifies:
  - approving a match renames (not copies) the file into the stage dir
  - filename collisions in the stage dir are handled
  - undo restores the file to its original location
"""

import shutil
import sys
from pathlib import Path

TMP = Path("/tmp/stage_test")


def reset():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "library").mkdir(parents=True)
    (TMP / "_to_delete").mkdir(parents=True)


def simulate_approve(src: Path, stage_dir: Path, preview_id: int) -> Path:
    dest = stage_dir / src.name
    if dest.exists():
        dest = stage_dir / f"{src.stem}__{preview_id}{src.suffix}"
    shutil.move(str(src), str(dest))
    return dest


def simulate_undo(original_path: Path, stage_dir: Path, preview_id: int):
    staged_path = stage_dir / original_path.name
    if not staged_path.is_file():
        staged_path = stage_dir / f"{original_path.stem}__{preview_id}{original_path.suffix}"
    if staged_path.is_file():
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_path), str(original_path))


def test_basic_stage_and_undo():
    reset()
    lib = TMP / "library"
    stage = TMP / "_to_delete"

    src = lib / "preview_a.mp4"
    src.write_text("fake video bytes")

    dest = simulate_approve(src, stage, preview_id=1)
    assert not src.exists(), "original should be gone after move"
    assert dest.exists() and dest.name == "preview_a.mp4", "should land in stage dir with same name"

    simulate_undo(src, stage, preview_id=1)
    assert src.exists(), "should be restored after undo"
    assert not dest.exists(), "should no longer be in stage dir after undo"
    print("test_basic_stage_and_undo: OK")


def test_collision_handling():
    reset()
    lib = TMP / "library"
    stage = TMP / "_to_delete"

    # Two different previews with the SAME filename in different subfolders
    (lib / "sub1").mkdir()
    (lib / "sub2").mkdir()
    src1 = lib / "sub1" / "preview.mp4"
    src2 = lib / "sub2" / "preview.mp4"
    src1.write_text("video one")
    src2.write_text("video two")

    dest1 = simulate_approve(src1, stage, preview_id=10)
    dest2 = simulate_approve(src2, stage, preview_id=20)

    assert dest1.name == "preview.mp4"
    assert dest2.name == "preview__20.mp4", f"expected collision-safe rename, got {dest2.name}"
    assert dest1.read_text() == "video one"
    assert dest2.read_text() == "video two"

    # undo both, verify each goes back to its own original path with correct content
    simulate_undo(src1, stage, preview_id=10)
    simulate_undo(src2, stage, preview_id=20)
    assert src1.read_text() == "video one"
    assert src2.read_text() == "video two"
    print("test_collision_handling: OK")


def test_undo_when_nothing_staged_is_noop():
    reset()
    lib = TMP / "library"
    stage = TMP / "_to_delete"
    src = lib / "never_staged.mp4"
    src.write_text("untouched")

    # undo without a prior approve should not crash or destroy the file
    simulate_undo(src, stage, preview_id=5)
    assert src.read_text() == "untouched"
    print("test_undo_when_nothing_staged_is_noop: OK")


if __name__ == "__main__":
    test_basic_stage_and_undo()
    test_collision_handling()
    test_undo_when_nothing_staged_is_noop()
    shutil.rmtree(TMP)
    print("\nAll staging logic tests passed.")
