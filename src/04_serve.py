"""
04_serve.py — Local web UI for reviewing preview/full-video matches.

Run:
    python3 src/04_serve.py --db data/library.db --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000 in a browser.

SAFETY MODEL:
    Nothing is ever hard-deleted by this server directly. "Approve" moves
    the preview file into a `_to_delete/` staging folder next to the
    library root (a rename, not a copy — instant, no extra disk usage)
    and records the decision in the `decisions` table. A separate,
    explicit "empty staging folder" action (also exposed here, but kept
    behind its own confirmation) is what actually unlinks files. This
    two-step structure exists specifically so a misclick during a long
    review session can't destroy data — staged files are easy to grep
    and sanity-check (e.g. `du -sh _to_delete/`) before the final purge.

STREAMING:
    Videos are served with HTTP range-request support so the <video>
    tag can seek without downloading the whole file — important once
    individual files are multiple GB.

    Some real files in this library (likely from scraped/re-muxed
    sources) are unplayable in a browser for one of two independent
    reasons, both detected by _probe_playability() and fixed the same
    way by _ensure_playable() — a cached, re-tagged `-c copy` remux,
    lossless, no re-encode, built once per affected file under
    data/remux_cache/ and reused after that. The original file is never
    modified:

    1. A valid h264/aac bitstream but a missing/zero codec FourCC tag in
       its MP4 sample description. ffmpeg/VLC shrug this off (they fall
       back to extradata), but a browser's native demuxer rejects it
       outright ("no supported source was found"), even though the
       codec itself is fine.
    2. The file's *actual* container, per ffprobe's format_name, isn't
       one a browser's native <video> demuxer understands at all —
       found via video #5855: a file named *.mp4 that's actually raw
       MPEG-TS. Its codec_tag is a real, non-zero MPEG-TS stream_type
       (0x1b for H.264), not the broken-MP4-tag pattern at all, so
       check #1 alone never caught it; the browser still rejects it
       with the same "no supported source" error, since it can't parse
       an MPEG-TS container from a plain <video src> regardless of the
       codec inside being fine.

    matroska/webm are deliberately exempt from *both* checks: ffprobe
    always reports codec_tag=0x0000 for every Matroska-family file (it
    has no FourCC-tag concept at all) — that's normal, not a defect,
    and confirmed live against the real library's actual .mkv/.webm
    files. Treating check #1 as container-agnostic would have flagged
    *every* mkv/webm file in the library as "broken" the first time it
    was ever streamed.

    The remux itself only ever runs when the source is actually h264
    video with aac-or-no audio (true for every real case found in this
    library so far) — _ensure_playable() checks this before remuxing,
    not just before deciding whether to. Forcing the hardcoded
    avc1/mp4a tags onto a different codec (the library does have a
    handful of av1/hevc/vp9 videos) would mislabel it, not fix it —
    actively breaking playback instead of restoring it. When that
    safety check fails, the original file is served as a last resort,
    same as a failed ffmpeg remux attempt.

NOT-A-MATCH FEEDBACK:
    Distinct from the preview-level approve/reject decision, a single
    candidate pairing can be dismissed as a false positive via
    /api/not-a-match/{preview_id}/{candidate_id} (see its docstring below).
    This never touches a file — it only records feedback so the matcher
    doesn't keep re-suggesting a pairing a human already said is wrong.

SCAN ORCHESTRATION (no manual commands required):
    01_inventory.py / 02_fingerprint.py / 03_match.py are launched as
    subprocesses from here, in sequence, instead of requiring someone to
    SSH in and run them by hand. A "scan" is one (inventory -> fingerprint
    -> match) run; progress lives in the scan_runs table (see db.py),
    which the stage scripts themselves update via their --run-id flag —
    04_serve.py never parses subprocess output for progress, it just
    polls the same DB row the UI polls.

    Only one scan runs at a time (SCAN_LOCK + SCAN_STATE below) — starting
    a second one while one is active is rejected, not queued.

    Each stage subprocess is started with start_new_session=True so its
    PID is also its process-group leader; cancel/pause send SIGTERM to the
    whole group (os.killpg), which is what actually reaches 02's
    ProcessPoolExecutor workers and the ffmpeg/fpcalc processes they spawn
    — sending SIGTERM to just the parent PID would orphan those.

    "Pause" and "cancel" are the same mechanism (kill the running stage)
    with a different status string. Resuming just re-runs the same
    stage/params: 01 only re-probes changed files and 02 only fingerprints
    videos with fingerprinted_at IS NULL, so a re-run naturally picks up
    where an interrupted one left off rather than redoing finished work.
    A startup hook marks any row still 'running' as 'interrupted' (its
    orchestrating thread died with the old process, e.g. a container
    restart), so the UI never shows a stale "running" forever.
"""

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import connect, init_db, update_scan_run
from procutil import run_with_hard_timeout

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

APP_DIR = Path(__file__).parent
PROJECT_ROOT = APP_DIR.parent

# Populated in main() from CLI args; module-level so route handlers can reach it
STATE = {"db_path": None, "stage_dir": None}


def _read_version() -> str:
    try:
        return (PROJECT_ROOT / "VERSION").read_text().strip()
    except OSError:
        return "unknown"


APP_VERSION = _read_version()

# Scan orchestration state. Guarded by SCAN_LOCK since route handlers run in
# Starlette's thread pool — two requests (e.g. a double-click on "start")
# can race otherwise. Only one scan at a time: run_id is non-None iff a
# scan is currently active.
SCAN_LOCK = threading.Lock()
SCAN_STATE = {"run_id": None, "proc": None, "stop_status": None}

ALL_STAGES = [
    ("inventory", "01_inventory.py"),
    ("fingerprint", "02_fingerprint.py"),
    ("match", "03_match.py"),
]


def _selected_stages(params: dict) -> list[tuple[str, str]]:
    """The stages to run for this params dict, always in canonical
    (inventory, fingerprint, match) order regardless of how params["stages"]
    lists them — so passing e.g. ["match", "inventory"] still runs
    inventory first, not match-then-inventory. Defaults to all three when
    not given, for backward compatibility with scan_runs rows stored
    before per-stage selection existed."""
    wanted = params.get("stages") or [name for name, _ in ALL_STAGES]
    return [(name, file) for name, file in ALL_STAGES if name in wanted]


@asynccontextmanager
async def lifespan(app: FastAPI):
    if STATE.get("db_path"):
        with connect(STATE["db_path"]) as conn:
            conn.execute(
                "UPDATE scan_runs SET status='interrupted', finished_at=? WHERE status='running'",
                (time.time(),),
            )
    yield


app = FastAPI(title="Preview Matcher Review", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


def get_video_row(conn, video_id: int):
    row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"video {video_id} not found")
    return dict(row)


def _fmt_size(num_bytes) -> str:
    """Human-readable filesize, e.g. '1.2 GB', '340 MB'. Mirrored in
    review.html's JS (fmtSize) for the candidate panel, which re-renders
    without a page reload when switching between match candidates."""
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_duration(seconds) -> str:
    """'1h 15m' / '3m 20s' / '45s' — same convention as index.html's
    fmtDuration (JS) for the queue list, reused here for consistency.
    Mirrored in review.html's JS (fmtDuration) for the candidate panel."""
    if seconds is None:
        return "—"
    seconds = max(0, round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _fmt_video_meta(duration_sec, width, height, size_bytes) -> str:
    """'1h 15m · 1920x1080 · 1.2 GB' for a video panel's card title —
    replaces the filename there (now shown on its own line below,
    split from its folder; see review.html)."""
    resolution = f"{width}x{height}" if width and height else "—"
    return f"{_fmt_duration(duration_sec)} · {resolution} · {_fmt_size(size_bytes)}"


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

PAGE_SIZE = 40

# Shared between queue_rows() and _pending_total() so the two can never
# drift apart (the count must describe exactly the same set the rows are
# a page of). AND (d.status IS NULL OR d.status != 'staged') is what
# keeps a staged preview out of the pending tab once it has one — the
# staged tab (staged_queue_rows() below) is where it lives instead.
_PENDING_FROM_WHERE = """
    FROM matches m
    JOIN videos p ON p.id = m.preview_id
    JOIN videos c ON c.id = m.candidate_id
    LEFT JOIN decisions d ON d.preview_id = m.preview_id
    WHERE p.missing_since IS NULL AND c.missing_since IS NULL
    AND (d.status IS NULL OR d.status != 'staged')
    AND m.id IN (
        SELECT m2.id FROM matches m2
        JOIN videos c2 ON c2.id = m2.candidate_id
        WHERE m2.preview_id = m.preview_id AND c2.missing_since IS NULL
        ORDER BY m2.combined_score DESC LIMIT 1
    )
"""


def _pending_total(conn) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n {_PENDING_FROM_WHERE}").fetchone()["n"]


def _staged_total(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM decisions WHERE status = 'staged'").fetchone()["n"]


def queue_rows(conn, page: int = 1, page_size: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """One row per *pending* preview (not yet staged — see
    staged_queue_rows() for that bucket), with its best match among
    non-missing candidates, ordered worst-decided-first (highest
    confidence and not yet decided floats to the top; a rejected
    preview, having no file consequence either way, just sinks toward
    the bottom rather than getting its own tab). A preview whose
    candidates are *all* missing matches nothing here and is correctly
    absent — a preview with a missing *top* candidate but a viable
    second-best one still surfaces with that one, rather than
    disappearing from the queue just because its #1 match happened to be
    the file that vanished (see db.py's missing_since docstring — nothing
    is ever deleted by this, it's just hidden until a human prunes via
    /api/missing-files/prune or the file reappears on a later scan).
    Returns (rows_for_this_page, total_matching_count) for pagination."""
    page = max(1, page)
    total = _pending_total(conn)
    rows = conn.execute(
        f"""
        SELECT
            m.preview_id, p.filename AS preview_filename, p.duration_sec AS preview_duration,
            m.candidate_id, c.filename AS candidate_filename, c.duration_sec AS candidate_duration,
            m.visual_score, m.audio_score, m.combined_score,
            d.status AS decision_status
        {_PENDING_FROM_WHERE}
        ORDER BY (d.status IS NOT NULL) ASC, m.combined_score DESC
        LIMIT ? OFFSET ?
        """,
        (page_size, (page - 1) * page_size),
    ).fetchall()
    return [dict(r) for r in rows], total


def staged_queue_rows(conn, page: int = 1, page_size: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """Staged previews — read directly from `decisions`, deliberately
    *not* through `matches` the way queue_rows() does. Found necessary
    via a real incident: a preview that's been approved-for-deletion and
    re-scored by a later 03_match.py run can drop below threshold and
    lose its `matches` row entirely (matches is fully recomputed every
    run — see Architecture in CLAUDE.md), and 01_inventory.py naturally
    flags its now-staged-away original path missing on the next scan.
    Either one alone used to make an already-staged preview vanish from
    every view in the UI, with its `decisions` row — the only record of
    where to undo it back to — becoming unreachable through any link.
    Sourcing straight from `decisions` instead means a staged preview
    stays visible and undoable regardless of what matching or inventory
    do later; missing_since is intentionally not checked anywhere here.
    Returns (rows_for_this_page, total_staged_count)."""
    page = max(1, page)
    total = _staged_total(conn)
    rows = conn.execute(
        """
        SELECT d.preview_id, p.filename AS preview_filename, p.duration_sec AS preview_duration,
               d.matched_candidate_id AS candidate_id, c.filename AS candidate_filename,
               c.duration_sec AS candidate_duration, d.decided_at
        FROM decisions d
        JOIN videos p ON p.id = d.preview_id
        LEFT JOIN videos c ON c.id = d.matched_candidate_id
        WHERE d.status = 'staged'
        ORDER BY d.decided_at DESC
        LIMIT ? OFFSET ?
        """,
        (page_size, (page - 1) * page_size),
    ).fetchall()
    return [dict(r) for r in rows], total


@app.get("/")
def index(request: Request, tab: str = "pending", page: int = 1):
    tab = tab if tab in ("pending", "staged") else "pending"
    page = max(1, page)

    with connect(STATE["db_path"]) as conn:
        if tab == "staged":
            matches, total = staged_queue_rows(conn, page)
            pending_count, staged_count = _pending_total(conn), total
        else:
            matches, total = queue_rows(conn, page)
            pending_count, staged_count = total, _staged_total(conn)

        stats = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM videos) AS total_videos,
                (SELECT COUNT(*) FROM videos WHERE fingerprinted_at IS NOT NULL) AS fingerprinted_videos,
                (SELECT COUNT(DISTINCT preview_id) FROM matches) AS previews_with_match,
                (SELECT COUNT(*) FROM decisions WHERE status = 'approved_delete') AS approved,
                (SELECT COUNT(*) FROM decisions WHERE status = 'staged') AS staged,
                (SELECT COUNT(*) FROM decisions WHERE status = 'rejected') AS rejected
            """
        ).fetchone()

    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    return templates.TemplateResponse(request, "index.html", {
        "matches": matches,
        "stats": dict(stats),
        "app_version": APP_VERSION,
        "tab": tab,
        "page": page,
        "total_pages": total_pages,
        "total_count": total,
        "pending_count": pending_count,
        "staged_count": staged_count,
    })


@app.get("/review/{preview_id}")
def review_detail(request: Request, preview_id: int, tab: str = "pending", page: int = 1):
    with connect(STATE["db_path"]) as conn:
        preview = get_video_row(conn, preview_id)
        candidates = conn.execute(
            """
            SELECT m.*, c.filename AS candidate_filename, c.path AS candidate_path,
                   c.duration_sec AS candidate_duration,
                   c.width AS candidate_width, c.height AS candidate_height,
                   c.size_bytes AS candidate_size_bytes,
                   c.missing_since AS candidate_missing_since
            FROM matches m JOIN videos c ON c.id = m.candidate_id
            WHERE m.preview_id = ?
            ORDER BY m.combined_score DESC
            """, (preview_id,)
        ).fetchall()
        decision = conn.execute("SELECT * FROM decisions WHERE preview_id = ?", (preview_id,)).fetchone()
        preview_scene_count = conn.execute(
            "SELECT COUNT(*) AS n FROM scenes WHERE video_id = ?", (preview_id,)
        ).fetchone()["n"]

    candidates_parsed = []
    for c in candidates:
        d = dict(c)
        d["scene_matches"] = json.loads(d["scene_matches_json"]) if d["scene_matches_json"] else []
        d["candidate_meta"] = _fmt_video_meta(d["candidate_duration"], d["candidate_width"], d["candidate_height"],
                                               d["candidate_size_bytes"])
        d["candidate_folder"] = os.path.dirname(d["candidate_path"])
        candidates_parsed.append(d)

    return templates.TemplateResponse(request, "review.html", {
        "preview": preview,
        "preview_meta": _fmt_video_meta(preview["duration_sec"], preview["width"], preview["height"], preview["size_bytes"]),
        "preview_folder": os.path.dirname(preview["path"]),
        "candidates": candidates_parsed,
        "decision": dict(decision) if decision else None,
        "preview_scene_count": preview_scene_count,
        "back_tab": tab if tab in ("pending", "staged") else "pending",
        "back_page": page,
        "app_version": APP_VERSION,
    })


# ---------------------------------------------------------------------------
# Video streaming (range-request aware)
# ---------------------------------------------------------------------------

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

REMUX_PROBE_TIMEOUT = 15
REMUX_TIMEOUT = 600  # stream-copy only (no re-encode), so even multi-GB files over NFS are normally well under this


def _remux_cache_dir() -> Path:
    return Path(STATE["db_path"]).parent / "remux_cache"


def _debug_log_path() -> Path:
    return Path(STATE["db_path"]).parent / "subprocess.log"


# A <video> tag makes many small range requests while loading/seeking, each
# hitting /stream/{video_id} -> _ensure_playable -> this check. Without a
# cache, a perfectly fine file would get re-ffprobed on every single one of
# those requests forever (found via the debug log above: the same already-
# confirmed-clean file logged dozens of identical checks within seconds of
# normal playback). Keyed by mtime so a replaced file is re-checked.
_PLAYABILITY_CACHE: dict[str, tuple[float, dict]] = {}

# format_name families ffprobe reports for containers a browser's native
# <video> demuxer already understands directly — no remux needed even if
# codec_tag looks "broken" (see _probe_playability's docstring for why
# that's expected, not a defect, for the matroska family specifically).
_BROWSER_NATIVE_FORMATS = ("mp4", "matroska", "webm")


def _parse_playability(ffprobe_json: dict) -> dict:
    """Pure decision logic over an already-parsed ffprobe JSON response
    (requested via `-show_entries stream=codec_name,codec_tag,codec_type:
    format=format_name`) — separated from _probe_playability's actual
    subprocess call specifically so this part is unit-testable without
    invoking real ffprobe. Returns {"needs_remux": bool, "video_codec":
    str|None, "audio_codec": str|None}. needs_remux is True for either
    of two independent reasons — see module docstring's STREAMING
    section for the full incident writeup behind each:

    1. A real MP4-family file with a missing/zero codec FourCC tag
       (codec_tag) in its sample description.
    2. The file's *actual* container (format_name) isn't one a browser
       understands at all, regardless of extension or codec_tag — e.g.
       a *.mp4 file that's actually raw MPEG-TS (video #5855).

    matroska/webm are exempt from check #1: ffprobe reports
    codec_tag=0x0000 for every Matroska-family file unconditionally (no
    FourCC-tag concept in that container), so applying check #1
    container-agnostically would flag every single real .mkv/.webm file
    as "broken" — confirmed against this library's actual mkv/webm
    files, none of which need or want a remux.

    video_codec/audio_codec are returned so the caller can verify it's
    actually safe to force the avc1/mp4a tags before remuxing — see
    _safe_to_remux/_ensure_playable."""
    result = {"needs_remux": False, "video_codec": None, "audio_codec": None}
    streams = ffprobe_json.get("streams", [])
    fmt_name = ffprobe_json.get("format", {}).get("format_name", "") or ""
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return result

    result["video_codec"] = video.get("codec_name")
    result["audio_codec"] = audio.get("codec_name") if audio else None
    is_mp4_family = "mp4" in fmt_name
    is_browser_native = any(fam in fmt_name for fam in _BROWSER_NATIVE_FORMATS)
    video_tag = str(video.get("codec_tag") or "").strip()
    broken_mp4_tag = is_mp4_family and video_tag in ("0x0000", "0x0", "0", "")
    result["needs_remux"] = broken_mp4_tag or not is_browser_native
    return result


def _safe_to_remux(info: dict) -> bool:
    """True only when forcing the hardcoded avc1/mp4a tags during remux
    would be correct, not a mislabeling — h264 video with aac-or-no
    audio. This library does have a handful of av1/hevc/vp9 videos;
    tagging one of those as avc1 would break playback, not fix it. See
    _ensure_playable's docstring."""
    return info["video_codec"] == "h264" and info["audio_codec"] in (None, "aac")


def _probe_playability(path: Path) -> dict:
    """One ffprobe call, cached by mtime — see _parse_playability for
    the actual decision logic over its output."""
    key = str(path)
    mtime = path.stat().st_mtime
    cached = _PLAYABILITY_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    out = run_with_hard_timeout(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=codec_name,codec_tag,codec_type:format=format_name",
         "-of", "json", str(path)],
        timeout=REMUX_PROBE_TIMEOUT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        log_path=_debug_log_path(), log_label=f"ffprobe(playability): {path.name}",
    )
    result = {"needs_remux": False, "video_codec": None, "audio_codec": None}
    if out is not None and out.returncode == 0:
        try:
            result = _parse_playability(json.loads(out.stdout))
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass  # can't tell; conservative default (no remux) above

    _PLAYABILITY_CACHE[key] = (mtime, result)
    return result


def _staged_file_path(original_path: Path, preview_id: int) -> Path | None:
    """Resolves a staged preview's actual on-disk location under the
    staging folder — same filename, or the `__{preview_id}` collision
    suffix (see /api/decide's approved_delete branch) — or None if
    neither is present. `videos.path` keeps pointing at the pre-staging
    location (display purposes only; see Architecture in CLAUDE.md), so
    anything that needs to actually open the file post-staging (stream,
    undo) must resolve through here instead of trusting that column.

    Checks the `__{preview_id}`-suffixed name *first*, plain name second
    — not an arbitrary choice. If two staged previews ever share a bare
    filename, only the one that lost the collision at staging time ever
    gets a suffixed file; checking that first means a preview which *did*
    collide is never misresolved to its plain-named sibling's file just
    because that sibling also happens to still be sitting in the staging
    folder (a real bug, caught by this function's own regression test:
    checking plain-name first picked the wrong preview's file whenever
    both were staged at once, e.g. resolving #20 to #10's content)."""
    stage_dir = Path(STATE["stage_dir"])
    candidate = stage_dir / f"{original_path.stem}__{preview_id}{original_path.suffix}"
    if candidate.is_file():
        return candidate
    candidate = stage_dir / original_path.name
    if candidate.is_file():
        return candidate
    return None


def _ensure_playable(path: Path, video_id: int) -> Path:
    """Returns a path safe to stream to a browser: the original file, or a
    cached re-tagged remux of it if the original needs one (see
    _probe_playability). Never modifies the original — the remux is a
    separate file under remux_cache/, built once per affected file and
    reused after that.

    Only actually remuxes when the source is h264 video with aac-or-no
    audio — forcing the hardcoded avc1/mp4a tags onto any other codec
    (this library does have a handful of av1/hevc/vp9 videos) would
    mislabel it, not fix it, breaking playback instead of restoring it.
    Falls back to serving the original untouched when that safety check
    fails, same as a failed ffmpeg remux attempt below."""
    cache_path = _remux_cache_dir() / f"{video_id}.mp4"
    if cache_path.is_file() and cache_path.stat().st_mtime >= path.stat().st_mtime:
        return cache_path

    info = _probe_playability(path)
    if not info["needs_remux"] or not _safe_to_remux(info):
        return path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(".tmp.mp4")
    proc = run_with_hard_timeout(
        ["ffmpeg", "-y", "-v", "error", "-i", str(path),
         "-c", "copy", "-tag:v", "avc1", "-tag:a", "mp4a", "-movflags", "+faststart",
         str(tmp_path)],
        timeout=REMUX_TIMEOUT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        log_path=_debug_log_path(), log_label=f"ffmpeg(remux): {path.name}",
    )
    if proc is None or proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        return path  # fall back to the original; playback may still fail, but we tried
    tmp_path.replace(cache_path)

    return cache_path


@app.get("/stream/{video_id}")
def stream_video(video_id: int, request: Request):
    with connect(STATE["db_path"]) as conn:
        row = get_video_row(conn, video_id)
        decision = conn.execute(
            "SELECT status FROM decisions WHERE preview_id = ?", (video_id,)
        ).fetchone()
    path = Path(row["path"])
    if decision is not None and decision["status"] == "staged":
        # The file was renamed into the staging folder on approval — the
        # `videos.path` row deliberately still shows the original location
        # (see Architecture in CLAUDE.md), so it has to be resolved here,
        # not trusted directly, or playback 404s on every staged preview.
        staged_path = _staged_file_path(path, video_id)
        if staged_path is not None:
            path = staged_path
    if not path.is_file():
        raise HTTPException(404, f"file missing on disk: {path}")

    path = _ensure_playable(path, video_id)
    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    ext_to_mime = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    }
    media_type = ext_to_mime.get(path.suffix.lower(), "application/octet-stream")

    if range_header:
        m = RANGE_RE.match(range_header)
        start = int(m.group(1)) if m and m.group(1) else 0
        end = int(m.group(2)) if m and m.group(2) else file_size - 1
        end = min(end, file_size - 1)
        chunk_size = end - start + 1

        def iterfile():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(chunk_size),
        }
        return StreamingResponse(iterfile(), status_code=206, media_type=media_type, headers=headers)

    def iterfull():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(file_size)}
    return StreamingResponse(iterfull(), media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# Decision actions
# ---------------------------------------------------------------------------

@app.post("/api/decide/{preview_id}")
async def decide(preview_id: int, request: Request):
    """
    body: {"status": "approved_delete" | "rejected", "candidate_id": int|null, "note": str|null}

    "approved_delete" immediately stages the file (renames into _to_delete/)
    — see module docstring for why staging, not direct unlink, happens here.
    """
    body = await request.json()
    status = body.get("status")
    candidate_id = body.get("candidate_id")
    note = body.get("note", "")

    if status not in ("approved_delete", "rejected"):
        raise HTTPException(400, "status must be 'approved_delete' or 'rejected'")

    with connect(STATE["db_path"]) as conn:
        preview = get_video_row(conn, preview_id)
        final_status = status

        if status == "approved_delete":
            src = Path(preview["path"])
            if not src.is_file():
                raise HTTPException(404, f"file already missing: {src}")
            stage_dir = Path(STATE["stage_dir"])
            stage_dir.mkdir(parents=True, exist_ok=True)
            dest = stage_dir / src.name
            # avoid collisions if two previews share a filename across subfolders
            if dest.exists():
                dest = stage_dir / f"{src.stem}__{preview_id}{src.suffix}"
            shutil.move(str(src), str(dest))
            final_status = "staged"

        conn.execute(
            """INSERT INTO decisions (preview_id, status, matched_candidate_id, decided_at, note)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(preview_id) DO UPDATE SET
                   status=excluded.status, matched_candidate_id=excluded.matched_candidate_id,
                   decided_at=excluded.decided_at, note=excluded.note""",
            (preview_id, final_status, candidate_id, time.time(), note),
        )

    return JSONResponse({"ok": True, "status": final_status})


@app.post("/api/not-a-match/{preview_id}/{candidate_id}")
def not_a_match(preview_id: int, candidate_id: int):
    """
    Flags one specific (preview, candidate) pairing as a false positive,
    without deciding the preview as a whole — a preview can have several
    candidates, and one being wrong doesn't say anything about the others.
    Recorded in match_feedback (see db.py) so 03_match.py never resurfaces
    this exact pair on a future re-run. No file operation happens here —
    this is metadata only, never a deletion.
    """
    with connect(STATE["db_path"]) as conn:
        get_video_row(conn, preview_id)
        get_video_row(conn, candidate_id)
        conn.execute(
            """INSERT INTO match_feedback (preview_id, candidate_id, status, decided_at)
               VALUES (?, ?, 'not_a_match', ?)
               ON CONFLICT(preview_id, candidate_id) DO UPDATE SET decided_at=excluded.decided_at""",
            (preview_id, candidate_id, time.time()),
        )
        conn.execute("DELETE FROM matches WHERE preview_id = ? AND candidate_id = ?", (preview_id, candidate_id))

    return JSONResponse({"ok": True})


@app.post("/api/undo/{preview_id}")
def undo(preview_id: int):
    """Move a staged file back to its original location and clear the decision."""
    with connect(STATE["db_path"]) as conn:
        preview = get_video_row(conn, preview_id)
        decision = conn.execute("SELECT * FROM decisions WHERE preview_id = ?", (preview_id,)).fetchone()
        if decision is None:
            raise HTTPException(404, "no decision recorded for this preview")

        if decision["status"] == "staged":
            original_path = Path(preview["path"])
            staged_path = _staged_file_path(original_path, preview_id)
            if staged_path is not None:
                original_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged_path), str(original_path))

        conn.execute("DELETE FROM decisions WHERE preview_id = ?", (preview_id,))

    return JSONResponse({"ok": True})


@app.get("/api/staging-summary")
def staging_summary():
    stage_dir = Path(STATE["stage_dir"])
    if not stage_dir.is_dir():
        return {"file_count": 0, "total_bytes": 0, "path": str(stage_dir)}
    files = list(stage_dir.glob("*"))
    total = sum(f.stat().st_size for f in files if f.is_file())
    return {"file_count": len(files), "total_bytes": total, "path": str(stage_dir)}


def purge_staging_files(conn, stage_dir: Path, remux_dir: Path) -> int:
    """Deletes every file in stage_dir, flips every 'staged' decision to
    'deleted', and cleans up each of those videos' remux_cache/{id}.mp4
    if one exists (see _ensure_playable) — without that last part, a
    staged-then-purged video that was ever played through the remux
    fallback would leave a permanent orphaned copy behind forever, since
    nothing else ever revisits remux_cache/ once a file's gone. The
    remux cleanup is keyed off `decisions` (status='staged' at the
    moment of purge), not off the stage_dir glob — the glob also sweeps
    any stray file with no matching decision row, which has no video_id
    to look up a cache entry by anyway. Returns the count of files
    deleted from stage_dir."""
    deleted = 0
    if stage_dir.is_dir():
        for f in stage_dir.glob("*"):
            if f.is_file():
                f.unlink()
                deleted += 1

    staged_ids = [r["preview_id"] for r in conn.execute(
        "SELECT preview_id FROM decisions WHERE status = 'staged'"
    ).fetchall()]
    conn.execute("UPDATE decisions SET status = 'deleted' WHERE status = 'staged'")

    for vid in staged_ids:
        (remux_dir / f"{vid}.mp4").unlink(missing_ok=True)

    return deleted


@app.post("/api/purge-staging")
async def purge_staging(request: Request):
    """
    Permanently deletes everything in the staging folder. Requires
    {"confirm": "DELETE"} in the body as a deliberate friction point —
    this is the one truly irreversible action in the whole tool. See
    purge_staging_files() for what this deletes.
    """
    body = await request.json()
    if body.get("confirm") != "DELETE":
        raise HTTPException(400, "must confirm with {'confirm': 'DELETE'}")

    with connect(STATE["db_path"]) as conn:
        deleted = purge_staging_files(conn, Path(STATE["stage_dir"]), _remux_cache_dir())

    return JSONResponse({"ok": True, "deleted": deleted})


def list_missing_files(conn) -> list[dict]:
    """Videos rows flagged missing by 01_inventory.py (path not found
    under a scanned library root on the last inventory run — see
    db.py's missing_since docstring). Listed, never auto-deleted; a
    human reviews this list and explicitly prunes via prune_missing_
    files(), same two-step pattern as the staging folder above.

    Excludes anything with a 'staged' decision even if it's flagged
    missing — 01_inventory.py's reconcile_missing() shouldn't ever set
    that combination anymore (see its docstring for the incident this
    caused), but this is the second, independent layer: a staged
    preview's `decisions` row is the only record of where to undo it
    back to, so it must never be listed here, let alone pruned, no
    matter how missing_since ended up set."""
    rows = conn.execute(
        """SELECT v.id, v.path, v.filename, v.missing_since FROM videos v
           LEFT JOIN decisions d ON d.preview_id = v.id
           WHERE v.missing_since IS NOT NULL AND (d.status IS NULL OR d.status != 'staged')
           ORDER BY v.missing_since ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def prune_missing_files(conn, remux_dir: Path) -> list[int]:
    """Permanently deletes the `videos` rows currently flagged missing
    (cascading to their scenes/audio_fp/matches/decisions/match_feedback
    via the existing ON DELETE CASCADE foreign keys — see db.py). This
    never touches the *source* file on disk — these rows already have
    no file backing them (that's what "missing" means); pruning only
    removes the now-meaningless DB history for them.

    Also deletes each pruned video's remux_cache/{id}.mp4 if one exists
    (see _ensure_playable) — without this, a missing video that was
    ever played through the remux fallback before going missing would
    leave a permanent orphaned cache entry behind forever once its row
    (and every other trace of it) is gone.

    Never deletes a video with a 'staged' decision — see
    list_missing_files()'s docstring; same reasoning, same exclusion,
    independently enforced here too, since this is the destructive half.

    Returns the pruned video ids (not just a count)."""
    ids = [r["id"] for r in conn.execute(
        """SELECT id FROM videos WHERE missing_since IS NOT NULL
           AND id NOT IN (SELECT preview_id FROM decisions WHERE status = 'staged')"""
    ).fetchall()]
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", ids)

    for vid in ids:
        (remux_dir / f"{vid}.mp4").unlink(missing_ok=True)

    return ids


@app.get("/api/missing-files")
def missing_files():
    with connect(STATE["db_path"]) as conn:
        return {"files": list_missing_files(conn)}


@app.post("/api/missing-files/prune")
async def prune_missing_files_route(request: Request):
    """
    Requires {"confirm": "DELETE"}, same deliberate-friction pattern as
    /api/purge-staging. See prune_missing_files() for what this deletes
    (and what it deliberately never deletes).
    """
    body = await request.json()
    if body.get("confirm") != "DELETE":
        raise HTTPException(400, "must confirm with {'confirm': 'DELETE'}")

    with connect(STATE["db_path"]) as conn:
        pruned_ids = prune_missing_files(conn, _remux_cache_dir())

    return JSONResponse({"ok": True, "deleted": len(pruned_ids)})


# ---------------------------------------------------------------------------
# Scan orchestration (runs 01/02/03 as subprocesses; see module docstring)
# ---------------------------------------------------------------------------

def _build_cmd(stage_file: str, run_id: int, db_path: str, params: dict) -> list[str]:
    cmd = [sys.executable, str(APP_DIR / stage_file), "--db", db_path, "--run-id", str(run_id)]
    if stage_file in ("01_inventory.py", "02_fingerprint.py"):
        cmd += ["--debug-log", str(Path(db_path).parent / "subprocess.log")]
    if stage_file == "01_inventory.py":
        cmd += params["roots"]
        if params.get("limit"):
            cmd += ["--limit", str(params["limit"])]
    elif stage_file == "02_fingerprint.py":
        if params.get("limit"):
            cmd += ["--limit", str(params["limit"])]
        if params.get("hwaccel"):
            cmd += ["--hwaccel", params["hwaccel"]]
        if params.get("fp_workers"):
            cmd += ["--workers", str(params["fp_workers"])]
    elif stage_file == "03_match.py":
        if params.get("match_workers"):
            cmd += ["--workers", str(params["match_workers"])]
    return cmd


def _run_scan(run_id: int, params: dict, db_path: str, start_stage: str | None = None,
               first_stage_limit: int | None = None):
    """Runs in a background daemon thread (one at a time, see SCAN_LOCK).
    Wrapped so SCAN_STATE is always released and the DB row always reaches
    a terminal status, even on a bug here — otherwise a crash in this
    orchestrator (as opposed to an expected subprocess failure, already
    handled below) would leave the run stuck at 'running' forever, with
    no process left for cancel/pause to even act on."""
    try:
        _run_scan_stages(run_id, params, db_path, start_stage, first_stage_limit)
    except Exception as e:
        with connect(db_path) as conn:
            conn.execute(
                "UPDATE scan_runs SET status='failed', message=?, finished_at=? WHERE id=?",
                (f"orchestrator error: {e}", time.time(), run_id),
            )
    finally:
        with SCAN_LOCK:
            SCAN_STATE["run_id"] = None
            SCAN_STATE["proc"] = None


def _stages_from(stages: list[tuple[str, str]], start_stage: str | None) -> list[tuple[str, str]]:
    """`stages`, minus everything before start_stage — used when resuming a
    run that had already gotten past inventory (or fingerprint) last
    time. Without this, resuming always restarted from inventory, which
    re-walks the library and can discover a fresh batch of new-to-probe
    files; those then crowd out the *specific* files the interrupted
    fingerprint stage still had left to do once 02_fingerprint.py's own
    --limit is applied to the now-larger backlog — reported live as
    "resume found 50 new files instead of finishing the previous 38" (it
    wasn't redoing finished work, it just stopped prioritizing the right
    remaining work). Unknown/missing start_stage just runs `stages`
    unmodified."""
    if not start_stage:
        return stages
    names = [name for name, _ in stages]
    if start_stage not in names:
        return stages
    return stages[names.index(start_stage):]


def _run_scan_stages(run_id: int, params: dict, db_path: str, start_stage: str | None = None,
                      first_stage_limit: int | None = None):
    for i, (stage_name, stage_file) in enumerate(_stages_from(_selected_stages(params), start_stage)):
        stage_params = params
        if i == 0 and first_stage_limit is not None:
            # Only the stage actually being resumed into gets the
            # reduced limit (see _resume_plan()) — a later stage in this
            # same multi-stage run (e.g. fingerprint finishing and the
            # pipeline naturally moving on to match) is a fresh attempt
            # at that stage, not a resume of it, and should get the
            # original --limit, not whatever was left of a *different*
            # stage's budget.
            stage_params = {**params, "limit": first_stage_limit}

        with connect(db_path) as conn:
            if i == 0:
                # resume_baseline_* was already set at row-creation time
                # (by _start_scan, from _resume_plan()) for the very
                # first stage of a resumed run — leave it alone here.
                conn.execute(
                    "UPDATE scan_runs SET stage=?, stage_total=NULL, stage_done=0, "
                    "stage_started_at=?, message=?, updated_at=? WHERE id=?",
                    (stage_name, time.time(), f"starting {stage_name}", time.time(), run_id),
                )
            else:
                # A genuine stage transition (not a resume) has no prior
                # attempt at *this* stage to carry forward — reset to 0
                # so a previous stage's baseline doesn't leak into this
                # one's progress display.
                conn.execute(
                    "UPDATE scan_runs SET stage=?, stage_total=NULL, stage_done=0, stage_started_at=?, "
                    "message=?, updated_at=?, resume_baseline_done=0, resume_baseline_elapsed=0 WHERE id=?",
                    (stage_name, time.time(), f"starting {stage_name}", time.time(), run_id),
                )

        cmd = _build_cmd(stage_file, run_id, db_path, stage_params)
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        with SCAN_LOCK:
            SCAN_STATE["proc"] = proc

        tail = []
        for line in proc.stdout:
            tail.append(line)
            tail = tail[-20:]
        proc.wait()

        with SCAN_LOCK:
            stop_status = SCAN_STATE["stop_status"]

        if stop_status:
            with connect(db_path) as conn:
                conn.execute(
                    "UPDATE scan_runs SET status=?, message=?, finished_at=? WHERE id=?",
                    (stop_status, f"stopped during {stage_name}", time.time(), run_id),
                )
            return

        if proc.returncode != 0:
            with connect(db_path) as conn:
                conn.execute(
                    "UPDATE scan_runs SET status=?, message=?, finished_at=? WHERE id=?",
                    ("failed", ("".join(tail) or f"{stage_file} exited {proc.returncode}")[-2000:],
                     time.time(), run_id),
                )
            return

    with connect(db_path) as conn:
        conn.execute(
            "UPDATE scan_runs SET status='completed', stage=NULL, message=?, finished_at=? WHERE id=?",
            ("scan complete", time.time(), run_id),
        )


def _start_scan(params: dict, start_stage: str | None = None, first_stage_limit: int | None = None,
                 baseline_done: int = 0, baseline_elapsed: float = 0.0, started_at: float | None = None) -> int:
    """first_stage_limit/baseline_done/baseline_elapsed/started_at are
    only ever non-default when resuming — see _resume_plan() for where
    they come from and why a naive resume (just replaying params_json
    unmodified) silently dropped a live worker-count adjustment,
    overshot --limit, reset progress/elapsed display to 0, and showed no
    ETA until this invocation's own first completion despite already
    having plenty of rate data from before the pause."""
    with SCAN_LOCK:
        if SCAN_STATE["run_id"] is not None:
            raise HTTPException(409, "a scan is already running")
        with connect(STATE["db_path"]) as conn:
            cur = conn.execute(
                "INSERT INTO scan_runs (params_json, status, started_at, updated_at, target_workers, "
                "resume_baseline_done, resume_baseline_elapsed) VALUES (?, 'running', ?, ?, ?, ?, ?)",
                (json.dumps(params), started_at or time.time(), time.time(), params.get("fp_workers") or 4,
                 baseline_done, baseline_elapsed),
            )
            run_id = cur.lastrowid
        SCAN_STATE["run_id"] = run_id
        SCAN_STATE["stop_status"] = None
        threading.Thread(
            target=_run_scan, args=(run_id, params, STATE["db_path"], start_stage, first_stage_limit), daemon=True,
        ).start()
    return run_id


def _all_descendant_pids(pid: int) -> list[int]:
    """All descendant PIDs of `pid`, found by walking /proc rather than
    relying on process-group membership. Needed because
    procutil.run_with_hard_timeout starts each ffmpeg/ffprobe/fpcalc call
    with start_new_session=True (so *its own* timeout-triggered kill can
    target just that one call without taking out sibling workers) — which
    means those processes are in their *own* session, not the
    orchestrator's, and a plain killpg(orchestrator_pid) never reaches
    them. Confirmed live: cancelling a scan left 4 ffmpeg processes
    running, orphaned and still holding the GPU, until killed by PID by
    hand. killpg is still tried first below (cheap, catches the common
    case), this is the sweep that actually guarantees the whole tree dies."""
    children_by_parent: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
            # Format: "pid (comm) state ppid ...". comm can itself contain
            # spaces/parens, so split on the *last* ')' to get past it.
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children_by_parent.setdefault(ppid, []).append(int(entry))

    result = []
    frontier = [pid]
    while frontier:
        kids = children_by_parent.get(frontier.pop(), [])
        result.extend(kids)
        frontier.extend(kids)
    return result


def _stop_scan(status: str) -> bool:
    """Send SIGTERM to the active stage's whole process *tree* (not just
    its process group — see _all_descendant_pids), escalating to SIGKILL
    after a grace period for anything still alive."""
    with SCAN_LOCK:
        proc = SCAN_STATE["proc"]
        if proc is None:
            return False
        SCAN_STATE["stop_status"] = status

    # Capture the full tree *before* signalling anything: once the parent
    # dies, its children are reparented (to this container's PID 1) and
    # /proc no longer shows them as descendants of proc.pid — confirmed
    # live, that's exactly how orphaned ffmpeg processes survived a
    # cancel. Killing by this fixed PID list (not re-walking /proc later)
    # is what makes the escalation step still reach them.
    pids = [proc.pid] + _all_descendant_pids(proc.pid)

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _escalate():
        time.sleep(5)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    threading.Thread(target=_escalate, daemon=True).start()
    return True


@app.get("/api/scan/roots")
def scan_roots():
    """Library roots available to select for a scan — whatever LIBRARY_PATHS
    is set to (see 01_inventory.py / docker-compose.yml)."""
    env_val = os.environ.get("LIBRARY_PATHS", "")
    roots = [p for p in env_val.split(":") if p.strip()]
    return {"roots": roots}


def _progress_with_baseline(stage_done, stage_total, stage_started_at, baseline_done, baseline_elapsed, now):
    """Folds resume_baseline_done into stage_done/stage_total so the
    displayed progress is continuous across a pause/interrupt/resume
    instead of restarting at 0 (see _resume_plan()'s docstring point 3).
    There's no separate "baseline total" — every already-completed item
    contributes exactly 1 to both the done *and* total tallies, so the
    same baseline_done count covers both (adding it to stage_total too
    is what reconstructs the *original* intended total, e.g. 12 done +
    188 of this invocation's own --limit-reduced total = 200, matching
    what the user actually asked for — see _resume_plan()'s --limit
    section for why the 188 isn't 200 again).

    ETA's rate is cumulative *done* (display_done, including the
    baseline) over cumulative *active* elapsed time (baseline_elapsed —
    real seconds actually spent processing in prior attempts, not
    wall-clock time including any paused gaps — plus this invocation's
    own elapsed since stage_started_at). Mixing baselined-in done with
    only this invocation's own elapsed (the previous version of this
    function) would make the rate look artificially high; using only
    this invocation's own raw numbers (the version before *that*) meant
    no ETA at all until this invocation's own first completion, even
    though plenty of rate data already existed from before the resume —
    found live: a real --limit 1000 fingerprint run kept showing
    "estimating…" for several minutes after every resume despite having
    hours of prior measured throughput to estimate from. Folding in
    baseline_elapsed fixes both: right after a resume (this invocation's
    own elapsed ≈ 0), the rate is essentially the prior attempts' own
    measured rate; as this invocation accumulates its own data, the rate
    naturally blends toward (and eventually is dominated by) its own
    fresh throughput.
    Returns (display_done, display_total, eta_sec)."""
    baseline_done = baseline_done or 0
    baseline_elapsed = baseline_elapsed or 0
    display_done = (stage_done or 0) + baseline_done
    display_total = (stage_total + baseline_done) if stage_total is not None else None

    eta_sec = None
    if display_total is not None:
        stage_elapsed = max(0.0, now - stage_started_at) if stage_started_at else 0.0
        total_elapsed = baseline_elapsed + stage_elapsed
        rate = display_done / total_elapsed if total_elapsed > 0 else 0
        if rate > 0:
            eta_sec = (display_total - display_done) / rate
    return display_done, display_total, eta_sec


@app.get("/api/scan/status")
def scan_status():
    with connect(STATE["db_path"]) as conn:
        row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"status": "idle"}

    d = dict(row)
    d["params"] = json.loads(d.pop("params_json"))
    now = time.time()
    d["elapsed_sec"] = (now - d["started_at"]) if d["started_at"] else None

    baseline_done = d.pop("resume_baseline_done", None)
    baseline_elapsed = d.pop("resume_baseline_elapsed", None)
    d["stage_done"], d["stage_total"], d["eta_sec"] = _progress_with_baseline(
        d["stage_done"], d["stage_total"], d["stage_started_at"], baseline_done, baseline_elapsed, now,
    )
    # So the scan panel's worker slider knows its own upper bound without
    # hardcoding it client-side — see /api/scan/workers and
    # 02_fingerprint.py's LIVE-ADJUSTABLE WORKER COUNT section.
    d["cpu_count"] = os.cpu_count() or 4
    return d


@app.post("/api/scan/start")
async def scan_start(request: Request):
    """body: {"roots": [str, ...], "limit": int|null, "hwaccel": "none"|"vaapi"|null,
    "stages": ["inventory"|"fingerprint"|"match", ...]|null,
    "fp_workers": int|null, "match_workers": int|null}
    stages defaults to all three (the full pipeline). roots is only
    required/validated when "inventory" is among the selected stages —
    fingerprint-only and match-only runs don't touch the filesystem walk
    at all, so a root selection would be meaningless for them. fp_workers
    sets 02_fingerprint.py's *starting* concurrency (default 4 if omitted)
    — it can still be raised/lowered live afterward via
    /api/scan/workers, see that endpoint and 02_fingerprint.py's module
    docstring. match_workers sets 03_match.py's (static, not live-
    adjustable) worker count."""
    body = await request.json()
    valid_stage_names = [name for name, _ in ALL_STAGES]
    stages = body.get("stages") or valid_stage_names
    invalid_stages = [s for s in stages if s not in valid_stage_names]
    if not stages or invalid_stages:
        raise HTTPException(400, f"invalid stages: {invalid_stages or 'must select at least one'}")

    roots = body.get("roots") or []
    if "inventory" in stages:
        if not roots:
            raise HTTPException(400, "select at least one library root")
        allowed = set(scan_roots()["roots"])
        invalid = [r for r in roots if r not in allowed]
        if invalid:
            raise HTTPException(400, f"not a configured library root: {invalid[0]}")

    hwaccel = body.get("hwaccel") or "none"
    if hwaccel not in ("none", "vaapi"):
        raise HTTPException(400, "hwaccel must be 'none' or 'vaapi'")

    cpu_max = os.cpu_count() or 4
    fp_workers = body.get("fp_workers")
    if fp_workers is not None and not (isinstance(fp_workers, int) and 2 <= fp_workers <= cpu_max):
        raise HTTPException(400, f"fp_workers must be an integer between 2 and {cpu_max}")
    match_workers = body.get("match_workers")
    if match_workers is not None and not (isinstance(match_workers, int) and match_workers >= 1):
        raise HTTPException(400, "match_workers must be a positive integer")

    run_id = _start_scan({
        "roots": roots, "limit": body.get("limit"), "hwaccel": hwaccel, "stages": stages,
        "fp_workers": fp_workers, "match_workers": match_workers,
    })
    return {"ok": True, "run_id": run_id}


@app.post("/api/scan/workers")
async def scan_workers(request: Request):
    """body: {"workers": int}, clamped to [2, cpu_count]. Updates the most
    recent scan_runs row's target_workers — 02_fingerprint.py polls this
    column roughly every 2s and adjusts how many fingerprinting tasks it
    keeps in flight to match, without ever killing one already running
    (see its module docstring). Writable any time a scan row exists, not
    just while the fingerprint stage is the active one — it's harmless to
    set early (e.g. before fingerprinting starts) since it'll just be
    waiting there once that stage begins polling it."""
    body = await request.json()
    workers = body.get("workers")
    cpu_max = os.cpu_count() or 4
    if not (isinstance(workers, int) and 2 <= workers <= cpu_max):
        raise HTTPException(400, f"workers must be an integer between 2 and {cpu_max}")

    with connect(STATE["db_path"]) as conn:
        row = conn.execute("SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(409, "no scan found")
        conn.execute("UPDATE scan_runs SET target_workers = ? WHERE id = ?", (workers, row["id"]))

    return {"ok": True, "workers": workers}


def _resume_plan(row: dict) -> dict:
    """Computes everything needed to correctly resume an interrupted/
    paused/failed/cancelled scan_runs row. Pure function (no I/O, no
    SCAN_LOCK) so it's unit-testable without a real DB/HTTP request —
    see test_resume_plan_* in scan_orchestration_test.py.

    Naively replaying params_json unmodified (the original behavior)
    had three real bugs, all reported live against an actual --limit
    1000 fingerprint run that got interrupted partway through:

    1. **Worker count silently reverting to the scan's original starting
       value.** `/api/scan/workers` only ever updates the *live* row's
       target_workers — it has no way to reach back into params_json,
       which is fixed at the moment the scan was first started. A naive
       resume replays that stale original value (e.g. the default 4),
       discarding a live adjustment made mid-run (e.g. raised to 8).
       Fixed by overwriting params["fp_workers"] with the row's current
       target_workers, which already reflects the last live adjustment
       (or the original starting value, if it was never adjusted).

    2. **--limit overshooting the user's actual intended total.**
       --limit means "(re)probe/fingerprint at most N items this run" —
       01/02 apply it to whatever's currently outstanding (not yet
       probed / not yet fingerprinted), which after a partial run
       already EXCLUDES the items that got done. Naively replaying the
       original --limit unmodified therefore applies a *fresh* N to the
       *remaining* backlog — e.g. resuming a --limit 1000 run that got
       134 done starts a new 1000-item batch on top, for an effective
       total of 1134, not the 1000 the user actually asked for. Fixed by
       reducing the limit by however much of *this stage* was already
       completed *across every resume hop so far* — the cumulative
       baseline_done below, not just this one row's own stage_done (a
       second resume of an already-once-resumed run undercounted this
       by exactly the first resume's own baseline contribution, found
       while writing the test for this). Applied only to the stage being
       resumed into, never to a later stage in the same multi-stage run
       (see _run_scan_stages's first_stage_limit handling), since a
       later stage's own --limit hasn't been touched yet.

    3. **Progress/elapsed/ETA resetting to 0.** Each resume inserts a
       new scan_runs row (so SCAN_STATE/process-tracking stays simple),
       and _run_scan_stages's per-stage reset (stage_total=NULL,
       stage_done=0) runs for *every* stage start, including a resume —
       correct for a genuine stage transition, wrong for continuing the
       same stage. Fixed by carrying the prior attempt's own stage_done
       forward as resume_baseline_done (cumulative across however many
       resume hops occurred — see the `+` below), which scan_status()
       folds back into *both* stage_done and stage_total for display
       (see _progress_with_baseline — there's no separate "baseline
       total" since every already-completed item counts toward both
       tallies equally). started_at is also carried forward unmodified
       so elapsed_sec reflects the whole job, not just this latest
       attempt.

    4. **No ETA shown until this invocation's own first completion,
       despite already having hours of measured throughput from before
       the pause.** _progress_with_baseline used to compute rate from
       only this invocation's own raw stage_done/stage_started_at, so
       right after a resume — before any fresh completions — it had
       nothing to divide by and showed "estimating…" for however long
       the first video took. Fixed by also carrying forward
       resume_baseline_elapsed: real seconds actually spent *processing*
       in the prior attempt (its own finished_at - stage_started_at,
       i.e. wall-clock minus any paused time), accumulated the same way
       as baseline_done. scan_status() then computes rate as cumulative
       done over cumulative *active* elapsed, so immediately after a
       resume the rate starts out as essentially the prior attempt's own
       measured throughput, and naturally blends toward this
       invocation's own fresh numbers as they accumulate.
    """
    params = dict(json.loads(row["params_json"]))

    if row.get("target_workers"):
        params["fp_workers"] = row["target_workers"]

    # Cumulative across every resume hop so far, not just this one row's
    # own stage_done — see point 2 above for the bug this avoids.
    baseline_done = (row.get("resume_baseline_done") or 0) + (row.get("stage_done") or 0)

    # Likewise for active-processing time — see point 4 above. Uses this
    # row's own finished_at (set the moment it was paused/interrupted/
    # failed/cancelled), not "now", since that's when this row's own
    # stage_elapsed clock actually stopped.
    prior_attempt_elapsed = 0.0
    if row.get("stage_started_at") and row.get("finished_at"):
        prior_attempt_elapsed = max(0.0, row["finished_at"] - row["stage_started_at"])
    baseline_elapsed = (row.get("resume_baseline_elapsed") or 0) + prior_attempt_elapsed

    first_stage_limit = None
    if params.get("limit") and baseline_done:
        first_stage_limit = max(0, params["limit"] - baseline_done)

    return {
        "params": params,
        "start_stage": row.get("stage"),
        "first_stage_limit": first_stage_limit,
        "baseline_done": baseline_done,
        "baseline_elapsed": baseline_elapsed,
        "started_at": row.get("started_at"),
    }


@app.post("/api/scan/resume")
def scan_resume():
    """Re-runs the most recent scan's params — valid after it was paused,
    cancelled, interrupted (e.g. a container restart), or failed. 01/02 are
    idempotent (skip already-done files/videos), so this is a real resume,
    not a from-scratch restart. Also resumes from the stage that was
    actually interrupted (skipping earlier ones already finished) rather
    than always restarting at inventory — see _run_scan_stages's
    start_stage docstring for why that matters. See _resume_plan() for
    the worker-count/--limit/progress-continuity fixes applied here."""
    with connect(STATE["db_path"]) as conn:
        row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or row["status"] not in ("paused", "interrupted", "failed", "cancelled"):
        raise HTTPException(400, "no resumable scan found")
    plan = _resume_plan(dict(row))
    run_id = _start_scan(
        plan["params"], start_stage=plan["start_stage"], first_stage_limit=plan["first_stage_limit"],
        baseline_done=plan["baseline_done"], baseline_elapsed=plan["baseline_elapsed"],
        started_at=plan["started_at"],
    )
    return {"ok": True, "run_id": run_id}


@app.post("/api/scan/pause")
def scan_pause():
    if not _stop_scan("paused"):
        raise HTTPException(409, "no scan is currently running")
    return {"ok": True}


@app.post("/api/scan/cancel")
def scan_cancel():
    if not _stop_scan("cancelled"):
        raise HTTPException(409, "no scan is currently running")
    return {"ok": True}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # DB_PATH/STAGE_DIR env vars let the Docker image set sane defaults
    # (see Dockerfile) without baking environment-specific paths into a
    # CMD — an explicit --db/--stage-dir flag still overrides either.
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "data/library.db"), help="SQLite DB path (env: DB_PATH)")
    ap.add_argument("--stage-dir", default=os.environ.get("STAGE_DIR"),
                     help="folder to move approved-for-deletion previews into (default: <project>/_to_delete) (env: STAGE_DIR)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    init_db(args.db)
    STATE["db_path"] = args.db
    STATE["stage_dir"] = args.stage_dir or str(PROJECT_ROOT / "_to_delete")

    print(f"DB: {args.db}")
    print(f"Staging folder for approved deletions: {STATE['stage_dir']}")
    print(f"Open http://{args.host}:{args.port} in your browser")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
