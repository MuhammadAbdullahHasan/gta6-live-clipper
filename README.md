# GTA 6 Live Clipper

An experimental, fully local bot that watches GTA 6 live streams and turns hype
moments into YouTube Shorts automatically. Built in a day on an old 2-core MacBook,
free tools only.

> Status: **archived / not maintained.** It works end to end, but auto-picked
> moments were too random to be worth uploading. Shared as-is for anyone who
> wants to build on it.

## What it does

1. **Watches streamers** listed in `streamers.txt` (Twitch, YouTube `yt:`, Kick `kick:`,
   TikTok `tiktok:`), only while their category/title is GTA.
2. **Keeps a rolling buffer** of the last few minutes (`streamlink` -> `ffmpeg` segments).
3. **Detects moments**
   - Twitch: anonymous chat (IRC) message-rate spikes + hype words/emotes.
   - Others: audio-loudness spikes over the stream's own baseline (sleep-cam filter included).
4. **Builds a 1080x1920 Short** (`layout_filter`)
   - known streamer layout: facecam panel on top, wide gameplay below;
   - unknown: 4:3 gameplay over a blurred background;
   - hook text, optional credit line, loudness normalisation, hardware H.264.
5. **Captions** (`subs.py`): faster-whisper `small`, word-by-word highlighted ASS
   captions (Latin script, so Hindi/Urdu comes out as Roman Urdu), bad words masked.
   Clips where the streamer barely talks are scored down.
6. **Publishes** the best clips per day (limit + gap in `config.json`):
   - `upload_mode: "studio"` -> `yt_uploader.py` drives YouTube Studio in a dedicated
     Chrome profile (Playwright over CDP; you log in once). No API quota, no private-lock
     that unverified API projects get.
   - otherwise copies to a Google Drive folder for a Zapier "new file -> YouTube upload" Zap.
7. Optional: push notifications on upload / 6-hourly channel stats (`NOTIFY_URL` in `.env`).

## Setup

```bash
brew install ffmpeg            # needs libass for captions
uv tool install streamlink
uv venv .venv -p 3.11 && uv pip install -p .venv/bin/python -r requirements.txt
cp config.example.json config.json   # set youtube_channel_id, layouts, limits
.venv/bin/python yt_uploader.py login   # sign in to YouTube Studio once
python3 clipper.py
```

`config.json` -> `layouts` holds the facecam box per streamer as fractions of the
frame `[x, y, w, h]`; check a frame of their stream and adjust.

## Files

| file | purpose |
|---|---|
| `clipper.py` | watcher, recorder, detectors, clip builder, publisher (stdlib only) |
| `subs.py` | Whisper captions -> `.ass` |
| `yt_uploader.py` | YouTube Studio uploader (Playwright) |
| `streamers.txt` | who to watch, in priority order |
| `config.example.json` | all tunables |

## Notes

- Respect streamers: only clip people who allow it, and credit them.
- YouTube's reused-content policy applies to clip channels; add real value
  (commentary, edits) if you plan to monetise.

MIT licensed.
