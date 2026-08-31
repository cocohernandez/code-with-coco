#!/usr/bin/env python3
"""
Daily content manager for your video archive.

Watches a drop folder, transcribes anything new with Amazon Transcribe (reusing
the logic in transcribe.py), and files the result in a local SQLite database so
you can grep years of footage in milliseconds.

Usage:
    python manager.py ingest ~/Desktop/ContentDrop
    python manager.py search "startup school"
    python manager.py list
    python manager.py dashboard --open

Nothing here talks to AWS directly -- all of that lives in transcribe.py.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

import dashboard
import transcribe

# What counts as a clip worth transcribing.
MEDIA_EXTS = {".mov", ".mp4", ".m4a", ".wav", ".mp3"}

# Keep the archive next to the script, not next to whatever the current working
# directory happens to be -- launchd runs this from somewhere unpredictable.
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "archive.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL UNIQUE,
    filepath    TEXT NOT NULL,
    transcript  TEXT NOT NULL,
    date_added  TEXT NOT NULL,
    duration    REAL,
    timings     TEXT
)
"""


def open_db(path):
    """Open (creating if needed) the archive database."""
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    # Archives built before word timings existed are missing the column, so
    # add it rather than making people start over.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(transcripts)")}
    if "timings" not in columns:
        conn.execute("ALTER TABLE transcripts ADD COLUMN timings TEXT")
    conn.commit()
    return conn


def indexed_filenames(conn):
    return {row[0] for row in conn.execute("SELECT filename FROM transcripts")}


def find_media(folder):
    """Every supported clip in the folder, oldest first."""
    found = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not name.startswith(".") and os.path.isfile(path):
            if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                found.append(path)
    return sorted(found, key=os.path.getmtime)


def word_timings(data):
    """Every word with the second it's spoken, as [start, end, text].

    Amazon Transcribe hands back one item per word plus one per punctuation
    mark. Punctuation carries no timestamp, so it rides along as [null, null,
    "."] and gets glued to the word before it when the page renders.
    """
    words = []
    for item in data.get("results", {}).get("items", []):
        content = item["alternatives"][0]["content"]
        if item.get("type") == "pronunciation" and item.get("start_time"):
            words.append([round(float(item["start_time"]), 2),
                          round(float(item["end_time"]), 2), content])
        else:
            words.append([None, None, content])
    return words


def clip_duration(data):
    """Length in seconds, taken from the last spoken word's end time."""
    for item in reversed(data.get("results", {}).get("items", [])):
        if item.get("end_time"):
            return float(item["end_time"])
    return None


def pretty_duration(seconds):
    if not seconds:
        return "?"
    # Round first, so 59.6s reads 01:00 rather than 00:60.
    total = round(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def ingest(args):
    folder = os.path.expanduser(args.folder)
    if not os.path.isdir(folder):
        sys.exit(f"Not a folder: {folder}")

    # Timestamp the launchd log, but keep the on-camera run clean.
    if not sys.stdout.isatty():
        print(f"\n=== ingest {datetime.now():%Y-%m-%d %H:%M} - {folder} ===")

    conn = open_db(args.db)
    already = indexed_filenames(conn)

    clips = find_media(folder)
    new = [p for p in clips if os.path.basename(p) not in already]
    skipped = len(clips) - len(new)

    print(f"Found {len(new)} new file{'' if len(new) == 1 else 's'}"
          + (f" ({skipped} already indexed)" if skipped else ""))
    if not new:
        return

    s3, client, bucket = transcribe.connect(args.region, args.bucket)

    indexed = 0
    for path in new:
        name = os.path.basename(path)
        print(f"Transcribing {name}...")
        try:
            job = transcribe.start_job(s3, client, path, bucket, args.language)
            finished, reason = transcribe.wait_for_jobs(client, [job])[0]
        except (BotoCoreError, ClientError) as e:
            print(f"  skipped {name}: {e}")
            continue

        if reason:
            print(f"  failed {name}: {reason}")
            continue

        data = transcribe.fetch_transcript(s3, bucket, finished)
        text = data["results"]["transcripts"][0]["transcript"]

        conn.execute(
            "INSERT INTO transcripts"
            " (filename, filepath, transcript, date_added, duration, timings)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, os.path.abspath(path), text, datetime.now().isoformat(timespec="seconds"),
             clip_duration(data), json.dumps(word_timings(data))),
        )
        conn.commit()
        indexed += 1
        print(f"Indexed {name}")

    print(f"\nDone. Indexed {indexed} of {len(new)} new file(s) into {args.db}")


def term_pattern(term):
    """Match the term at a word start.

    Without the anchor, searching "AI" also hits the middle of "waitlist" and
    "downstairs". Prefixes still match, so "startup" finds "startups". A term
    opening with punctuation skips the anchor -- \\b means the opposite there.
    """
    prefix = r"\b" if term[:1].isalnum() or term[:1] == "_" else ""
    return re.compile(prefix + re.escape(term), re.IGNORECASE)


def snippet(text, term, width=70):
    """A window of transcript around the first hit, match highlighted."""
    found = term_pattern(term).search(text)
    at = found.start() if found else -1
    if at < 0:
        return text[:width].replace("\n", " ")

    start, end = max(0, at - width), min(len(text), at + len(term) + width)
    match = text[at:at + len(term)]
    if sys.stdout.isatty():
        match = f"\033[1;95m{match}\033[0m"

    body = (text[start:at] + match + text[at + len(term):end]).replace("\n", " ")
    return ("..." if start else "") + body + ("..." if end < len(text) else "")


def search(args):
    conn = open_db(args.db)
    # LIKE narrows it down in SQL; the word-start check then drops the rows
    # where the term only appeared inside a longer word.
    pattern = term_pattern(args.term)
    rows = [
        row for row in conn.execute(
            "SELECT filename, date_added, duration, transcript FROM transcripts"
            " WHERE transcript LIKE ? ORDER BY date_added",
            (f"%{args.term}%",),
        ).fetchall()
        if pattern.search(row[3])
    ]

    if not rows:
        print(f"No matches for '{args.term}'.")
        return

    print(f"\n{len(rows)} video{'' if len(rows) == 1 else 's'} mention '{args.term}':\n")
    for filename, date_added, duration, text in rows:
        hits = len(pattern.findall(text))
        print(f"  {filename}   {date_added[:10]}   {pretty_duration(duration)}"
              f"   {hits} mention{'' if hits == 1 else 's'}")
        print(f"    {snippet(text, args.term)}\n")


def list_all(args):
    conn = open_db(args.db)
    rows = conn.execute(
        "SELECT filename, date_added, duration FROM transcripts ORDER BY date_added"
    ).fetchall()
    if not rows:
        print("Archive is empty. Run 'ingest' first.")
        return
    print(f"\n{len(rows)} clip{'' if len(rows) == 1 else 's'} archived:\n")
    for filename, date_added, duration in rows:
        print(f"  {date_added[:10]}   {pretty_duration(duration):>7}   {filename}")
    print()


def show_dashboard(args):
    out = args.out or os.path.join(HERE, "archive.html")
    path, stats = dashboard.build(args.db, out)
    print(f"\nBuilt {path}")
    print(f"  {stats['clips']} clip(s), {stats['minutes']} minutes, {stats['words']:,} words")
    if args.open:
        subprocess.run(["open", path], check=False)
    else:
        print(f"\nOpen it with:  open {path}")


def main():
    parser = argparse.ArgumentParser(description="Transcribe and search your video archive.")
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="Transcribe every new clip in a folder.")
    ing.add_argument("folder", help="Folder to scan, e.g. ~/Desktop/ContentDrop")
    ing.add_argument("--language", default="en-US", help="Language code (default: en-US).")
    ing.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"),
                     help="AWS region (default: us-east-1 or $AWS_REGION).")
    ing.add_argument("--bucket", default=os.environ.get("TRANSCRIBE_BUCKET"),
                     help="S3 bucket to use. Defaults to $TRANSCRIBE_BUCKET or an auto-named one.")
    ing.add_argument("--db", default=DEFAULT_DB, help="Archive database (default: archive.db).")
    ing.set_defaults(func=ingest)

    find = sub.add_parser("search", help="Search every transcript in the archive.")
    find.add_argument("term", help="Word or phrase to look for.")
    find.add_argument("--db", default=DEFAULT_DB, help="Archive database (default: archive.db).")
    find.set_defaults(func=search)

    ls = sub.add_parser("list", help="Show everything in the archive.")
    ls.add_argument("--db", default=DEFAULT_DB, help="Archive database (default: archive.db).")
    ls.set_defaults(func=list_all)

    dash = sub.add_parser("dashboard", help="Render the archive as a searchable web page.")
    dash.add_argument("--open", action="store_true", help="Open the page in your browser when it's built.")
    dash.add_argument("--out", help="Where to write the page (default: archive.html).")
    dash.add_argument("--db", default=DEFAULT_DB, help="Archive database (default: archive.db).")
    dash.set_defaults(func=show_dashboard)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
