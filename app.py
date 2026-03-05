"""
IdeaSpark Flask Application
===========================

This module implements a rich idea‑tracking application with a variety of
features to help you capture, organize and review your thoughts.  It
goes well beyond a simple CRUD interface by adding support for:

* **Search and filtering** – search by title or notes, filter by tag or
  mood and show only your favourite ideas.
* **Voting and favourites** – upvote ideas to surface the best ones and
  mark your personal favourites with a star.
* **Comments** – add a running discussion under each idea.
* **Editing** – update an idea’s title, notes, tags and mood via a
  dedicated edit page.
* **Data export** – download all ideas in CSV format for offline use.
* **Trend insights** – view trending tags and a mood distribution chart.

All data is stored in a SQLite database located alongside this file.  On
first run the application automatically creates the necessary tables and
adds any missing columns when upgrading from earlier versions.
"""

import os
import sqlite3
import csv
import io
import json
from datetime import datetime
from collections import Counter, defaultdict
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    jsonify,
    Response,
)

# Application settings
APP_NAME = "IdeaSpark"
BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "ideas.db")

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    """Open a connection to the SQLite database with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create or migrate database tables and columns as needed."""
    conn = get_db()
    # Create the ideas table with newest schema
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            notes TEXT NOT NULL,
            tags TEXT,
            mood INTEGER NOT NULL,
            upvotes INTEGER DEFAULT 0,
            favourite INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    # Create the comments table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (idea_id) REFERENCES ideas (id)
        )
        """
    )
    # Inspect existing columns to add any missing ones when upgrading
    cur = conn.execute("PRAGMA table_info(ideas)")
    existing_cols = {row[1] for row in cur.fetchall()}
    alterations = []
    if "upvotes" not in existing_cols:
        alterations.append("ALTER TABLE ideas ADD COLUMN upvotes INTEGER DEFAULT 0")
    if "favourite" not in existing_cols:
        alterations.append("ALTER TABLE ideas ADD COLUMN favourite INTEGER DEFAULT 0")
    if "archived" not in existing_cols:
        alterations.append("ALTER TABLE ideas ADD COLUMN archived INTEGER DEFAULT 0")
    for sql in alterations:
        conn.execute(sql)
    conn.commit()
    conn.close()


# Initialize database at import time (important for Gunicorn workers)
init_db()


@app.route("/")
def home():
    """
    Display the list of ideas with a rich set of filters and insights.

    Query parameters:
      q: free‑text search (title or notes)
      tag: filter by a specific tag
      mood: filter by a specific mood (1–5)
      sort: sorting criterion (date, upvotes, title, mood, favourite)
      favourite: if "1", show only favourited ideas
    """
    # Retrieve filters from query string
    search_query = (request.args.get("q") or "").strip().lower()
    selected_tag = (request.args.get("tag") or "").strip().lower()
    selected_mood = (request.args.get("mood") or "").strip()
    sort_key = (request.args.get("sort") or "date").strip().lower()
    favourite_only = (request.args.get("favourite") or "0").strip() == "1"

    conn = get_db()
    # Fetch all ideas at once
    ideas = [dict(row) for row in conn.execute("SELECT * FROM ideas").fetchall()]

    # Fetch comments and group them by idea ID
    comments_by_idea: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM comments ORDER BY id ASC").fetchall():
        comments_by_idea[row["idea_id"]].append(dict(row))

    conn.close()

    # Apply filters in Python for flexibility
    filtered_ideas = []
    for idea in ideas:
        # Skip archived ideas
        if idea.get("archived", 0):
            continue
        # Filter by favourite flag
        if favourite_only and idea.get("favourite", 0) != 1:
            continue
        # Filter by tag
        if selected_tag:
            tag_list = [t.strip().lower() for t in (idea.get("tags") or "").split(",") if t.strip()]
            if selected_tag not in tag_list:
                continue
        # Filter by mood
        if selected_mood and selected_mood.isdigit():
            if str(idea.get("mood")) != selected_mood:
                continue
        # Search filter on title and notes
        if search_query:
            if search_query not in idea.get("title", "").lower() and search_query not in idea.get("notes", "").lower():
                continue
        filtered_ideas.append(idea)

    # Sorting
    if sort_key == "upvotes":
        filtered_ideas.sort(key=lambda x: (x.get("upvotes", 0), x["id"]), reverse=True)
    elif sort_key == "title":
        filtered_ideas.sort(key=lambda x: x.get("title", "").lower())
    elif sort_key == "mood":
        filtered_ideas.sort(key=lambda x: (x.get("mood", 0), x["id"]), reverse=True)
    elif sort_key == "favourite":
        filtered_ideas.sort(key=lambda x: (x.get("favourite", 0), x["id"]), reverse=True)
    else:  # date (default)
        filtered_ideas.sort(key=lambda x: x.get("id"), reverse=True)

    # Build list of all unique tags for filter dropdown and trending tag counts
    tag_counts: Counter[str] = Counter()
    for idea in ideas:
        for tag in (idea.get("tags") or "").split(","):
            tag = tag.strip().lower()
            if tag:
                tag_counts[tag] += 1
    all_tags = sorted(tag_counts.keys())
    top_tags = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    # Compute mood distribution counts for chart (1–5)
    mood_counts = {str(i): 0 for i in range(1, 6)}
    for idea in ideas:
        m = str(idea.get("mood", 0))
        if m in mood_counts:
            mood_counts[m] += 1

    # Convert mood counts to JSON for Chart.js
    mood_counts_json = json.dumps(mood_counts)

    return render_template(
        "index.html",
        app_name=APP_NAME,
        ideas=filtered_ideas,
        comments_by_idea=comments_by_idea,
        search_query=search_query,
        selected_tag=selected_tag,
        selected_mood=selected_mood,
        sort_key=sort_key,
        favourite_only=favourite_only,
        all_tags=all_tags,
        top_tags=top_tags,
        mood_counts_json=mood_counts_json,
    )


@app.route("/add", methods=["POST"])
def add_idea() -> Response:
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
        "INSERT INTO ideas(title, notes, tags, mood, upvotes, favourite, archived, created_at) VALUES (?, ?, ?, ?, 0, 0, 0, ?)",
        (title, notes, tags, mood, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/delete/<int:idea_id>", methods=["POST"])
def delete_idea(idea_id: int) -> Response:
    """Delete an idea and its comments."""
    conn = get_db()
    conn.execute("DELETE FROM comments WHERE idea_id = ?", (idea_id,))
    conn.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/upvote/<int:idea_id>", methods=["POST"])
def upvote(idea_id: int) -> Response:
    """Increment the upvote count for an idea."""
    conn = get_db()
    conn.execute("UPDATE ideas SET upvotes = COALESCE(upvotes,0) + 1 WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/toggle_favourite/<int:idea_id>", methods=["POST"])
def toggle_favourite(idea_id: int) -> Response:
    """Toggle the favourite flag for an idea."""
    conn = get_db()
    # Get current value
    cur = conn.execute("SELECT favourite FROM ideas WHERE id = ?", (idea_id,))
    row = cur.fetchone()
    if row:
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE ideas SET favourite = ? WHERE id = ?", (new_val, idea_id))
        conn.commit()
    conn.close()
    return redirect(url_for("home", favourite="1" if new_val else "0"))


@app.route("/comment/<int:idea_id>", methods=["POST"])
def add_comment(idea_id: int) -> Response:
    """Add a new comment to an idea."""
    content = (request.form.get("comment") or "").strip()
    if not content:
        return redirect(url_for("home"))
    conn = get_db()
    conn.execute(
        "INSERT INTO comments(idea_id, content, created_at) VALUES (?, ?, ?)",
        (idea_id, content, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/edit/<int:idea_id>", methods=["GET", "POST"])
def edit_idea(idea_id: int):
    """Edit an existing idea."""
    conn = get_db()
    if request.method == "POST":
        # Save updates
        title = (request.form.get("title") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        tags_raw = (request.form.get("tags") or "").strip()
        mood = int(request.form.get("mood") or 3)
        tags = ",".join([t.strip().lower() for t in tags_raw.split(",") if t.strip()])
        conn.execute(
            "UPDATE ideas SET title = ?, notes = ?, tags = ?, mood = ? WHERE id = ?",
            (title, notes, tags, mood, idea_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("home"))
    else:
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        conn.close()
        if not row:
            return redirect(url_for("home"))
        return render_template("edit.html", app_name=APP_NAME, idea=dict(row))


@app.route("/export")
def export_csv() -> Response:
    """Export all ideas to a CSV file."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM ideas ORDER BY id ASC").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "notes", "tags", "mood", "upvotes", "favourite", "archived", "created_at"])
    for r in rows:
        writer.writerow([
            r["id"],
            r["title"],
            r["notes"],
            r["tags"],
            r["mood"],
            r.get("upvotes", 0),
            r.get("favourite", 0),
            r.get("archived", 0),
            r["created_at"],
        ])
    csv_content = output.getvalue()
    output.close()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ideas.csv"},
    )


@app.route("/api/ideas")
def api_ideas() -> Response:
    """Return all ideas as JSON including upvotes and favourites."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM ideas ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


if __name__ == "__main__":
    # When run directly, ensure DB is initialized and start the development server
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)