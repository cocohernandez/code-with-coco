#!/usr/bin/env python3
"""
AWS Transcribe CLI tool.

Takes a local audio/video file, uploads it to S3, starts an Amazon Transcribe
job, waits for it to finish, and writes the transcript out as .txt and .json.

Usage:
    python transcribe.py path/to/audio.mp4
    python transcribe.py path/to/audio.mp4 --language en-US --bucket my-bucket

    # batch: every supported file in a folder, optionally filtered by date
    python transcribe.py path/to/reels/
    python transcribe.py path/to/reels/ --since 2026-01-01 --until 2026-06-30

Requires:
    pip install boto3
    AWS credentials configured (see README).
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    sys.exit("boto3 is not installed. Run:  pip install boto3")


# File extensions Amazon Transcribe accepts as a media format.
SUPPORTED = {".mp3", ".mp4", ".mov", ".wav", ".flac", ".ogg", ".amr", ".webm", ".m4a"}

# .mov shares the MP4/QuickTime container, so Amazon Transcribe expects it sent as "mp4".
FORMAT_MAP = {".m4a": "m4a", ".mov": "mp4"}

# How many Transcribe jobs to have in flight at once (account quota is 100).
MAX_CONCURRENT = 25


def media_format(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED:
        sys.exit(f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(SUPPORTED))}")
    # Transcribe wants the format name, not the dotted extension.
    return FORMAT_MAP.get(ext, ext.lstrip("."))


def ensure_bucket(s3, bucket, region):
    """Create the bucket if it doesn't already exist."""
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except ClientError:
        pass  # doesn't exist (or no access) -> try to create

    print(f"Creating S3 bucket '{bucket}' in {region}...")
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )


def parse_date(value, end_of_day=False):
    """Turn a YYYY-MM-DD string into a POSIX timestamp cutoff."""
    if not value:
        return None
    try:
        when = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"Bad date '{value}'. Use YYYY-MM-DD.")
    # --until is inclusive, so push the cutoff to the end of that day.
    if end_of_day:
        when += timedelta(days=1)
    return when.timestamp()


def collect_inputs(target, since, until):
    """One file, or every supported file in a folder, oldest first."""
    if os.path.isfile(target):
        return [target]
    if not os.path.isdir(target):
        sys.exit(f"File not found: {target}")

    found = []
    for name in os.listdir(target):
        path = os.path.join(target, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in SUPPORTED:
            continue
        mtime = os.path.getmtime(path)
        if since and mtime < since:
            continue
        if until and mtime >= until:
            continue
        found.append(path)
    return sorted(found, key=os.path.getmtime)


def transcript_base(path, outdir):
    """Output files are named after the source file, not the job id."""
    return os.path.join(outdir, os.path.splitext(os.path.basename(path))[0])


def start_job(s3, transcribe, path, bucket, language):
    """Upload one file and kick off its Transcribe job."""
    fmt = media_format(path)
    job = f"transcribe-cli-{uuid.uuid4().hex[:12]}"
    key = f"input/{job}{os.path.splitext(path)[1].lower()}"
    output_key = f"output/{job}.json"

    print(f"Uploading {os.path.basename(path)} -> s3://{bucket}/{key}")
    s3.upload_file(path, bucket, key)

    print(f"Starting Transcribe job '{job}' ({language})...")
    transcribe.start_transcription_job(
        TranscriptionJobName=job,
        Media={"MediaFileUri": f"s3://{bucket}/{key}"},
        MediaFormat=fmt,
        LanguageCode=language,
        OutputBucketName=bucket,
        OutputKey=output_key,
    )
    return {"job": job, "output_key": output_key, "source": path}


def wait_for_jobs(transcribe, jobs):
    """Poll every job until each has finished. Returns (job, failure_reason)."""
    pending = {j["job"]: j for j in jobs}
    finished = []
    while pending:
        for name in list(pending):
            resp = transcribe.get_transcription_job(TranscriptionJobName=name)
            status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                finished.append((pending.pop(name), None))
            elif status == "FAILED":
                reason = resp["TranscriptionJob"].get("FailureReason", "unknown")
                finished.append((pending.pop(name), reason))
        if pending:
            print(f"  ...{len(pending)} job(s) still transcribing, waiting 10s")
            time.sleep(10)
    return finished


def fetch_transcript(s3, bucket, job):
    """Read a finished job's result JSON back out of S3."""
    # The job wrote its output into our own bucket, so read it back through S3
    # with signed credentials -- TranscriptFileUri is not a pre-signed URL.
    obj = s3.get_object(Bucket=bucket, Key=job["output_key"])
    return json.load(obj["Body"])


def save_transcript(s3, bucket, job, outdir):
    """Read the finished job's JSON out of S3 and write .txt + .json locally."""
    data = fetch_transcript(s3, bucket, job)
    text = data["results"]["transcripts"][0]["transcript"]

    base = transcript_base(job["source"], outdir)
    with open(base + ".txt", "w") as f:
        f.write(text)
    with open(base + ".json", "w") as f:
        json.dump(data, f, indent=2)
    return base, text


def connect(region, bucket=None):
    """Build the S3/Transcribe clients and make sure the working bucket exists.

    Shared entry point so other tools (manager.py) can reuse this setup instead
    of duplicating the AWS wiring.
    """
    session = boto3.Session(region_name=region)
    sts = session.client("sts")
    try:
        account = sts.get_caller_identity()["Account"]
    except (BotoCoreError, ClientError) as e:
        sys.exit(f"Could not authenticate to AWS. Check your credentials.\n{e}")

    bucket = bucket or f"transcribe-cli-{account}-{region}"
    s3 = session.client("s3")
    transcribe = session.client("transcribe")
    ensure_bucket(s3, bucket, region)
    return s3, transcribe, bucket


def main():
    parser = argparse.ArgumentParser(description="Transcribe an audio/video file with Amazon Transcribe.")
    parser.add_argument("file", help="Path to a local audio/video file, or a folder of them.")
    parser.add_argument("--language", default="en-US", help="Language code (default: en-US).")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"),
                        help="AWS region (default: us-east-1 or $AWS_REGION).")
    parser.add_argument("--bucket", default=os.environ.get("TRANSCRIBE_BUCKET"),
                        help="S3 bucket to use. Defaults to $TRANSCRIBE_BUCKET or an auto-named one.")
    parser.add_argument("--outdir", default=".", help="Where to write the transcript files.")
    parser.add_argument("--since", help="Folder mode: skip files modified before this date (YYYY-MM-DD).")
    parser.add_argument("--until", help="Folder mode: skip files modified after this date (YYYY-MM-DD, inclusive).")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-transcribe files that already have a transcript in --outdir.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt in folder mode.")
    args = parser.parse_args()

    batch = os.path.isdir(args.file)
    files = collect_inputs(args.file, parse_date(args.since), parse_date(args.until, end_of_day=True))
    if not files:
        sys.exit("Nothing to transcribe (no supported files matched).")

    os.makedirs(args.outdir, exist_ok=True)
    if not args.overwrite:
        skipped = [f for f in files if os.path.exists(transcript_base(f, args.outdir) + ".txt")]
        files = [f for f in files if f not in skipped]
        for f in skipped:
            print(f"Skipping {os.path.basename(f)} (already transcribed; use --overwrite to redo)")
        if not files:
            sys.exit("Nothing left to transcribe.")

    # Transcribe bills per second of audio, so confirm before a bulk run.
    if batch and not args.yes:
        total_mb = sum(os.path.getsize(f) for f in files) / (1024 * 1024)
        print(f"\n{len(files)} file(s) to transcribe, {total_mb:,.0f} MB total:")
        for f in files:
            stamp = datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d")
            print(f"  {stamp}  {os.path.basename(f)}")
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Cancelled.")

    s3, transcribe, bucket = connect(args.region, args.bucket)

    # Start a wave of jobs up front, then poll them together -- Transcribe runs
    # them in parallel, so a wave takes about as long as its slowest file. The
    # account quota is 100 concurrent jobs; stay well under it.
    failures = []
    for i in range(0, len(files), MAX_CONCURRENT):
        wave = files[i:i + MAX_CONCURRENT]
        jobs = [start_job(s3, transcribe, f, bucket, args.language) for f in wave]

        print()
        for job, reason in wait_for_jobs(transcribe, jobs):
            name = os.path.basename(job["source"])
            if reason:
                print(f"FAILED   {name}: {reason}")
                failures.append(name)
                continue
            base, text = save_transcript(s3, bucket, job, args.outdir)
            print(f"Done     {name} -> {base}.txt")
            if not batch:
                print(f"  Full JSON:   {base}.json")
                print("\n--- Transcript preview ---")
                print(text[:500] + ("..." if len(text) > 500 else ""))

    if failures:
        sys.exit(f"\n{len(failures)} job(s) failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
