#!/usr/bin/env python3
"""
Renders the archive as a single self-contained HTML page.

Everything -- the transcripts, the styling, the search -- gets baked into one
file with no server and no network calls, so it opens straight off disk and
keeps working if you email it to yourself.

Usage:
    python manager.py dashboard
    python manager.py dashboard --open
"""

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Poster frames get cached here so a rebuild doesn't re-extract every video.
THUMB_DIR = os.path.join(HERE, "thumbs")
THUMB_WIDTH = 480

# Clips whose own poster frame isn't flattering -- a blink, a black frame, a
# half-formed word. Value is how many seconds in to grab instead. Anything not
# listed just uses the poster frame. Change a number here and the next build
# re-cuts that cover; the cache is keyed on the timestamp.
COVER_AT = {
    "reel2.MOV": 2.0,
}

GRABBER = os.path.join(HERE, "framegrab.swift")

# The bow from the repo's shared assets, reused as the browser tab icon.
FAVICON = os.path.join(HERE, os.pardir, "assets", "bow.svg")

# iPhone clips are HEVC, which Chrome on macOS only sometimes decodes. avconvert
# (stock macOS) re-wraps them as 720p H.264, which every browser plays. These
# are too big to inline, so the page links to them -- keep the folder next to
# archive.html.
WEB_DIR = os.path.join(HERE, "web")
WEB_PRESET = "PresetAppleM4V720pHD"

# The headline font, embedded so the page stays a single portable file. First
# match wins; if none exist the page falls back to a system serif (and to the
# installed copy of TAY Bang!, if you have it, since the name still resolves).
FONT_PATHS = [
    os.path.join(HERE, "TAYBang.woff2"),
    os.path.expanduser("~/Documents/font license/TAYBang!.woff2"),
]


def favicon_link():
    """The bow as an inline <link> icon, or '' if the asset isn't there."""
    if not os.path.exists(FAVICON):
        return ""
    with open(FAVICON, "rb") as f:
        svg = f.read()
    data = base64.b64encode(svg).decode("ascii")
    return f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{data}">'


def font_face():
    """@font-face rule with the font inlined as base64, or '' if not found."""
    for path in FONT_PATHS:
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            return ("@font-face { font-family: 'TAY Bang!'; font-display: swap;"
                    f" src: url(data:font/woff2;base64,{data}) format('woff2'); }}")
    return ""


def load_clips(db_path):
    """Every archived clip, newest first, as plain dicts for the page."""
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(transcripts)")}
    timings = "timings" if "timings" in columns else "NULL"
    rows = conn.execute(
        "SELECT filename, filepath, transcript, date_added, duration,"
        f" {timings} FROM transcripts ORDER BY date_added DESC"
    ).fetchall()
    conn.close()

    clips = []
    for filename, filepath, transcript, date_added, duration, timed in rows:
        text = (transcript or "").strip()
        clips.append({
            "filename": filename,
            "filepath": filepath,
            "transcript": text,
            "date": date_added[:10],
            "time": date_added[11:16],
            "duration": duration or 0,
            "words": len(text.split()),
            "timings": json.loads(timed) if timed else [],
        })
    return clips


def run(command):
    """True if the command ran cleanly. Failures here are never fatal -- a
    missing cover just falls back to the placeholder."""
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=90)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def poster_frame(video, dest_png):
    """Whatever frame the video opens on, via QuickLook."""
    if not shutil.which("qlmanage"):
        return None
    tmp = os.path.dirname(dest_png)
    if not run(["qlmanage", "-t", "-s", str(THUMB_WIDTH * 2), "-o", tmp, video]):
        return None
    # qlmanage names its output after the source file, so find whatever it
    # actually wrote rather than guessing the extension.
    made = [f for f in os.listdir(tmp) if f.lower().endswith(".png")]
    return os.path.join(tmp, made[0]) if made else None


def frame_at(video, seconds, dest_png):
    """A specific frame, via AVFoundation. Needs swift; None if unavailable."""
    if not shutil.which("swift") or not os.path.exists(GRABBER):
        return None
    if run(["swift", GRABBER, video, str(seconds), dest_png]):
        return dest_png
    return None


def extract_frame(video, dest, at=None):
    """Write a cover image for the video to dest as JPEG.

    Uses what every Mac already has -- AVFoundation to seek to an exact moment,
    QuickLook for the poster frame, sips to shrink whichever we got. No ffmpeg,
    nothing to install. Returns True on success.
    """
    if not os.path.exists(video):
        return False

    with tempfile.TemporaryDirectory() as tmp:
        raw = None
        if at is not None:
            raw = frame_at(video, at, os.path.join(tmp, "cover.png"))
        if raw is None:
            raw = poster_frame(video, os.path.join(tmp, "poster.png"))
        if raw is None:
            return False

        run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72",
             "-Z", str(THUMB_WIDTH), raw, "--out", dest])
    return os.path.exists(dest)


def thumbnail(clip):
    """Cached cover frame as a data URI, or '' if one can't be made."""
    os.makedirs(THUMB_DIR, exist_ok=True)

    # Keying the cache on the timestamp means editing COVER_AT re-cuts the
    # cover on the next build instead of serving a stale one.
    at = COVER_AT.get(clip["filename"])
    suffix = "" if at is None else f"@{at}"
    dest = os.path.join(THUMB_DIR, f"{clip['filename']}{suffix}.jpg")

    if not os.path.exists(dest):
        extract_frame(clip["filepath"], dest, at)
    if not os.path.exists(dest):
        return ""

    with open(dest, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("ascii")


def web_video(clip):
    """Path to a browser-playable copy of the clip, relative to archive.html.

    Returns '' if the source is missing or avconvert isn't available -- the
    card then just doesn't offer playback.
    """
    source = clip["filepath"]
    if not os.path.exists(source) or not shutil.which("avconvert"):
        return ""

    os.makedirs(WEB_DIR, exist_ok=True)
    name = os.path.splitext(clip["filename"])[0] + ".m4v"
    dest = os.path.join(WEB_DIR, name)

    if not os.path.exists(dest):
        print(f"  converting {clip['filename']} for playback...")
        # avconvert refuses to overwrite, so a half-written file from an
        # interrupted run has to go before we retry.
        if not run(["avconvert", "--preset", WEB_PRESET,
                    "--source", source, "--output", dest]):
            if os.path.exists(dest):
                os.remove(dest)
            return ""
    return "web/" + name


def opening_line(text, limit=150):
    """The first sentence or two -- what the clip actually opens with."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if stop > 60:
        return cut[:stop + 1]
    return cut[:cut.rfind(" ")] + "..."


def build(db_path, out_path):
    clips = load_clips(db_path)
    for index, clip in enumerate(clips):
        # Stable id so a card can point at its clip even once search has
        # filtered the list down.
        clip["index"] = index
        clip["thumb"] = thumbnail(clip)
        clip["video"] = web_video(clip)
        clip["opening"] = opening_line(clip["transcript"])

    stats = {
        "clips": len(clips),
        "minutes": round(sum(c["duration"] for c in clips) / 60, 1),
        "words": sum(c["words"] for c in clips),
        "thumbs": sum(1 for c in clips if c["thumb"]),
    }

    # json.dumps handles the quoting; escaping "</" stops a stray closing tag
    # inside a transcript from ending the <script> block early.
    payload = json.dumps({"clips": clips}).replace("</", "<\\/")
    page = (TEMPLATE
            .replace("__FAVICON__", favicon_link())
            .replace("__FONT__", font_face())
            .replace("__DATA__", payload))

    with open(out_path, "w") as f:
        f.write(page)
    return out_path, stats


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coco's Content Manager</title>
__FAVICON__
<style>
  __FONT__
  :root {
    --pink: #FF53A9;
    --pink-soft: #FFE6F2;
    --ink: #1B1420;
    --muted: #8A7F88;
    --line: #F0E4EC;
    --card: #FFFFFF;
  }
  * { box-sizing: border-box; }
  /* Gradient lives on <html> so a short archive doesn't leave a hard edge
     where the body stops. */
  html {
    min-height: 100%;
    background: radial-gradient(120% 900px at 50% 0%, #FFF3F9 0%, #FDFBFC 55%) no-repeat #FDFBFC;
  }
  body {
    margin: 0;
    padding: 48px 24px 96px;
    color: var(--ink);
    font: 17px/1.65 ui-serif, "New York", "Iowan Old Style", Charter, Georgia, serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1020px; margin: 0 auto; }

  header { text-align: center; margin-bottom: 36px; }
  h1 {
    /* TAY Bang! is a script face -- it wants room to breathe, so no negative
       tracking and a little extra leading. Scales with the viewport so the
       headline stays on one line instead of breaking mid-phrase. */
    font-family: 'TAY Bang!', "Snell Roundhand", Georgia, serif;
    font-size: clamp(46px, 9.2vw, 104px);
    line-height: 1.3; letter-spacing: 0; color: var(--pink);
    /* the descender on "Manager" runs long -- give it somewhere to go */
    margin: 0 0 16px; padding-bottom: 10px; font-weight: 400;
  }

  /* Search field on the left, the button on the right, sharing one row. */
  .controls {
    display: flex; align-items: stretch; gap: 14px; margin-bottom: 12px;
  }
  #go {
    flex: 0 0 auto; display: flex; align-items: center; gap: 9px;
    padding: 0 30px; border: 0; border-radius: 999px; cursor: pointer;
    background: var(--pink); color: #FFF;
    font: inherit; font-size: 18px; font-weight: 600;
    box-shadow: 0 2px 10px rgba(255,83,169,.32);
    transition: background .15s, box-shadow .15s, transform .1s;
  }
  #go svg { width: 19px; height: 19px; stroke: #FFF; fill: none; stroke-width: 2.2; }
  #go:hover { background: #F03D97; box-shadow: 0 4px 16px rgba(255,83,169,.42); }
  #go:active { transform: translateY(1px); }

  .searchbar { position: relative; flex: 1 1 auto; min-width: 0; }
  .searchbar svg {
    position: absolute; left: 20px; top: 50%; transform: translateY(-50%);
    width: 20px; height: 20px; stroke: var(--muted); fill: none; stroke-width: 2;
  }
  #q {
    width: 100%; height: 100%; padding: 16px 20px 16px 52px;
    font: inherit; font-size: 19px; color: var(--ink);
    background: var(--card); border: 1.5px solid var(--line); border-radius: 999px;
    outline: none; transition: border-color .15s, box-shadow .15s;
  }
  #q::placeholder { color: #BDB2B9; }
  #q:focus { border-color: var(--pink); box-shadow: 0 0 0 4px rgba(255,83,169,.13); }

  .count { color: var(--muted); font-size: 14px; padding: 4px 6px 18px; min-height: 26px; }
  .count b { color: var(--ink); }

  /* Three across, and each card only grows the row it lives in. */
  #results {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 22px; align-items: start;
  }
  .clip {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px;
    overflow: hidden; box-shadow: 0 1px 2px rgba(27,20,32,.04);
    transition: border-color .15s, box-shadow .15s;
  }
  .clip:hover { border-color: #E9D3E0; box-shadow: 0 8px 24px rgba(27,20,32,.09); }
  .clip.open { border-color: var(--pink); }

  .thumb {
    position: relative; aspect-ratio: 4 / 5; background: #F5EBF1;
    display: block; overflow: hidden;
  }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  /* No frame? Fall back to something on-brand rather than an empty box. */
  .thumb.none {
    background: linear-gradient(140deg, #FFD9EC, #FFF1F8);
    display: flex; align-items: center; justify-content: center;
    color: #D584AE; font-size: 34px;
  }
  .dur {
    position: absolute; right: 10px; bottom: 10px;
    background: rgba(27,20,32,.62); color: #FFF; backdrop-filter: blur(4px);
    font-size: 12px; padding: 3px 9px; border-radius: 999px;
    font-family: ui-sans-serif, -apple-system, system-ui, sans-serif;
    font-variant-numeric: tabular-nums;
  }
  .hits {
    position: absolute; left: 10px; top: 10px;
    background: var(--pink); color: #FFF; font-size: 12px; font-weight: 650;
    padding: 4px 10px; border-radius: 999px; white-space: nowrap;
    font-family: ui-sans-serif, -apple-system, system-ui, sans-serif;
  }

  .body { padding: 16px 18px 14px; }
  .preview {
    font-size: 15.5px; line-height: 1.55; color: #3D343A; margin: 0;
    display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .clip.open .preview { display: none; }

  .toggle {
    display: flex; align-items: center; gap: 7px; margin-top: 12px;
    background: none; border: 0; padding: 0; cursor: pointer;
    color: var(--pink); font: inherit; font-size: 14px;
    font-family: ui-sans-serif, -apple-system, system-ui, sans-serif;
  }
  .toggle .chev { transition: transform .2s; font-size: 10px; }
  .clip.open .toggle .chev { transform: rotate(180deg); }

  .full {
    display: none; margin-top: 12px; padding-top: 14px;
    border-top: 1px solid var(--line); color: #3D343A; font-size: 15.5px;
  }
  .clip.open .full { display: block; }


  mark { background: #FFD9EC; color: #A81A5E; border-radius: 3px; padding: 0 2px; font-weight: 600; }

  .empty {
    grid-column: 1 / -1;
    text-align: center; color: var(--muted); padding: 60px 20px;
  }

  /* Play button, sitting over the middle of the cover. */
  .play {
    position: absolute; inset: 0; border: 0; background: none; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  .play i {
    width: 54px; height: 54px; border-radius: 50%;
    background: rgba(255,255,255,.86); box-shadow: 0 4px 18px rgba(27,20,32,.28);
    display: flex; align-items: center; justify-content: center;
    color: var(--pink); font-size: 19px; font-style: normal; padding-left: 4px;
    transition: transform .18s, background .18s;
  }
  .clip:hover .play i, .play:focus-visible i {
    transform: scale(1.12); background: #FFF;
  }

  /* Player overlay: video on the left, transcript following along on the right. */
  .modal {
    position: fixed; inset: 0; z-index: 50; display: flex;
    align-items: center; justify-content: center; padding: 28px;
    background: rgba(27,20,32,.62); backdrop-filter: blur(6px);
  }
  .modal[hidden] { display: none; }
  .sheet {
    background: var(--card); border-radius: 22px; overflow: hidden;
    display: flex; gap: 0; max-width: 1000px; width: 100%; max-height: 88vh;
    box-shadow: 0 24px 70px rgba(27,20,32,.4);
  }
  .sheet video {
    width: 46%; flex: 0 0 46%; background: #000;
    object-fit: contain; max-height: 88vh;
  }
  .script {
    flex: 1 1 auto; overflow-y: auto; padding: 26px 28px 30px;
    font-size: 17px; line-height: 1.9;
  }
  .script h3 {
    margin: 0 0 18px; font-size: 15px; letter-spacing: .02em;
    font-family: ui-sans-serif, -apple-system, system-ui, sans-serif;
  }
  .script .note {
    margin: 0 0 18px; font-size: 13px; color: var(--muted);
    font-family: ui-sans-serif, -apple-system, system-ui, sans-serif;
  }
  /* Every word is clickable -- tap one to jump the video there. */
  .w { cursor: pointer; border-radius: 4px; padding: 1px 1px; transition: color .12s; }
  .w:hover { color: var(--pink); }
  .w.hit { background: #FFD9EC; color: #A81A5E; font-weight: 600; }
  .w.said { color: #A99DA5; }
  .w.now {
    background: var(--pink); color: #FFF; font-weight: 600;
    box-shadow: 0 0 0 3px rgba(255,83,169,.22);
  }
  .close {
    position: absolute; top: 40px; right: 42px; z-index: 60;
    width: 40px; height: 40px; border-radius: 50%; border: 0; cursor: pointer;
    background: rgba(255,255,255,.92); color: var(--ink); font-size: 20px;
    display: flex; align-items: center; justify-content: center;
  }

  @media (max-width: 820px) {
    .sheet { flex-direction: column; max-height: 92vh; }
    .sheet video { width: 100%; flex: 0 0 auto; max-height: 44vh; }
  }
  .empty code {
    display: inline-block; margin-top: 10px; background: #FFF; padding: 8px 14px;
    border: 1px solid var(--line); border-radius: 10px; font-size: 14px; color: var(--ink);
  }

  @media (max-width: 900px) {
    #results { grid-template-columns: repeat(2, 1fr); }
  }
  /* Too narrow to sit side by side -- stack the controls. */
  @media (max-width: 780px) {
    body { padding: 32px 16px 64px; }
    .controls { flex-direction: column; gap: 12px; }
    .profile { display: grid; grid-template-columns: repeat(3, 1fr); }
    .stat { min-width: 0; }
  }
  @media (max-width: 600px) {
    #results { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Coco's Content Manager</h1>
  </header>

  <div class="controls">
    <div class="searchbar">
      <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
      <input id="q" type="search" placeholder="Search my transcripts" autocomplete="off" autofocus>
    </div>
    <button id="go" type="button">
      <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
      Search
    </button>
  </div>
  <div class="count" id="count"></div>

  <div id="results"></div>
</div>

<div class="modal" id="modal" hidden>
  <button class="close" id="close" type="button" aria-label="Close">&times;</button>
  <div class="sheet">
    <video id="player" controls playsinline preload="metadata"></video>
    <div class="script" id="script"></div>
  </div>
</div>

<script>
const DATA = __DATA__;

const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const escRe = s => s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');

// Anchor at a word start, so searching "AI" doesn't light up the middle of
// "waitlist" or "downstairs". Still matches prefixes, so "startup" finds
// "startups". Terms opening with punctuation skip the anchor -- \\b would mean
// the opposite thing there.
const pattern = t => (/^\\w/.test(t) ? '\\\\b' : '') + escRe(t);
const matcher = (t, flags) => new RegExp(pattern(t), flags);
const countHits = (text, t) => (text.match(matcher(t, 'gi')) || []).length;

function dur(sec) {
  if (!sec) return '--';
  // Round first, so 59.6s reads 01:00 rather than 00:60.
  const total = Math.round(sec);
  return String(Math.floor(total / 60)).padStart(2, '0')
    + ':' + String(total % 60).padStart(2, '0');
}

// Escape first, then wrap matches -- so a transcript can never inject markup.
function highlight(text, term) {
  const safe = esc(text);
  if (!term) return safe;
  return safe.replace(matcher(esc(term), 'gi'), m => '<mark>' + m + '</mark>');
}

// A readable window around the first hit, rather than the top of the clip.
function snippet(text, term) {
  if (!term) return highlight(text.slice(0, 180), '') + (text.length > 180 ? '...' : '');
  const at = text.search(matcher(term, 'i'));
  if (at < 0) return highlight(text.slice(0, 180), term);
  const start = Math.max(0, at - 90), end = Math.min(text.length, at + term.length + 90);
  return (start ? '...' : '') + highlight(text.slice(start, end), term) + (end < text.length ? '...' : '');
}

const results = document.getElementById('results');
const countEl = document.getElementById('count');

function render(term) {
  term = term.trim();
  const matches = term
    ? DATA.clips.filter(c => countHits(c.transcript, term) > 0)
    : DATA.clips;

  if (!DATA.clips.length) {
    countEl.textContent = '';
    results.innerHTML = '<div class="empty">Nothing archived yet.<br>'
      + '<code>python manager.py ingest ~/Desktop/ContentDrop</code></div>';
    return;
  }

  countEl.innerHTML = term
    ? (matches.length
        ? `<b>${matches.length}</b> Clip${matches.length === 1 ? ' Mentions' : 's Mention'} &ldquo;${esc(term)}&rdquo;`
        : `No Clips Mention &ldquo;${esc(term)}&rdquo;`)
    : `<b>${DATA.clips.length}</b> Clip${DATA.clips.length === 1 ? '' : 's'} Archived`;

  results.innerHTML = matches.map(c => {
    const hits = term ? countHits(c.transcript, term) : 0;
    // Searching? Show the matching line. Otherwise show how the clip opens.
    const lead = term ? snippet(c.transcript, term) : esc(c.opening);
    return `<div class="clip">
      <div class="thumb${c.thumb ? '' : ' none'}">
        ${c.thumb ? `<img src="${c.thumb}" alt="Frame from ${esc(c.filename)}" loading="lazy">` : '&#9834;'}
        ${c.video ? `<button class="play" type="button" data-play="${c.index}"
           aria-label="Play ${esc(c.filename)}"><i>&#9654;</i></button>` : ''}
        ${hits ? `<span class="hits">${hits} Mention${hits === 1 ? '' : 's'}</span>` : ''}
        <span class="dur">${dur(c.duration)}</span>
      </div>
      <div class="body">
        <p class="preview">${lead}</p>
        <button class="toggle" type="button">
          <span class="chev">&#9660;</span><span class="label">Read Transcript</span>
        </button>
        <div class="full">${highlight(c.transcript, term)}</div>
      </div>
    </div>`;
  }).join('') || '<div class="empty">Try a different word.</div>';

  results.querySelectorAll('.clip').forEach(el => {
    const btn = el.querySelector('.toggle');
    if (btn) btn.onclick = () => {
      const open = el.classList.toggle('open');
      btn.querySelector('.label').textContent = open ? 'Hide Transcript' : 'Read Transcript';
    };
    const play = el.querySelector('.play');
    if (play) play.onclick = () => openPlayer(DATA.clips[+play.dataset.play], term);
  });
}

/* ---- Player: video on the left, transcript lighting up word by word ---- */

const modal = document.getElementById('modal');
const player = document.getElementById('player');
const script = document.getElementById('script');
let spans = [], starts = [], current = -1;

function openPlayer(clip, term) {
  const words = clip.timings || [];
  const terms = term ? term.trim().toLowerCase().split(/\\s+/).filter(Boolean) : [];
  const isHit = w => terms.some(t => w.toLowerCase().startsWith(t));

  script.innerHTML = `<h3>${esc(clip.filename)}</h3>`
    // Only worth saying something when the transcript *isn't* going to move.
    + (words.length ? '' : '<p class="note">No word timings for this clip.</p>')
    + (words.length
        // Punctuation has no timestamp -- glue it to the word before it so the
        // text reads normally instead of " word ."
        ? words.map(([s, e, text]) => s === null
            ? esc(text)
            : ` <span class="w${isHit(text) ? ' hit' : ''}" data-s="${s}" data-e="${e}">${esc(text)}</span>`
          ).join('').trim()
        : highlight(clip.transcript, term));

  spans = [...script.querySelectorAll('.w')];
  starts = spans.map(s => +s.dataset.s);
  current = -1;
  spans.forEach(s => s.onclick = () => { player.currentTime = +s.dataset.s; player.play(); });

  player.src = clip.video;
  modal.hidden = false;
  player.play().catch(() => {});   // autoplay may be blocked; controls still work
}

function closePlayer() {
  player.pause();
  player.removeAttribute('src');
  player.load();
  modal.hidden = true;
  spans = [];
}

// Binary search rather than scanning every word on each tick -- timeupdate
// fires ~4x a second and a long clip has hundreds of words.
function wordAt(time) {
  let lo = 0, hi = starts.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (starts[mid] <= time) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  return found;
}

player.addEventListener('timeupdate', () => {
  if (!spans.length) return;
  const i = wordAt(player.currentTime);
  if (i === current) return;

  // Everything before the playhead reads as already said, so the eye can find
  // the live word without hunting.
  if (current >= 0) spans[current].classList.remove('now');
  spans.forEach((s, n) => s.classList.toggle('said', n < i));
  current = i;
  if (i < 0) return;

  spans[i].classList.add('now');
  const box = script.getBoundingClientRect(), word = spans[i].getBoundingClientRect();
  if (word.top < box.top + 60 || word.bottom > box.bottom - 60) {
    spans[i].scrollIntoView({block: 'center', behavior: 'smooth'});
  }
});

document.getElementById('close').addEventListener('click', closePlayer);
modal.addEventListener('click', e => { if (e.target === modal) closePlayer(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !modal.hidden) closePlayer();
});

const q = document.getElementById('q');
q.addEventListener('input', () => render(q.value));
// Results already update as you type; the button re-runs it and hands focus
// back to the field, so clicking it never feels like a dead end.
document.getElementById('go').addEventListener('click', () => {
  render(q.value);
  q.focus();
});
render('');
q.focus();
</script>
</body>
</html>
"""
