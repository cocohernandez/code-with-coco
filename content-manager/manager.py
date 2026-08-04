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

Nothing here talks to AWS directly -- all of that lives in transcribe.py.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

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
    duration    REAL
)
"""


def open_db(path):
    """Open (creating if needed) the archive database."""
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
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


def clip_duration(data):
    """Length in seconds, taken from the last spoken word's end time."""
    for item in reversed(data.get("results", {}).get("items", [])):
        if item.get("end_time"):
            return float(item["end_time"])
    return None


def pretty_duration(seconds):
    if not seconds:
        return "?"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


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
            "INSERT INTO transcripts (filename, filepath, transcript, date_added, duration)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, os.path.abspath(path), text, datetime.now().isoformat(timespec="seconds"),
             clip_duration(data)),
        )
        conn.commit()
        indexed += 1
        print(f"Indexed {name}")

    print(f"\nDone. Indexed {indexed} of {len(new)} new file(s) into {args.db}")


def snippet(text, term, width=70):
    """A window of transcript around the first hit, match highlighted."""
    lowered = text.lower()
    at = lowered.find(term.lower())
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
    rows = conn.execute(
        "SELECT filename, date_added, duration, transcript FROM transcripts"
        " WHERE transcript LIKE ? ORDER BY date_added",
        (f"%{args.term}%",),
    ).fetchall()

    if not rows:
        print(f"No matches for '{args.term}'.")
        return

    print(f"\n{len(rows)} video{'' if len(rows) == 1 else 's'} mention '{args.term}':\n")
    for filename, date_added, duration, text in rows:
        hits = text.lower().count(args.term.lower())
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
