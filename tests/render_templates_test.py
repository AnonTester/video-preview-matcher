"""
Render templates against real DB data using raw Jinja2, to validate
template logic without needing FastAPI/uvicorn installed (not available
in this sandbox — no network egress to pip install them). This exercises
the exact same data shapes the route handlers in 04_serve.py build, just
without the HTTP layer around them. Run from project root:

    python3 tests/render_templates_test.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from db import connect

import jinja2

PROJECT_ROOT = Path(__file__).parent.parent
env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROJECT_ROOT / "templates")))
env.filters["tojson"] = lambda v: json.dumps(v)


def build_index_context(db_path):
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                m.preview_id, p.filename AS preview_filename, p.duration_sec AS preview_duration,
                m.candidate_id, c.filename AS candidate_filename, c.duration_sec AS candidate_duration,
                m.visual_score, m.audio_score, m.combined_score,
                d.status AS decision_status
            FROM matches m
            JOIN videos p ON p.id = m.preview_id
            JOIN videos c ON c.id = m.candidate_id
            LEFT JOIN decisions d ON d.preview_id = m.preview_id
            WHERE m.id IN (
                SELECT id FROM matches m2
                WHERE m2.preview_id = m.preview_id
                ORDER BY m2.combined_score DESC LIMIT 1
            )
            ORDER BY (d.status IS NOT NULL) ASC, m.combined_score DESC
            """
        ).fetchall()
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
    return {"matches": [dict(r) for r in rows], "stats": dict(stats), "app_version": "test", "request": None}


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
    }


def main():
    db_path = PROJECT_ROOT / "data" / "library.db"

    print("Rendering index.html ...")
    ctx = build_index_context(db_path)
    tpl = env.get_template("index.html")
    out = tpl.render(**ctx)
    assert "Preview Matcher" in out
    assert str(len(ctx["matches"])) in out or "0 previews" in out
    Path("/tmp/rendered_index.html").write_text(out)
    print(f"  OK — {len(out)} chars, {len(ctx['matches'])} match rows. Saved to /tmp/rendered_index.html")

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
