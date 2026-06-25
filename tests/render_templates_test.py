"""
Render templates against real DB data using raw Jinja2, to validate
template logic without needing a real running server. This exercises
the exact same data shapes the route handlers in 04_serve.py build, just
without the HTTP layer around them. Run from project root:

    python3 tests/render_templates_test.py

build_index_context() used to hand-duplicate 04_serve.py's pending-queue
SQL inline (written back when FastAPI wasn't installable in the original
dev sandbox) — it had already drifted out of sync with the real query
(missing the missing_since filtering entirely) by the time tabs/
pagination were added. Now loads 04_serve.py directly via importlib
(same pattern as scan_orchestration_test.py) and calls its real
queue_rows()/staged_queue_rows()/_pending_total()/_staged_total(),
so this can't drift from production behavior again.
"""

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from db import connect

import jinja2

PROJECT_ROOT = Path(__file__).parent.parent
env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROJECT_ROOT / "templates")))
env.filters["tojson"] = lambda v: json.dumps(v)

spec = importlib.util.spec_from_file_location("serve_mod", PROJECT_ROOT / "src" / "04_serve.py")
serve_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve_mod)


def build_index_context(db_path, tab="pending", page=1):
    with connect(db_path) as conn:
        if tab == "staged":
            rows, total = serve_mod.staged_queue_rows(conn, page)
            pending_count, staged_count = serve_mod._pending_total(conn), total
        else:
            rows, total = serve_mod.queue_rows(conn, page)
            pending_count, staged_count = total, serve_mod._staged_total(conn)

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

    import math
    total_pages = max(1, math.ceil(total / serve_mod.PAGE_SIZE))
    return {
        "matches": rows, "stats": dict(stats), "app_version": "test", "request": None,
        "tab": tab, "page": page, "total_pages": total_pages, "total_count": total,
        "pending_count": pending_count, "staged_count": staged_count,
    }


def build_review_context(db_path, preview_id):
    with connect(db_path) as conn:
        preview = dict(conn.execute("SELECT * FROM videos WHERE id = ?", (preview_id,)).fetchone())
        candidates = conn.execute(
            """
            SELECT m.*, c.filename AS candidate_filename, c.path AS candidate_path,
                   c.duration_sec AS candidate_duration,
                   c.width AS candidate_width, c.height AS candidate_height
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
        candidates_parsed.append(d)

    return {
        "preview": preview,
        "candidates": candidates_parsed,
        "decision": dict(decision) if decision else None,
        "preview_scene_count": preview_scene_count,
        "request": None,
        "back_tab": "pending",
        "back_page": 1,
    }


def main():
    db_path = PROJECT_ROOT / "data" / "library.db"

    print("Rendering index.html (pending tab) ...")
    ctx = build_index_context(db_path)
    tpl = env.get_template("index.html")
    out = tpl.render(**ctx)
    assert "Preview Matcher" in out
    assert str(len(ctx["matches"])) in out or "0 previews" in out
    Path("/tmp/rendered_index.html").write_text(out)
    print(f"  OK — {len(out)} chars, {len(ctx['matches'])} match rows. Saved to /tmp/rendered_index.html")

    print("Rendering index.html (staged tab) ...")
    sctx = build_index_context(db_path, tab="staged")
    sout = tpl.render(**sctx)
    assert "Preview Matcher" in sout
    assert "Staged for deletion" in sout
    print(f"  OK — {len(sout)} chars, {len(sctx['matches'])} staged rows.")

    if ctx["matches"]:
        preview_id = ctx["matches"][0]["preview_id"]
        print(f"Rendering review.html for preview_id={preview_id} ...")
        rctx = build_review_context(db_path, preview_id)
        rtpl = env.get_template("review.html")
        rout = rtpl.render(**rctx)
        assert rctx["preview"]["filename"] in rout
        assert "CANDIDATES" in rout
        Path("/tmp/rendered_review.html").write_text(rout)
        print(f"  OK — {len(rout)} chars, {len(rctx['candidates'])} candidates. Saved to /tmp/rendered_review.html")
    else:
        print("  SKIPPED — no matches in DB to render a review page for")

    print("\nAll template renders succeeded.")


if __name__ == "__main__":
    main()
