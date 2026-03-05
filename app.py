import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify

# Application settings
APP_NAME = "IdeaSpark"
DB_PATH = os.path.join(os.path.dirname(__file__), "ideas.db")

app = Flask(__name__)


def get_db():
    """
    Open a connection to the SQLite database.  The connection uses row
    objects to allow name-based column access.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the ideas table if it doesn't already exist."""
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            notes TEXT NOT NULL,
            tags TEXT NOT NULL,
            mood INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

# Initialize the database when the module is imported (important for Gunicorn)
init_db()


@app.route("/")
def home():
    """
    Display the list of ideas.  Supports search via query parameter ``q`` and
    filtering by tag via ``tag``.
    """
    q = (request.args.get("q") or "").strip()
    tag = (request.args.get("tag") or "").strip()

    conn = get_db()
    sql = "SELECT * FROM ideas"
    params = []
    where_clauses = []

    if q:
        where_clauses.append("(title LIKE ? OR notes LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    if tag:
        # Surround tags with commas to avoid partial matches
        where_clauses.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag},%")

    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    sql += " ORDER BY id DESC"
    ideas = conn.execute(sql, params).fetchall()

    # Gather all unique tags from the ideas for the filter dropdown
    all_tags = set()
    for row in conn.execute("SELECT tags FROM ideas").fetchall():
        for t in (row["tags"] or "").split(","):
            t = t.strip()
            if t:
                all_tags.add(t)

    conn.close()
    return render_template(
        "index.html",
        app_name=APP_NAME,
        ideas=ideas,
        q=q,
        tag=tag,
        all_tags=sorted(all_tags, key=str.lower),
    )


@app.route("/add", methods=["POST"])
def add_idea():
    """Handle submission of a new idea from the form."""
    title = (request.form.get("title") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    tags_raw = (request.form.get("tags") or "").strip()
    mood = int(request.form.get("mood") or 3)

    if not title or not notes:
        return redirect(url_for("home"))

    tags = ",".join([t.strip().lower() for t in tags_raw.split(",") if t.strip()])

    conn = get_db()
    conn.execute(
        "INSERT INTO ideas(title, notes, tags, mood, created_at) VALUES (?, ?, ?, ?, ?)",
        (title, notes, tags, mood, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/delete/<int:idea_id>", methods=["POST"])
def delete_idea(idea_id: int):
    """Remove an idea by its ID."""
    conn = get_db()
    conn.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/api/ideas")
def api_ideas():
    """Return all ideas as JSON for API consumers."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM ideas ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


if __name__ == "__main__":
    # Initialize database when running directly
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
