# clipforge

**One command per VOD.** Feed it a Twitch/Kick/YouTube VOD (or a live stream) and get back
platform-ready vertical clips: highlight moments picked from chat velocity, cut 9:16 with the
HUD intact, word-level burned captions, normalised audio, hard QC gates, a thumbnail, per-platform
metadata — and a review gate that blocks posting until you approve.

It runs entirely on your machine. No cloud, no credits, no per-clip limits. It ships a CLI **and**
an MCP server, so an agent can drive it with one tool call:

```
"Clip these videos, 4-10 clips each, with subtitles and catchy thumbnails"
        -> clip_videos(urls=[...], min_clips=4, max_clips=10, captions=True, thumbnails=True)
```

---

## What was actually verified

Run in this repo on Linux / Python 3.12 / ffmpeg 7.0.2, CPU only:

| Check | Result |
| --- | --- |
| `clipforge selftest` (synthetic 100 s source, injected chat + transcript) | **PASS** |
| Clips produced | 3 clips, **1080×1920**, 19.4–20.0 s |
| Loudness | **−14.0 LUFS** on every clip, QC 3/3 passed |
| Burned captions | verified by pixel count in the caption band (2,633 near-white px), not just file existence |
| Thumbnails | 1080×1920 JPEG, 2-line hook, measured to fit the frame |
| Review gate | unapproved run **refused** to post, by design |
| Posting | dry run wrote `post_queue.json` for 3 approved clips |
| MCP server | 4 tools listed successfully |

Not verified here: real Twitch/Kick ingest (no accounts or network access in the test container) and
the live faster-whisper transcription pass (the self-test injects a transcript to stay offline).
Both are exercised the first time you run `clipforge run` on a real URL without `--transcript-json`.

---

## Install

Needs **ffmpeg + ffprobe** on PATH, Python 3.10+, and a GPU is optional but much faster.

```bash
# 1. ffmpeg
brew install ffmpeg                 # macOS
sudo apt install ffmpeg             # Debian/Ubuntu
winget install Gyan.FFmpeg          # Windows

# 2. the pipeline
git clone <this repo> && cd clipforge
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[chat,mcp]"

# 3. check your machine
clipforge doctor
clipforge selftest        # renders a synthetic clip end to end, ~2 min on CPU
```

`doctor` tells you exactly what is missing: ffmpeg, yt-dlp, faster-whisper, chat-downloader, CUDA,
the caption font, and which posting keys are set.

**GPU note:** `CLIPFORGE_ENCODER=auto` picks `h264_nvenc` when the GPU build of ffmpeg supports it,
otherwise libx264. Whisper uses CUDA automatically when CTranslate2 sees a device.

---

## The one command

```bash
# one VOD, 6-10 clips with captions and thumbnails
clipforge run "https://www.twitch.tv/videos/123456789" --min-clips 6 --max-clips 10

# several VODs in a row, same settings
clipforge run URL1 URL2 URL3 --min-clips 4 --max-clips 8

# a live stream (captures the first 45 min of the DVR window)
clipforge run "https://kick.com/somechannel" --live --min-clips 5

# a file you already downloaded
clipforge run --source ./vod.mp4 --min-clips 4

# then, always: review before anything leaves your machine
clipforge review <run-id>              # shows the queue
clipforge review <run-id> --approve 1,3
clipforge review <run-id> --approve-all
clipforge post  <run-id> --platforms tiktok,youtube,instagram          # dry run
clipforge post  <run-id> --platforms tiktok,youtube,instagram --yes    # publish
```

Everything lands in `runs/<run-id>/`:

```
runs/20261116-2130-xqc8k2j/
├── summary.json            # stages, signal used, per-clip scores, timings
├── chat_series.json        # the chat-velocity series that drove selection
├── review.json             # the gate: pending / approved / rejected + posted ledger
├── post_queue.json         # written by a dry run
└── clips/clip_01/
    ├── clip.mp4            # 1080x1920, captions burned, -14 LUFS
    ├── captions.srt        # for native uploaders
    ├── captions.ass        # the burned style (karaoke)
    ├── thumb.jpg           # 1080x1920, hook stamped on
    ├── metadata.json       # title, hook, per-platform captions + hashtags
    └── manifest.json       # sha256 + probe data + QC evidence
```

---

## How each stage works

| Stage | What it does | Tuning |
| --- | --- | --- |
| **Ingest** | `yt-dlp` reads metadata, then downloads **only the winning windows** at up to 1080p — not the whole 6-hour VOD. Live mode grabs the first `CLIPFORGE_LIVE_MINUTES` of the DVR window. | `CLIPFORGE_MAX_HEIGHT`, `CLIPFORGE_LIVE_MINUTES`, `CLIPFORGE_COOKIES`, `CLIPFORGE_YTDLP_EXTRA` |
| **Signal** | Twitch/YouTube **chat replay** per-second message counts, smoothed, rolling z-score over a 2-minute window, plus emote density. Chat is the best highlight detector for gameplay. No chat (e.g. Kick) → audio-energy RMS curve fallback. | `CLIPFORGE_PEAK_Z`, `CLIPFORGE_MIN_GAP_S`, `CLIPFORGE_PRE_ROLL`, `CLIPFORGE_POST_ROLL` |
| **Windows** | Each peak becomes `peak − pre_roll … peak + post_roll`, clamped to `CLIP_MIN_S..CLIP_MAX_S`, non-overlapping, strongest first. | `CLIPFORGE_MIN_CLIPS`, `CLIPFORGE_MAX_CLIPS`, `CLIPFORGE_CLIP_MIN_S`, `CLIPFORGE_CLIP_MAX_S` |
| **Cut** | One ffmpeg pass. `--reframe fit` (default) shows the **whole frame over a blurred copy**, so the minimap, HUD and on-screen subtitles survive. `crop` fills the frame instead. | `CLIPFORGE_REFRAME=fit\|crop`, `CLIPFORGE_CRF` |
| **Captions** | faster-whisper word timestamps → 3-word karaoke ASS lines, active word highlighted, burned by libass. Edges snap to speech boundaries so nothing is cut mid-sentence. | `CLIPFORGE_ASR_MODEL`, `CLIPFORGE_CAPTION_FONT`, `CLIPFORGE_CAPTION_FONT_SIZE`, `CLIPFORGE_CAPTION_MARGIN_V`, `CLIPFORGE_CAPTION_WORDS` |
| **Audio** | two-stage loudness: single-pass `loudnorm`, measure, and re-render linearly if QC would complain. Optional music bed sidechain-ducked under speech. | `CLIPFORGE_LOUDNESS`, `CLIPFORGE_TRUE_PEAK`, `CLIPFORGE_MUSIC`, `CLIPFORGE_MUSIC_GAIN` |
| **QC** | resolution exactly 1080×1920, duration window, audio present, fps sane, integrated LUFS within ±2 of target, no >1 s of black frames, caption files non-empty. Failures are recorded per clip and shown in the CLI. | — |
| **Package** | sharpest-of-three frame → cover-cropped 1080×1920 → hook text auto-fitted and stamped; metadata from an LLM when `OPENAI_API_KEY` is set, else a heuristic from the transcript; sha256 manifest. | `CLIPFORGE_THUMBNAILS`, `OPENAI_API_KEY`, `CLIPFORGE_LLM_MODEL` |
| **Review gate** | every clip starts `pending`. `post` hard-refuses unless at least one clip is `approved`. | `clipforge review` |
| **Post** | `none` (dry-run queue), `uploadpost`, or `postiz`, with a posted-ledger so nothing double-posts. | `CLIPFORGE_POSTER` + keys |

---

## MCP: drive it from an agent

```bash
pip install -e ".[mcp]"
claude mcp add clipforge -- python -m clipforge.mcp_server
# or in any client's config JSON:
# {"mcpServers": {"clipforge": {"command": "python", "args": ["-m", "clipforge.mcp_server"]}}}
```

Four tools, by design — one does the work, two are guardrails:

| Tool | Arguments |
| --- | --- |
| `clip_videos` | `urls`, `source`, `min_clips`, `max_clips`, `captions`, `thumbnails`, `reframe`, `live`, `asr_model`, `run_id` |
| `review_clips` | `run_id`, `approve` (`"1,3"`), `reject`, `approve_all` |
| `post_clips` | `run_id`, `platforms`, `confirm` (**false = dry run**) |
| `run_status` | `run_id` |

In Freebuff Desktop, add it as a **local** MCP server on the machine that has the GPU (it appears
under your computer's devices, like `browsermcp`), then just ask for clips in chat. Render jobs
execute on your machine; only paths and JSON summaries travel back to the agent.

---

## Auto-posting

Posting is gated, and dry-run by default. Two adapters ship:

**Upload-Post** — one call fans out to TikTok, Reels and Shorts. MIT MCP server, 22 networks,
free tier 10 uploads/month, $24/mo for 5 profiles.

```bash
CLIPFORGE_POSTER=uploadpost
UPLOADPOST_API_KEY=...
UPLOADPOST_USER=your-profile-name
UPLOADPOST_URL=https://api.upload-post.com/api/upload
```

**Postiz** — self-hostable AGPL scheduler. Needs the integration IDs you connected in Postiz:

```bash
CLIPFORGE_POSTER=postiz
POSTIZ_API_KEY=...
POSTIZ_URL=https://api.postiz.com/public/v1
POSTIZ_INTEGRATIONS={"tiktok":"id","youtube":"id","instagram":"id"}
```

Both adapters send the standard multipart/JSON shapes and surface the provider's error body
verbatim. **Check the field names against your provider's current API docs** — these are
third-party APIs and their shapes change; the request is built in one small function each in
`clipforge/post.py`.

---

## Config keys, in one place

Everything is env vars (see `.env.example`); CLI flags override them.

| Group | Keys |
| --- | --- |
| Paths | `CLIPFORGE_OUT` |
| Selection | `CLIPFORGE_MIN_CLIPS`, `CLIPFORGE_MAX_CLIPS`, `CLIPFORGE_CLIP_MIN_S`, `CLIPFORGE_CLIP_MAX_S`, `CLIPFORGE_PRE_ROLL`, `CLIPFORGE_POST_ROLL`, `CLIPFORGE_MIN_GAP_S`, `CLIPFORGE_PEAK_Z` |
| Ingest | `CLIPFORGE_MAX_HEIGHT`, `CLIPFORGE_LIVE_MINUTES`, `CLIPFORGE_COOKIES`, `CLIPFORGE_YTDLP_EXTRA` |
| ASR | `CLIPFORGE_ASR_MODEL`, `CLIPFORGE_ASR_DEVICE`, `CLIPFORGE_ASR_COMPUTE`, `CLIPFORGE_ASR_LANGUAGE` |
| Captions | `CLIPFORGE_CAPTIONS`, `CLIPFORGE_CAPTION_FONT`, `CLIPFORGE_CAPTION_FONT_FILE`, `CLIPFORGE_CAPTION_FONT_SIZE`, `CLIPFORGE_CAPTION_MARGIN_V`, `CLIPFORGE_CAPTION_UPPER`, `CLIPFORGE_CAPTION_WORDS` |
| Video/audio | `CLIPFORGE_REFRAME`, `CLIPFORGE_ENCODER`, `CLIPFORGE_CRF`, `CLIPFORGE_LOUDNESS`, `CLIPFORGE_TRUE_PEAK`, `CLIPFORGE_MUSIC`, `CLIPFORGE_MUSIC_GAIN` |
| Packaging | `CLIPFORGE_THUMBNAILS`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `CLIPFORGE_LLM_MODEL` |
| Posting | `CLIPFORGE_POSTER`, `UPLOADPOST_API_KEY`, `UPLOADPOST_USER`, `UPLOADPOST_URL`, `POSTIZ_API_KEY`, `POSTIZ_URL`, `POSTIZ_INTEGRATIONS` |

A Roboto Bold (Apache-2.0) is bundled in `assets/` so captions and thumbnails render identically on
machines without system fonts. Point `CLIPFORGE_CAPTION_FONT_FILE` at Impact or anything else and set
`CLIPFORGE_CAPTION_FONT` to its family name.

---

## Hosting: why not Vercel

It cannot work there, and it is not a config problem:

- Rendering is **minutes of ffmpeg per VOD**; serverless functions cap out far sooner.
- Vercel functions have **no GPU**, no persistent disk for a 2 GB source, and a 250 MB bundle limit
  that a static ffmpeg build alone can blow through.
- MCP over stdio needs a **long-lived process**; serverless is request-scoped.
- `yt-dlp` from datacenter IPs is routinely blocked by Twitch and Kick, so ingest would fail even
  if the rest ran.

Three options that do work, cheapest first:

1. **Run it locally** (recommended, and what this repo is built for). Unlimited, free, uses your GPU.
2. **Expose the local MCP over HTTPS** so you can drive it from anywhere:
   `cloudflared tunnel --url http://localhost:8765` — compute still stays on your machine.
3. **A VPS** if it must be always-on: a CPU box handles 1080p slowly; rent GPU time per second
   (RunPod/Modal) only for the transcription pass if you want to keep the box cheap.

What *can* live on Vercel is a thin dashboard that lists runs and approves clips; the render worker
still has to be the machine with ffmpeg.

---

## Limits worth knowing

- **Kick chat replay** is not exposed the way Twitch/YouTube chat is, so Kick VODs are ranked by
  audio energy unless you pass `--chat-json`. Twitch and YouTube use real chat velocity.
- **Live capture** needs a DVR window (`--live-from-start`); if the platform won't serve it, download
  the finished VOD instead.
- **"Insane moment" is a heuristic**, not taste. Chat burst + emotes finds hype reliably; it will
  never understand context the way you do. That is what the review gate is for.
- **Transcript quality** drives caption quality. `small` is the default for speed; use
  `--asr-model large-v3` on a GPU for accented or overlapping speech.
- **The pipeline never posts without approval.** `post` with no approvals exits non-zero by design.

## Legal, briefly

Clipping someone else's stream without permission is copyright infringement in practice — editing,
captions and credit don't change ownership. Clean routes: a **clipping campaign** brief (Content
Rewards/Whop, Vyro, Clipster — the brief is your licence), **written permission** covering platforms
and monetisation, or **your own gameplay**. YouTube also demonetises "reused content" even with
permission, and three strikes in 90 days ends a channel. Disclose paid clips inside the video.
Not legal advice.
