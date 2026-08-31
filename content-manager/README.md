<div align="center">
<img src="../assets/readme_background.png" alt="code with coco" />
</div>

# <img src="../assets/text_bubble.svg" height="48" style="vertical-align: middle;" /> &nbsp; content archive

i have hundreds of videos on this laptop and no idea what i said in any of them. "wasn't there a clip where i talked about imposter syndrome?" — cool, enjoy scrubbing through 200 files to find it. so i built a thing that transcribes every video i drop in a folder and shoves the text into a tiny local database. now i just search for a phrase and it tells me which clip, what day, and the exact sentence around it. runs itself every morning. costs about two cents a video.

## <img src="../assets/light.svg" height="36" style="vertical-align: middle;" /> &nbsp; what it does

drop clips into a folder → amazon transcribe turns them into text → everything lands in `archive.db` (a single sqlite file, no database server, comes free with python) → you search it instantly.

it only pays for files it hasn't seen before, so re-running is free and safe. and it runs on a schedule, so the archive builds itself while you sleep.

two files:
- `transcribe.py` — does one video at a time (or a whole folder). this is the aws part.
- `manager.py` — the daily brain. scans, skips what's done, saves to the database, searches it.

## <img src="../assets/wrench.png" height="36" style="vertical-align: middle;" /> &nbsp; tutorial

### <img src="../assets/one.png" height="24" style="vertical-align: middle;" /> &nbsp; get your aws credentials

amazon transcribe doesn't use a simple api key — it uses **aws iam credentials**, an access key id + secret access key.

1. sign in to the [aws console](https://console.aws.amazon.com/) → search **iam**
2. **users** → create user (don't use root!). name it something like `transcribe-cli`
3. **uncheck** "provide user access to the console" — this user only needs a key
4. permissions → **attach policies directly** → **create inline policy** → json → paste this:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Resource": "*"
    }
  ]
}
```

5. once the user exists → **security credentials** → **create access key** → "command line interface (cli)"
6. copy both values. **the secret only shows once.**

> a brand new iam user has *zero* permissions until you attach that policy — it's not that it starts open and you lock it down, it starts fully closed.

### <img src="../assets/two.png" height="24" style="vertical-align: middle;" /> &nbsp; tell your mac about them

make the folder aws looks in, and drop your keys there:

```bash
mkdir -p ~/.aws
```

create `~/.aws/credentials`:

```ini
[default]
aws_access_key_id = AKIA...your key...
aws_secret_access_key = ...your secret...
```

and `~/.aws/config`:

```ini
[default]
region = us-east-1
output = json
```

then lock them down so only you can read them:

```bash
chmod 600 ~/.aws/credentials ~/.aws/config
```

these live in your home folder, **not in this repo**, so there's no way to accidentally commit them.

### <img src="../assets/three.png" height="24" style="vertical-align: middle;" /> &nbsp; install + test one video

```bash
cd content-manager
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

check your keys actually work:

```bash
python -c "import boto3; print(boto3.client('sts').get_caller_identity())"
```

that should print your account id. if it says `InvalidClientTokenId` you fat-fingered the key, `SignatureDoesNotMatch` means the secret. then transcribe something:

```bash
python transcribe.py my_video.mov
```

it uploads to s3, runs the job, and drops `my_video.txt` + `my_video.json` next to you. the `.json` has word-level timestamps and confidence scores if you ever want them.

> **`SubscriptionRequiredException`?** your aws account isn't fully activated yet — usually a missing payment method. check billing → payment methods, then wait a bit. brand new accounts can take a few hours.

### <img src="../assets/four.png" height="24" style="vertical-align: middle;" /> &nbsp; build the archive

make the drop folder and throw some clips in it:

```bash
mkdir -p ~/Desktop/ContentDrop
python manager.py ingest ~/Desktop/ContentDrop
```

```
Found 2 new files
Transcribing clip_042.mov...
Indexed clip_042.mov
Transcribing clip_043.mov...
Indexed clip_043.mov

Done. Indexed 2 of 2 new file(s) into archive.db
```

run it again and it does nothing, because it already knows those files:

```
Found 0 new files (2 already indexed)
```

*that's it! you're done!!!!!!!!!!*

## <img src="../assets/cocos_terminal.png" height="36" style="vertical-align: middle;" /> &nbsp; searching your own footage

this is the whole point:

```bash
python manager.py search "imposter syndrome"
```

```
2 videos mention 'imposter syndrome':

  clip_042.mov   2026-08-04   1m12s   3 mentions
    ...everyone at stanford has imposter syndrome and nobody talks about it until...

  clip_071.mov   2026-07-22   0m48s   1 mention
    ...i thought imposter syndrome would go away after the internship but...
```

case-insensitive, matches anywhere in the transcript, and the match gets highlighted in your terminal. see everything you've archived with:

```bash
python manager.py list
```

## <img src="../assets/text_bubble.svg" height="36" style="vertical-align: middle;" /> &nbsp; the pretty version

the terminal is great but not everyone wants to look at a terminal. so there's a web version too:

```bash
python manager.py dashboard --open
```

that builds `archive.html` and pops it open in your browser. type in the search box and it filters every clip live, with the matches highlighted — hit *read transcript* on any card to peel the whole thing open.

each card gets a **thumbnail pulled straight out of the video**, three to a row, so you're looking at your footage instead of a wall of filenames. it grabs the frame with quicklook and shrinks it with sips — both already on your mac, nothing to install. frames get cached in `thumbs/`, so the first build is slow-ish and every one after is instant.

### picking a better cover

sometimes the frame a video opens on is a blink, or a black frame, or you mid-syllable. so you can say which second to grab instead — edit `COVER_AT` at the top of `dashboard.py`:

```python
COVER_AT = {
    "reel2.MOV": 2.0,   # two seconds in
}
```

that one uses `framegrab.swift`, a tiny avfoundation script that seeks to an exact timestamp. swift comes with the xcode command line tools; if you don't have them it quietly falls back to the poster frame. the cache is keyed on the timestamp, so changing the number re-cuts the cover on the next build.

to find a good moment, grab a few frames yourself and look at them:

```bash
swift framegrab.swift reel2.MOV 4.5 test.png
```

the transcripts, the styling, the search, the fonts and the thumbnails are all baked into `archive.html` itself. no server running, no internet needed. rebuild it whenever you add new clips:

```bash
python manager.py dashboard
```

### <img src="../assets/light.svg" height="28" style="vertical-align: middle;" /> &nbsp; play it back, follow along

hit the play button on any cover and the clip opens with its transcript beside it, **lighting up word by word as you talk**. click any word to jump the video to that exact second.

that works because amazon transcribe returns a timestamp for every single word, not just the finished paragraph — `manager.py` stashes those in the `timings` column and the page reads them back. searching first? the words you searched for stay highlighted inside the player.

two things happen the first time you build this:

- iphone records in **hevc**, which chrome on mac only sometimes decodes. so `avconvert` (already on your mac) makes a 720p h.264 copy of each clip in `web/`. takes about 10 seconds a clip, then it's cached.
- those copies are far too big to inline, so `archive.html` **links** to them. the page still opens straight off disk, but if you move it, bring `web/` along or you'll get the transcript with a dead player.

## <img src="../assets/gcal.png" height="36" style="vertical-align: middle;" /> &nbsp; run it every day

macos has a built-in scheduler called `launchd`. the plist in this repo tells it to run `ingest` on `~/Desktop/ContentDrop` every morning at 9am.

### <img src="../assets/one.png" height="24" style="vertical-align: middle;" /> &nbsp; check the paths

open `com.coco.contentmanager.plist` and make sure the paths match where you actually put this folder. every path has to be **absolute** — `~` does not work in a plist.

### <img src="../assets/two.png" height="24" style="vertical-align: middle;" /> &nbsp; install it

```bash
cp com.coco.contentmanager.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.coco.contentmanager.plist
```

confirm it registered:

```bash
launchctl list | grep contentmanager
```

### <img src="../assets/three.png" height="24" style="vertical-align: middle;" /> &nbsp; give it permission to see your desktop

macos blocks background jobs from reading `~/Desktop` unless you allow it. go to **system settings → privacy & security → full disk access**, hit **+**, press `⌘ + shift + G`, and paste:

```
/Users/athenahernandez/Documents/code-with-coco/content-manager/venv/bin/
```

pick `python3.13` and toggle it on. skip this and your log will just say "operation not permitted" every morning.

### <img src="../assets/four.png" height="24" style="vertical-align: middle;" /> &nbsp; watch it work

it's silent when it fires, so the log is how you know:

```bash
tail -f contentmanager.log
```

force a run right now without waiting for 9am:

```bash
launchctl start com.coco.contentmanager
```

to change the time, edit `StartCalendarInterval` in the plist, then unload and reload it. to turn it off entirely:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.coco.contentmanager.plist
```

## <img src="../assets/folder.png" height="36" style="vertical-align: middle;" /> &nbsp; what's in the database

one row per video, in `archive.db`:

| column | what it is |
|---|---|
| `filename` | just the name, e.g. `clip_042.mov` — this is what makes re-runs free |
| `filepath` | full path so you can find the actual video |
| `transcript` | the whole thing as plain text |
| `date_added` | when it got archived |
| `duration` | length in seconds, pulled from the last spoken word |

it's a normal sqlite file, so you can open it in any sqlite browser, or poke at it directly:

```bash
sqlite3 archive.db "SELECT filename, duration FROM transcripts ORDER BY duration DESC LIMIT 5"
```

## <img src="../assets/smiley.png" height="36" style="vertical-align: middle;" /> &nbsp; the fine print

**formats:** `.mov`, `.mp4`, `.m4a`, `.wav`, `.mp3`. iphone `.mov` files work — they're really mp4 containers underneath, so the script tells transcribe to treat them that way.

**cost:** transcribe is about $0.024/minute. a 1-minute reel is ~2 cents, so a hundred of them is a couple bucks. s3 storage for the uploads is pennies.

**your videos get uploaded to s3** and stay there. clean them out occasionally:

```bash
aws s3 ls s3://your-bucket-name/input/
```

(needs `brew install awscli`, and you'd have to add `s3:DeleteObject` to the policy above to actually delete them.)

**it skips by filename, not content.** two different videos both named `IMG_1234.mov` will look like the same file, so rename before dropping them in.

## <img src="../assets/star.png" height="36" style="vertical-align: middle;" /> &nbsp; episode
- coming soon!

## <img src="../assets/wink.png" height="36" style="vertical-align: middle;" /> &nbsp; kudos

feel free to copy, fork, and share. if you make a video with it, tag me! and if you remix the code in your own project, a quick credit in the file is appreciated.
- <img src="../assets/tiktok.png" height="20" style="vertical-align: middle;" /> &nbsp; [`@cocopuffffffffs`](https://tiktok.com/@cocopuffffffffs)
- <img src="../assets/instagram.png" height="20" style="vertical-align: middle;" /> &nbsp; [`@cocohdzz`](https://instagram.com/cocohdzz)
