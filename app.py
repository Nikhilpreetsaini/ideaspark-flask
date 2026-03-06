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
from datetime import datetime, timedelta
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
import random

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
            -- Optional due date for the idea (ISO yyyy-mm-dd).  Allows scheduling and deadline reminders.
            due_date TEXT,
            upvotes INTEGER DEFAULT 0,
            favourite INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            -- Track the progress of an idea (Not Started, In Progress, Completed)
            status TEXT DEFAULT 'Not Started',
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
    # Add a status column when upgrading from older schemas
    if "status" not in existing_cols:
        alterations.append("ALTER TABLE ideas ADD COLUMN status TEXT DEFAULT 'Not Started'")
    # Add a due_date column when upgrading from older schemas
    if "due_date" not in existing_cols:
        alterations.append("ALTER TABLE ideas ADD COLUMN due_date TEXT")

    # Add a priority column when upgrading from older schemas.  This allows
    # users to set the urgency of an idea (Low, Medium, High).  The default
    # priority is 'Medium'.  Older databases will get this new column via
    # ALTER TABLE when the app starts.
    if "priority" not in existing_cols:
        alterations.append("ALTER TABLE ideas ADD COLUMN priority TEXT DEFAULT 'Medium'")

    for sql in alterations:
        conn.execute(sql)

    # Create the tasks table if it does not exist.  Each task is tied to a
    # parent idea and can be marked complete.  Tasks support simple project
    # breakdowns and progress tracking for each idea.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            completed INTEGER DEFAULT 0,
            created TEXT DEFAULT (DATETIME('now')),
            FOREIGN KEY (idea_id) REFERENCES ideas(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


# Initialize database at import time (important for Gunicorn workers)
init_db()

# --------------------------
# Curiosity / Prompt engine
# --------------------------

# A bank of curiosity prompts to generate daily or roulette ideas. These are intentionally
# thought‑provoking to spur creative thinking and can be expanded freely. They are used by
# the API endpoints and the Intrigue Mode on the frontend.
PROMPT_BANK = [
    "What would you build if failure was impossible?",
    "What’s a problem you secretly want to solve because it annoys you daily?",
    "If you had to earn ₹1 lakh in 7 days ethically, what’s your first move?",
    "What is one thing you can simplify by 10x for beginners?",
    "What would your ‘future self’ beg you to start today?",
    "What’s a feature everyone hates… but still uses? Why?",
    "If your idea had to work without an app, how would it still work?",
    "What tiny habit would compound massively in 90 days?",
    "What’s the most unfair advantage you can create for yourself this month?",
    "What’s the fastest way to test your idea with real people in 24 hours?",
    "Turn your biggest fear into a product: what is it?",
    "What would you build for your parents to make their life easier?",
]


def _seed_for_today() -> int:
    """Return a deterministic integer seed based on the current UTC date.

    This makes the daily prompt consistent for a given day no matter where the server runs.
    """
    d = datetime.utcnow().date().isoformat()
    return sum(ord(c) for c in d)


def get_daily_prompt() -> str:
    """Select a prompt from the bank based on today’s seed."""
    seed = _seed_for_today()
    return PROMPT_BANK[seed % len(PROMPT_BANK)]


# ---------------------------------------------------------------------------
# Badge system
#
# To encourage continued use of the app and celebrate milestones, we expose a
# simple badge system.  The more ideas you collect, the higher your badge
# rank becomes.  This can motivate users to reach the next level and adds a
# fun gamified touch to the interface.  The badge names deliberately grow
# from small to epic.

def badge_for_count(count: int) -> str | None:
    """Return a badge label based on the number of active ideas.

    Badges are awarded at 5, 10, 20 and 50+ ideas.  If the count is below
    the first threshold, no badge is returned (None).
    """
    if count >= 50:
        return "Master Innovator"
    elif count >= 20:
        return "Visionary"
    elif count >= 10:
        return "Builder"
    elif count >= 5:
        return "Novice"
    else:
        return None


# ---------------------------------------------------------------------------
# Achievements system
#
# In addition to badges for idea counts, we award achievements for overall
# engagement across upvotes, comments and favourites. These badges
# celebrate different patterns of use and provide extra goals for users.
def achievements_for_stats(
    total_ideas: int, total_upvotes: int, total_comments: int, total_favourites: int
) -> list[str]:
    """Return a list of achievement labels based on usage statistics.

    Achievements are granted at the following milestones:

    * Upvotes: 10 (Supporter), 50 (Super Fan)
    * Comments: 5 (Commentator), 20 (Conversationalist)
    * Favourites: 5 (Taste Maker), 15 (Super Taste)

    Additional achievements can be added here in future updates.
    """
    achievements: list[str] = []
    if total_upvotes >= 50:
        achievements.append("Super Fan")
    elif total_upvotes >= 10:
        achievements.append("Supporter")
    if total_comments >= 20:
        achievements.append("Conversationalist")
    elif total_comments >= 5:
        achievements.append("Commentator")
    if total_favourites >= 15:
        achievements.append("Super Taste")
    elif total_favourites >= 5:
        achievements.append("Taste Maker")
    return achievements


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
    # Filter by priority (Low, Medium, High).  An empty string means no filter.
    selected_priority = (request.args.get("priority") or "").strip().title()
    # Determine if the board view is requested.  Use ?view=board to display ideas
    # grouped by status instead of the standard list.  Any other value falls back
    # to list view.
    board_view = (request.args.get("view") or "").strip().lower() == "board"
    # Optional filter: only show ideas due within the next 7 days
    due_filter = (request.args.get("due") or "").strip().lower()
    favourite_only = (request.args.get("favourite") or "0").strip() == "1"
    show_archived = (request.args.get("archived") or "0").strip() == "1"

    conn = get_db()
    # Fetch all ideas at once
    ideas = [dict(row) for row in conn.execute("SELECT * FROM ideas").fetchall()]

    # Fetch all tasks and group them by idea for easy lookup.  This allows us
    # to display checklists and compute progress bars in the UI.  We'll also
    # build a progress dictionary with counts of completed vs total tasks.
    tasks_by_idea: dict[int, list[sqlite3.Row]] = defaultdict(list)
    progress_by_idea: dict[int, tuple[int, int]] = {}
    for t in conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall():
        tid = t["idea_id"]
        tasks_by_idea[tid].append(dict(t))
    for idea_id, tasks in tasks_by_idea.items():
        total = len(tasks)
        completed = sum(1 for t in tasks if t.get("completed", 0))
        progress_by_idea[idea_id] = (completed, total)

    # Fetch comments and group them by idea ID
    comments_by_idea: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM comments ORDER BY id ASC").fetchall():
        comments_by_idea[row["idea_id"]].append(dict(row))

    conn.close()

    # Apply filters in Python for flexibility
    filtered_ideas = []
    today_date = datetime.utcnow().date()
    for idea in ideas:
        # Skip archived ideas
        if idea.get("archived", 0) and not show_archived:
            continue
        # Filter by favourite flag
        if favourite_only and idea.get("favourite", 0) != 1:
            continue
        # Filter by priority
        if selected_priority:
            if idea.get("priority", "").title() != selected_priority:
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
        # Due soon filter
        if due_filter == "soon":
            due = idea.get("due_date")
            if not due:
                continue
            try:
                due_dt = datetime.fromisoformat(due).date()
            except Exception:
                continue
            if not (today_date <= due_dt <= today_date + timedelta(days=7)):
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
    elif sort_key == "due":
        # Sort by due date ascending; None dates go last
        filtered_ideas.sort(key=lambda x: ((x.get("due_date") or "9999-12-31"), x["id"]))
    elif sort_key == "status":
        order = {"Not Started": 0, "In Progress": 1, "Completed": 2}
        filtered_ideas.sort(key=lambda x: (order.get(x.get("status"), 0), x["id"]))
    elif sort_key == "priority":
        order = {"High": 0, "Medium": 1, "Low": 2}
        filtered_ideas.sort(key=lambda x: (order.get(x.get("priority", "Medium"), 1), x["id"]))
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

    # Compute a bar chart for the most common tags.  We'll keep the top
    # five tags and their counts to visualize tag usage across all ideas.
    tag_counts_list = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

    # Compute a tag cloud from all tags.  The cloud uses the entire
    # tag_counts mapping and assigns a relative size to each tag based
    # on its frequency.  The template will scale font sizes using
    # max_tag_count below.  If no tags exist, default max_tag_count to 1
    # to avoid division by zero.
    tag_cloud = list(tag_counts.items())
    max_tag_count = max(tag_counts.values()) if tag_counts else 1

    # Compute simple word frequency across titles and notes to provide
    # search suggestions.  Remove very short or common stopwords to keep
    # suggestions meaningful.  You can expand the stopwords list as needed.
    stopwords = {
        "the", "and", "for", "with", "that", "this", "there", "their", "have", "has", "are",
        "you", "your", "what", "which", "when", "where", "how", "why", "from", "into", "each",
        "will", "would", "could", "should", "can", "all", "not", "but", "out", "our", "about",
        "idea", "ideas", "notes", "title", "build", "make", "just",
    }
    word_freq: Counter[str] = Counter()
    for idea in ideas:
        text = f"{idea.get('title', '')} {idea.get('notes', '')}"
        # Replace non‑letters with space and split
        words = [w.lower() for w in ''.join(ch if ch.isalnum() else ' ' for ch in text).split()]
        for w in words:
            if len(w) < 3 or w in stopwords:
                continue
            word_freq[w] += 1
    top_words = [w for w, _ in word_freq.most_common(10)]

    # Count ideas that are due within the next two days (overdue items are also shown).
    due_soon_count = 0
    for idea in ideas:
        due = idea.get("due_date")
        if not due:
            continue
        try:
            due_dt = datetime.fromisoformat(due).date()
        except Exception:
            continue
        if due_dt <= today_date + timedelta(days=2):
            due_soon_count += 1

    # Determine badge based on the number of ideas shown
    badge_label = badge_for_count(len(filtered_ideas))

    # Compute overall statistics across all ideas (not just filtered) for the stats card
    total_ideas = len(ideas)
    total_upvotes = sum(int(it.get("upvotes", 0) or 0) for it in ideas)
    total_comments = sum(len(comments_by_idea.get(it["id"], [])) for it in ideas)
    total_favourites = sum(1 for it in ideas if it.get("favourite", 0) == 1)
    stats = {
        "ideas": total_ideas,
        "upvotes": total_upvotes,
        "comments": total_comments,
        "favourites": total_favourites,
    }
    # Derive achievements based on stats
    achievements = achievements_for_stats(total_ideas, total_upvotes, total_comments, total_favourites)

    return render_template(
        "index.html",
        app_name=APP_NAME,
        ideas=filtered_ideas,
        comments_by_idea=comments_by_idea,
        tasks_by_idea=tasks_by_idea,
        progress_by_idea=progress_by_idea,
        search_query=search_query,
        selected_tag=selected_tag,
        selected_mood=selected_mood,
        selected_priority=selected_priority,
        sort_key=sort_key,
        favourite_only=favourite_only,
        all_tags=all_tags,
        top_tags=top_tags,
        tag_counts_list=tag_counts_list,
        tag_cloud=tag_cloud,
        max_tag_count=max_tag_count,
        mood_counts_json=mood_counts_json,
        badge_label=badge_label,
        stats=stats,
        achievements=achievements,
        datetime=datetime,
        show_archived=show_archived,
        due_filter=due_filter,
        board_view=board_view,
        top_words=top_words,
        due_soon_count=due_soon_count,
    )


@app.route("/add", methods=["POST"])
def add_idea() -> Response:
    """Handle submission of a new idea from the form."""
    title = (request.form.get("title") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    tags_raw = (request.form.get("tags") or "").strip()
    mood = int(request.form.get("mood") or 3)
    # Priority value: Low, Medium or High (default is Medium)
    priority = (request.form.get("priority") or "Medium").strip().title()
    if priority not in {"Low", "Medium", "High"}:
        priority = "Medium"
    # Due date can be empty or a YYYY‑MM‑DD string.  Leave None if not provided.
    due_date_raw = (request.form.get("due_date") or "").strip()
    due_date = due_date_raw if due_date_raw else None
    if not title or not notes:
        return redirect(url_for("home"))
    tags = ",".join([t.strip().lower() for t in tags_raw.split(",") if t.strip()])
    conn = get_db()
    # Note: specify columns explicitly so that new columns (e.g. due_date) are populated correctly.
    conn.execute(
        "INSERT INTO ideas (title, notes, tags, mood, due_date, upvotes, favourite, archived, status, priority, created_at) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 'Not Started', ?, ?)",
        (title, notes, tags, mood, due_date, priority, datetime.utcnow().isoformat(timespec="seconds")),
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


@app.route("/archive/<int:idea_id>", methods=["POST"])
def toggle_archive(idea_id: int) -> Response:
    """Toggle the archived flag for an idea.

    When archived, an idea is hidden from the default listing but still stored
    in the database.  Users can unarchive it later to bring it back.
    """
    conn = get_db()
    cur = conn.execute("SELECT archived FROM ideas WHERE id = ?", (idea_id,))
    row = cur.fetchone()
    if row:
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE ideas SET archived = ? WHERE id = ?", (new_val, idea_id))
        conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/duplicate/<int:idea_id>", methods=["POST"])
def duplicate_idea(idea_id: int) -> Response:
    """Create a duplicate of an existing idea with '(copy)' appended to the title.

    The new idea copies the title, notes, tags, mood, due_date and status but
    resets upvotes, favourite and archived flags.  It is stamped with the
    current timestamp.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT title, notes, tags, mood, due_date, status FROM ideas WHERE id = ?",
        (idea_id,),
    ).fetchone()
    if row:
        now = datetime.utcnow().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO ideas (title, notes, tags, mood, due_date, upvotes, favourite, archived, status, created_at) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?)",
            (
                f"{row['title']} (copy)",
                row["notes"],
                row["tags"],
                row["mood"],
                row["due_date"],
                row["status"],
                now,
            ),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("home"))


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
        # Due date may be updated; allow empty string to clear
        due_date_raw = (request.form.get("due_date") or "").strip()
        new_due_date = due_date_raw if due_date_raw else None
        # Read status if provided, otherwise keep existing
        new_status = (request.form.get("status") or "").strip()
        allowed_status = {"Not Started", "In Progress", "Completed"}
        if not new_status or new_status not in allowed_status:
            # fetch current status from DB
            cur = conn.execute("SELECT status FROM ideas WHERE id = ?", (idea_id,))
            row = cur.fetchone()
            new_status = row[0] if row else "Not Started"
        # Read priority if provided, otherwise keep existing
        new_priority = (request.form.get("priority") or "").strip().title()
        allowed_priorities = {"Low", "Medium", "High"}
        if new_priority not in allowed_priorities:
            cur = conn.execute("SELECT priority FROM ideas WHERE id = ?", (idea_id,))
            r = cur.fetchone()
            new_priority = r[0] if r else "Medium"
        tags = ",".join([t.strip().lower() for t in tags_raw.split(",") if t.strip()])
        conn.execute(
            "UPDATE ideas SET title = ?, notes = ?, tags = ?, mood = ?, due_date = ?, status = ?, priority = ? WHERE id = ?",
            (title, notes, tags, mood, new_due_date, new_status, new_priority, idea_id),
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
    writer.writerow([
        "id",
        "title",
        "notes",
        "tags",
        "mood",
        "due_date",
        "upvotes",
        "favourite",
        "archived",
        "status",
        "created_at",
    ])
    for r in rows:
        writer.writerow([
            r["id"],
            r["title"],
            r["notes"],
            r["tags"],
            r["mood"],
            r.get("due_date"),
            r.get("upvotes", 0),
            r.get("favourite", 0),
            r.get("archived", 0),
            r.get("status"),
            r["created_at"],
        ])
    csv_content = output.getvalue()
    output.close()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ideas.csv"},
    )


@app.route("/export.json")
def export_json() -> Response:
    """Export all ideas as JSON for offline use or API consumption."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM ideas ORDER BY id ASC").fetchall()
    conn.close()
    payload = json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=ideas.json"},
    )


@app.route("/api/prompt")
def api_prompt() -> Response:
    """Return the daily curiosity prompt as JSON."""
    return jsonify({"prompt": get_daily_prompt(), "date_utc": datetime.utcnow().date().isoformat()})


@app.route("/api/spotlight")
def api_spotlight() -> Response:
    """Return a spotlight idea: highest upvotes or most recent active idea."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM ideas WHERE archived = 0 ORDER BY COALESCE(upvotes,0) DESC, id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return jsonify({"idea": dict(row) if row else None})

# ---------------------------------------------------------------------------
# Additional data APIs

@app.route("/api/top_ideas")
def api_top_ideas() -> Response:
    """Return a list of the top ideas ranked by engagement.

    Each entry contains id, title, upvote count, comment count, favourite flag
    and completion ratio.  The list is sorted by a weighted score combining
    upvotes, favourites, comments and task completion.  Only non‑archived
    ideas are considered.  The top 5 results are returned.
    """
    conn = get_db()
    # Fetch active ideas
    ideas = [dict(row) for row in conn.execute("SELECT * FROM ideas WHERE archived = 0").fetchall()]
    # Comment counts per idea
    comment_counts: Counter[int] = Counter()
    for row in conn.execute("SELECT idea_id FROM comments").fetchall():
        comment_counts[row[0]] += 1
    # Task completion per idea: completed, total
    tasks_by: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for row in conn.execute("SELECT idea_id, completed FROM tasks").fetchall():
        idea_id, completed = row
        tasks_by[idea_id][1] += 1
        if completed:
            tasks_by[idea_id][0] += 1
    conn.close()
    ranked: list[tuple[float, dict]] = []
    for it in ideas:
        completed, total = tasks_by.get(it["id"], [0, 0])
        completion = (completed / total) if total > 0 else 0.0
        comments = comment_counts.get(it["id"], 0)
        upvotes = int(it.get("upvotes", 0) or 0)
        fav = int(it.get("favourite", 0) or 0)
        # Weighted score: upvotes*2 + favourites*1 + comments*1 + completion
        score = upvotes * 2 + fav * 1 + comments * 1 + completion
        ranked.append((score, {
            "id": it["id"],
            "title": it["title"],
            "upvotes": upvotes,
            "comments": comments,
            "favourites": fav,
            "completion": completion,
        }))
    # Sort by score descending then id descending
    ranked.sort(key=lambda x: (x[0], x[1]["id"]), reverse=True)
    top = [entry for _, entry in ranked[:5]]
    return jsonify(top)


@app.route("/api/random_mashup")
def api_random_mashup() -> Response:
    """Return a random mashup of two ideas.

    Two random active ideas are selected and merged to form a new idea.  The
    title is built by combining parts of each title, the notes are joined
    with a separator, tags are merged and deduplicated, the mood is the
    rounded average of the two moods, and the due date is carried over
    from whichever idea has a due date (preferring the first).  Priority
    defaults to Medium.  If fewer than two active ideas exist, an error
    is returned.
    """
    conn = get_db()
    ideas = [dict(row) for row in conn.execute("SELECT * FROM ideas WHERE archived = 0").fetchall()]
    conn.close()
    if len(ideas) < 2:
        return jsonify({"error": "Not enough ideas to mashup"}), 400
    a, b = random.sample(ideas, 2)
    # Split titles into words
    words_a = a.get("title", "").split()
    words_b = b.get("title", "").split()
    half_a = words_a[: max(1, len(words_a) // 2)]
    half_b = words_b[len(words_b) // 2 :]
    new_title = " ".join(half_a + half_b)
    if not new_title.strip():
        new_title = f"{a.get('title', '')} & {b.get('title', '')}"
    # Combine notes with separator
    notes_a = a.get("notes", "")
    notes_b = b.get("notes", "")
    new_notes = (notes_a + "\n\n---\n\n" + notes_b).strip()
    # Merge tags and deduplicate
    tags_a = [t.strip() for t in (a.get("tags") or "").split(",") if t.strip()]
    tags_b = [t.strip() for t in (b.get("tags") or "").split(",") if t.strip()]
    tags_merged = sorted(set(tags_a + tags_b))
    # Average mood
    try:
        mood_avg = round((int(a.get("mood", 3)) + int(b.get("mood", 3))) / 2)
    except Exception:
        mood_avg = 3
    # Choose due date (prefer due date of first idea, fallback to second)
    due_date = a.get("due_date") or b.get("due_date")
    result = {
        "title": new_title,
        "notes": new_notes,
        "tags": ", ".join(tags_merged),
        "mood": mood_avg,
        "due_date": due_date,
        "priority": "Medium",
    }
    return jsonify(result)

@app.route("/export.md")
def export_markdown() -> Response:
    """Export all ideas to a Markdown file with comments."""
    conn = get_db()
    ideas = conn.execute("SELECT * FROM ideas ORDER BY id ASC").fetchall()
    # Load all comments and group by idea
    comments_by: dict[int, list] = defaultdict(list)
    for c in conn.execute("SELECT * FROM comments ORDER BY id ASC").fetchall():
        comments_by[c["idea_id"]].append(dict(c))
    conn.close()
    lines: list[str] = []
    for row in ideas:
        r = dict(row)
        lines.append(f"## Idea {r['id']}: {r['title']}")
        lines.append("")
        lines.append(f"- **Notes:** {r['notes']}")
        if r.get('tags'):
            lines.append(f"- **Tags:** {r['tags']}")
        lines.append(f"- **Mood:** {r['mood']}/5")
        lines.append(f"- **Due Date:** {r.get('due_date') or 'N/A'}")
        lines.append(f"- **Status:** {r.get('status')}")
        lines.append(f"- **Upvotes:** {r.get('upvotes', 0)}")
        lines.append(f"- **Favourite:** {'Yes' if r.get('favourite') else 'No'}")
        lines.append(f"- **Archived:** {'Yes' if r.get('archived') else 'No'}")
        lines.append(f"- **Created At:** {r['created_at']}")
        # Comments
        if comments_by.get(r['id']):
            lines.append("- **Comments:**")
            for c in comments_by[r['id']]:
                lines.append(f"  - ({c['created_at']}) {c['content']}")
        lines.append("")
    payload = "\n".join(lines)
    return Response(
        payload,
        mimetype="text/markdown",
        headers={"Content-Disposition": "attachment; filename=ideas.md"},
    )

@app.route("/api/status_counts")
def api_status_counts() -> Response:
    """Return counts of idea statuses for a pie chart."""
    conn = get_db()
    rows = conn.execute("SELECT status FROM ideas WHERE archived = 0").fetchall()
    conn.close()
    counts = {"Not Started": 0, "In Progress": 0, "Completed": 0}
    for row in rows:
        st = row[0] or "Not Started"
        counts[st] = counts.get(st, 0) + 1
    return jsonify(counts)

@app.route("/comment/edit/<int:comment_id>", methods=["GET", "POST"])
def edit_comment(comment_id: int):
    """Edit an existing comment."""
    conn = get_db()
    if request.method == "POST":
        content = (request.form.get("content") or "").strip()
        if content:
            conn.execute("UPDATE comments SET content = ? WHERE id = ?", (content, comment_id))
            conn.commit()
        # Redirect back to home (ideally we would redirect to the idea page)
        conn.close()
        return redirect(url_for("home"))
    else:
        row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        conn.close()
        if not row:
            return redirect(url_for("home"))
        return render_template("edit_comment.html", app_name=APP_NAME, comment=dict(row))

@app.route("/comment/delete/<int:comment_id>", methods=["POST"]) 
def delete_comment(comment_id: int) -> Response:
    """Delete a comment by its ID."""
    conn = get_db()
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/api/ideas")
def api_ideas() -> Response:
    """Return all ideas as JSON including upvotes and favourites."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM ideas ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


# ---------------------------------------------------------------------------
# Action plan generator
#
# For each idea we can generate a simple roadmap to turn the concept into
# reality.  The plan outlines a target audience, high‑level steps, and a
# one‑day experiment.  This is intentionally basic and can be extended
# further in future updates (for example by incorporating AI suggestions).

@app.route("/api/plan/<int:idea_id>")
def api_plan(idea_id: int) -> Response:
    """Return a basic action plan for the given idea as JSON.

    The plan includes a title, a short overview, an audience description,
    a list of steps and a one‑day test suggestion.  If the idea does not
    exist, a 404 JSON error is returned.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Idea not found"}), 404
    idea = dict(row)
    title = idea.get("title", "Your idea")
    # Audience based on tags
    tags = [t.strip() for t in (idea.get("tags") or "").split(",") if t.strip()]
    audience = f"people interested in {', '.join(tags)}" if tags else "a general audience"
    overview = f"This plan helps you turn '{title}' into a real product."  # basic overview
    steps = [
        "Write down your core problem and solution in one paragraph.",
        "Create a simple prototype or mock‑up using tools you know (paper, slides or software).",
        "Show the prototype to 3–5 members of your target audience and gather feedback."
    ]
    test = (
        "Within a day, share your idea on social media or with friends and observe how they respond."
    )
    return jsonify({
        "title": title,
        "overview": overview,
        "audience": audience.capitalize(),
        "steps": steps,
        "test": test,
    })


# ---------------------------------------------------------------------------
# Additional API endpoints

@app.route("/api/stats")
def api_stats() -> Response:
    """Return aggregate statistics across all active ideas as JSON.

    Statistics include counts of ideas, total upvotes, total comments and
    total favourites. Archived ideas are excluded from these tallies.
    """
    conn = get_db()
    # Select active ideas only
    ideas = [dict(row) for row in conn.execute("SELECT * FROM ideas WHERE archived = 0").fetchall()]
    # Comments count
    comment_counts = Counter()
    for row in conn.execute("SELECT idea_id FROM comments").fetchall():
        comment_counts[row[0]] += 1
    conn.close()
    total_ideas = len(ideas)
    total_upvotes = sum(int(it.get("upvotes", 0) or 0) for it in ideas)
    total_comments = sum(comment_counts.get(it["id"], 0) for it in ideas)
    total_favourites = sum(1 for it in ideas if it.get("favourite", 0) == 1)
    return jsonify({
        "ideas": total_ideas,
        "upvotes": total_upvotes,
        "comments": total_comments,
        "favourites": total_favourites,
    })


@app.route("/api/timeline")
def api_timeline() -> Response:
    """Return counts of ideas per day for the last 7 days (UTC) as JSON.

    The response is a list of objects with `date` (ISO YYYY-MM-DD) and `count` keys.
    Only non-archived ideas are counted.
    """
    today = datetime.utcnow().date()
    start_date = today - timedelta(days=6)
    # Prepare dictionary with zero counts
    date_counts = { (start_date + timedelta(days=i)).isoformat(): 0 for i in range(7) }
    conn = get_db()
    rows = conn.execute("SELECT created_at FROM ideas WHERE archived = 0").fetchall()
    conn.close()
    for row in rows:
        ts = row[0]
        try:
            d = datetime.fromisoformat(ts).date()
        except Exception:
            continue
        if start_date <= d <= today:
            iso = d.isoformat()
            date_counts[iso] += 1
    # Convert to sorted list
    timeline = [ { "date": iso, "count": date_counts[iso] } for iso in sorted(date_counts.keys()) ]
    return jsonify(timeline)


@app.route("/status/<int:idea_id>", methods=["POST"])
def update_status(idea_id: int) -> Response:
    """Update the status of an idea.

    Accepts a `status` value from the POST body and updates the corresponding
    record. The status must be one of: Not Started, In Progress or Completed.
    """
    new_status = (request.form.get("status") or "").strip()
    allowed = {"Not Started", "In Progress", "Completed"}
    if new_status not in allowed:
        return redirect(url_for("home"))
    conn = get_db()
    conn.execute("UPDATE ideas SET status = ? WHERE id = ?", (new_status, idea_id))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# Task management routes
#
# Tasks allow users to break an idea into smaller actionable items.  Users can
# add tasks, mark them complete or delete them.  Progress is displayed on
# each idea card as a progress bar.

@app.route("/task/add/<int:idea_id>", methods=["POST"])
def add_task(idea_id: int) -> Response:
    """Add a new task to the specified idea."""
    text = (request.form.get("task") or "").strip()
    if not text:
        return redirect(request.referrer or url_for("home"))
    conn = get_db()
    # Ensure the idea exists
    r = conn.execute("SELECT id FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    if not r:
        conn.close()
        return redirect(url_for("home"))
    conn.execute(
        "INSERT INTO tasks (idea_id, text, completed, created) VALUES (?, ?, 0, ?)",
        (idea_id, text, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("home"))


@app.route("/task/complete/<int:task_id>", methods=["POST"])
def toggle_task(task_id: int) -> Response:
    """Toggle the completion state of a task."""
    conn = get_db()
    # Fetch current state and idea ID for redirection after update
    cur = conn.execute("SELECT completed, idea_id FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return redirect(url_for("home"))
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE tasks SET completed = ? WHERE id = ?", (new_val, task_id))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("home"))


@app.route("/task/delete/<int:task_id>", methods=["POST"])
def delete_task(task_id: int) -> Response:
    """Delete a task."""
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("home"))


if __name__ == "__main__":
    # When run directly, ensure DB is initialized and start the development server
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)