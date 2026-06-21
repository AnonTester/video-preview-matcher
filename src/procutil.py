"""
procutil.py — subprocess helper that can't itself hang on an
uninterruptible (D-state) child, unlike subprocess.run(..., timeout=N),
plus optional debug logging of every ffmpeg/ffprobe/fpcalc invocation.

THE D-STATE RISK (defended against here, but NOT what the original bug
report turned out to be — see below):
    subprocess.run(cmd, timeout=N) and Popen.communicate(timeout=N) both
    call Popen.kill() when the timeout fires, then — in the same except
    block, before re-raising TimeoutExpired — call a BLOCKING, un-timed
    Popen.wait() to reap the child. If that child is stuck in an
    uninterruptible kernel sleep (D state), which a process reading from
    a slow/unresponsive NFS share can be even *after* SIGKILL is
    delivered (the signal is pending but can't be handled until the
    in-flight I/O resolves), that final wait() blocks for as long as the
    I/O does — which, on a `hard`-mounted NFS share (no fixed retry
    bound by design, since `hard` prioritizes not silently losing data
    over availability), can be far longer than the timeout you asked
    for. This is real, documented Python behavior, worth guarding
    against regardless — but it was NOT what actually caused the
    production hang that motivated this file (see below), so don't
    assume a future "looks frozen" report is automatically this.

THE FIX (for the D-state risk):
    Poll Popen.wait(timeout=<small>) in a loop instead of one
    big-timeout wait. Each poll uses a non-blocking waitpid(WNOHANG)
    under the hood (confirmed by reading CPython's actual Popen._wait
    source — worth re-checking if upgrading Python, since this relies on
    that implementation detail), so it can never itself block past that
    small interval, even against a D-state child. Once the real budget
    is exceeded, kill() and return immediately *without* waiting again —
    the process may linger as a zombie/orphan until its I/O actually
    resolves, but that no longer blocks the caller.

WHAT THE FIRST PRODUCTION HANG ACTUALLY WAS:
    A single library file appeared to freeze 01_inventory.py's progress
    display for minutes. Direct manual ffprobe on the exact same file,
    run while the scan was still "stuck", returned in well under a
    second — proving ffprobe itself was never the problem. The real bug:
    the main loop's db connection held an UNCOMMITTED write transaction
    open for the whole loop (only committing periodically), which
    blocked update_scan_run()'s own short-lived connections past their
    busy_timeout — so progress display froze on the last message that
    happened to land before the lock was taken, while the actual
    ffprobe/insert work kept going underneath the whole time. Fixed by
    committing after every item instead of periodically (see
    01_inventory.py / 02_fingerprint.py's main loops and db.py's
    update_scan_run docstring).

WHAT A SECOND, WORSE HANG ACTUALLY WAS (this file's own bug):
    This module itself had a textbook subprocess deadlock: the wait loop
    below polled Popen.wait() but never read proc.stdout/proc.stderr
    until *after* the process had already exited. ffmpeg with
    `-loglevel info` plus the `showinfo` filter (one line of stats per
    frame that passes) can write more than the OS pipe buffer (~64KB on
    Linux) before exiting on a video with many scenes — once that
    buffer fills, the child blocks on write() and can NEVER exit, because
    nothing was draining the pipe to make room. Confirmed live: four
    "stuck" ffmpeg processes were all in synchronous-IO sleep
    (`S`, wchan `pipe_write` or `futex_wait_queue`), GPU at ~1% busy,
    their CPU-time totals not moving at all even minutes apart — not
    slow, not D-state, just deadlocked on their own log output. This
    likely explains some of the earlier "needs a longer timeout for long
    videos" cases too (see CHANGELOG/02_fingerprint.py) — scaling the
    timeout up just meant waiting longer to hit the same deadlock, not
    fixing it. Fixed by draining stdout/stderr continuously in background
    threads for the lifetime of the child, instead of only after wait()
    succeeds.
    **The lesson, twice over now: a frozen display or a frozen process
    doesn't tell you *why* on its own** — check for live child processes
    (`docker top`), their actual states (`/proc/<pid>/stat` 3rd field,
    `/proc/<pid>/wchan`), GPU/CPU utilization, and whether their CPU-time
    totals are advancing between two checks a few minutes apart, before
    deciding whether it's slow, deadlocked, or genuinely stuck.

DEBUG LOGGING:
    Pass log_path to append one line per call (timestamp, command,
    target file, duration, exit status) to a plain text file, plus the
    full stdout/stderr on failure or timeout (truncated, to bound size).
    Off by default — meant to be turned on when actually debugging a
    "what is it doing" report, not left on permanently across a
    multi-thousand-file scan.
"""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

MAX_LOGGED_OUTPUT = 4000  # chars per stream, to bound log file growth on a chatty/failing call


def _drain(pipe, chunks: list):
    """Runs in a background thread for the lifetime of the child. Reads
    until EOF (pipe closes when the process exits or is killed) so the OS
    pipe buffer never fills up and blocks the child's write() — see
    module docstring's second hang writeup for why this exists."""
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (ValueError, OSError):
        pass  # pipe closed out from under us (process killed) — fine, just stop
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _log_call(log_path, label: str, cmd: list[str], elapsed: float,
              returncode: int | None, stdout: str | bytes | None, stderr: str | bytes | None,
              timed_out: bool):
    try:
        status = "TIMEOUT" if timed_out else f"rc={returncode}"
        line = [f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {label} ({elapsed:.2f}s, {status})", f"  cmd: {' '.join(cmd)}"]
        for name, data in (("stdout", stdout), ("stderr", stderr)):
            if not data:
                continue
            text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
            text = text.strip()
            if text:
                if len(text) > MAX_LOGGED_OUTPUT:
                    text = text[:MAX_LOGGED_OUTPUT] + f"... [truncated, {len(text)} chars total]"
                line.append(f"  {name}: {text}")
        with open(log_path, "a") as f:
            f.write("\n".join(line) + "\n")
    except OSError:
        pass  # logging is a debugging aid, never let it break the actual work


def run_with_hard_timeout(cmd: list[str], timeout: float, poll_interval: float = 0.5,
                           log_path: str | Path | None = None, log_label: str | None = None,
                           **popen_kwargs) -> subprocess.CompletedProcess | None:
    """Like subprocess.run(cmd, timeout=timeout), but guaranteed to
    return within roughly `timeout` seconds even if the child ends up
    stuck in an uninterruptible sleep. Returns None on timeout instead
    of raising — callers already handle subprocess.TimeoutExpired as a
    "this one failed" case, so None fits the same call pattern.

    If log_path is given, appends one entry to it for this call (see
    module docstring's DEBUG LOGGING). log_label identifies the call in
    the log (e.g. the target filename) — defaults to cmd[0] (the
    program name) if not given, which is a lot less useful, so callers
    that care about debuggability should pass one."""
    start = time.monotonic()
    proc = subprocess.Popen(cmd, start_new_session=True, **popen_kwargs)
    is_text = bool(popen_kwargs.get("text") or popen_kwargs.get("universal_newlines") or popen_kwargs.get("encoding"))

    # Drain stdout/stderr continuously for the child's whole lifetime —
    # NOT just after it exits. Without this, a chatty child (ffmpeg with
    # -loglevel info + showinfo can write well past the ~64KB OS pipe
    # buffer on a long video) blocks on write() forever once that buffer
    # fills, since nothing is reading it to make room — a real deadlock
    # this hit in production (see module docstring's second hang writeup).
    stdout_chunks: list = []
    stderr_chunks: list = []
    readers = []
    if proc.stdout:
        t = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks), daemon=True)
        t.start()
        readers.append(t)
    if proc.stderr:
        t = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
        t.start()
        readers.append(t)

    timed_out = False
    while True:
        try:
            proc.wait(timeout=poll_interval)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() - start >= timeout:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break

    # The process has exited (or been killed) by this point, so its end of
    # each pipe is closed — the reader threads see EOF and finish almost
    # immediately. The join timeout is just a safety net, not expected to
    # ever actually fire.
    for t in readers:
        t.join(timeout=5)

    empty = "" if is_text else b""
    joiner = "".join if is_text else b"".join
    stdout = joiner(stdout_chunks) if proc.stdout else None
    stderr = joiner(stderr_chunks) if proc.stderr else None
    if stdout is not None and not stdout:
        stdout = empty
    if stderr is not None and not stderr:
        stderr = empty

    if log_path:
        _log_call(log_path, log_label or cmd[0], cmd, time.monotonic() - start,
                   None if timed_out else proc.returncode, stdout, stderr, timed_out)

    if timed_out:
        return None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
