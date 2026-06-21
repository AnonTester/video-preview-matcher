"""
Tests procutil.run_with_hard_timeout(): the happy path, a failing
command, and — the actual reason this module exists — that a slow
process is killed and returns None within roughly the requested timeout
rather than hanging. `sleep 30` standing in for a process stuck in an
uninterruptible NFS wait isn't a perfect simulation (sleep dies on
SIGKILL immediately, a real D-state process might not), but it does
prove run_with_hard_timeout's *polling loop itself* never blocks past
`timeout`, which is the actual bug this fixes (subprocess.run's blocking
cleanup wait(), not the kill() call). Run from project root:

    python3 tests/procutil_test.py
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from procutil import run_with_hard_timeout  # noqa: E402


def test_fast_command_succeeds():
    proc = run_with_hard_timeout(["echo", "hello"], timeout=5, stdout=subprocess.PIPE, text=True)
    assert proc is not None
    assert proc.returncode == 0
    assert proc.stdout.strip() == "hello"
    print("test_fast_command_succeeds: OK")


def test_slow_command_times_out_without_hanging():
    start = time.monotonic()
    proc = run_with_hard_timeout(["sleep", "30"], timeout=1, poll_interval=0.1,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = time.monotonic() - start
    assert proc is None
    assert elapsed < 5, f"took {elapsed:.1f}s — should return shortly after the 1s timeout, not hang"
    print(f"test_slow_command_times_out_without_hanging: OK ({elapsed:.2f}s)")


def test_failing_command_reports_nonzero_returncode():
    proc = run_with_hard_timeout(["false"], timeout=5)
    assert proc is not None
    assert proc.returncode != 0
    print("test_failing_command_reports_nonzero_returncode: OK")


def test_chatty_command_does_not_deadlock_on_full_pipe_buffer():
    """The actual production bug: a child that writes more than the OS
    pipe buffer (~64KB on Linux) before exiting blocks on write() forever
    if nothing drains the pipe while it runs — exactly what ffmpeg with
    -loglevel info + showinfo did on real long videos. Print well past
    that here (200000 bytes) with a generous-but-bounded timeout; this
    must complete fast, not stall until the timeout."""
    start = time.monotonic()
    proc = run_with_hard_timeout(
        ["python3", "-c", "import sys; sys.stdout.write('x' * 200000)"],
        timeout=15, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    elapsed = time.monotonic() - start
    assert proc is not None, "should have completed normally, not timed out/deadlocked"
    assert proc.returncode == 0
    assert len(proc.stdout) == 200000, f"got {len(proc.stdout)} bytes, pipe was likely truncated by a deadlock"
    assert elapsed < 5, f"took {elapsed:.1f}s for a trivial command — looks like the old pipe deadlock"
    print(f"test_chatty_command_does_not_deadlock_on_full_pipe_buffer: OK ({elapsed:.2f}s)")


if __name__ == "__main__":
    test_fast_command_succeeds()
    test_slow_command_times_out_without_hanging()
    test_failing_command_reports_nonzero_returncode()
    test_chatty_command_does_not_deadlock_on_full_pipe_buffer()
    print("\nAll procutil tests passed.")
