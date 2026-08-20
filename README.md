# Sourcer

Local web UI for downloading YouTube videos with yt-dlp — with trimming, MP3
extraction, and built-in handling for the auth/proxy problems that usually break
downloaders.

## Run

```powershell
.venv\Scripts\python.exe app.py
```

Then open http://127.0.0.1:8765

First-time setup (already done if `.venv` exists):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### Prerequisites — install these before reporting a broken download

Missing any of these produces a confusing cascade of `HTTP Error 403`,
`Requested format is not available`, and `The page needs to be reloaded` errors
that look like an auth/cookie problem but are not. Open
http://127.0.0.1:8765/api/preflight to see which are missing on a given machine.

| Requirement | Why | Notes |
|---|---|---|
| **ffmpeg + ffprobe** on PATH | merging, trimming, MP3 | |
| **Node >= 22** on PATH | solves YouTube's `n`-signature challenge via `yt-dlp-ejs` | [nodejs.org](https://nodejs.org). Without it, several player clients return **no downloadable formats**. |
| **yt-dlp nightly** | stable lags YouTube by weeks | `requirements.txt` pins the nightly channel on purpose — see the comment there. A *stable* yt-dlp makes **every** format 403. |
| Docker Desktop | runs the bgutil PO-token provider | Optional in practice; useful for videos whose streams are withheld without a PO token. |

## Features

- **Video download** — pick max resolution (from the qualities the video actually
  has) and container (MP4 / MKV / WebM).
- **MP3 extraction** — bitrate selectable from 64 to 320 kbps.
- **Trim** — check "Trim to a section", set start/end via the slider or typed
  timestamps (`m:ss` or `h:mm:ss`); only that range is downloaded. "Precise cut"
  re-encodes at the cut points for frame-exact boundaries (slower); without it,
  cuts snap to the nearest keyframe (fast, may include ~1–5 s extra).
- **Trim + MP3 combine** — trim a section and convert it to MP3 in one go.

## When downloads break ("Sign in to confirm you're not a bot", 403, 429…)

The app already auto-retries every request across multiple YouTube player
clients (default → web_safari → mweb → tv) before reporting failure. If it still
fails, **check http://127.0.0.1:8765/api/preflight first** — a stale yt-dlp or a
missing Node produces errors that look exactly like an auth problem, and the
cookie advice below will waste your time if that's what's actually wrong.

Then open **⚙️ Settings** and fix it once — the fix persists:

1. **Update yt-dlp** — the #1 cause of sudden breakage, by a wide margin.
   One click (it tracks the nightly channel), then restart the app.
2. **Cookies** — fixes login-required, age-restricted, and bot-check errors.
   Recommended: install the *Get cookies.txt LOCALLY* browser extension, export
   cookies while logged in to youtube.com, paste into Settings. (Reading
   directly from a Chromium browser also works but Windows may block it while
   the browser is open.)
3. **Proxy** — if your IP is rate-limited/blocked, set `http://…` or
   `socks5://…` proxy; it's used for all requests from then on.

Settings live in `data/config.json`, cookies in `data/cookies.txt`, downloads
in `downloads/`.

---

## Subtitles tool (`/subtitles.html`)

A separate tool in the same app: upload a video, auto-generate captions with AI,
edit them, style them, and render a subtitled video.

**Flow**

1. **Upload** a video (drag/drop or pick) — MP4/MKV/MOV/WebM/AVI.
2. **Generate subtitles with AI** — the audio is extracted and transcribed to
   word-level timestamps by **Gemini 2.5 Flash, reached through the Vercel AI
   Gateway** using only `AI_GATEWAY_API_KEY`. Long videos are chunked
   automatically to fit the gateway's inline-audio limit.
3. **Edit** — every caption line is editable (text + start/end). Click a row to
   jump the player there; double-click a timestamp to seek. Add/delete lines.
   The live preview overlays your captions on the video as you type and restyle.
4. **Style** — font, size, position, text/highlight/outline colors, outline &
   shadow thickness, bold/italic/UPPERCASE, background (none / solid box /
   translucent), and a **karaoke** mode where each word lights up as it's spoken.
   Four one-click presets (Clean, Viral, Boxed, Minimal).
5. **Render** — *Burn into video* (styled captions hard-baked via ffmpeg+libass)
   or *Soft subtitles* (toggleable `mov_text` track). A `.srt` sidecar is always
   produced too. Output lands in `downloads/`.

**The gateway key**

The AI transcription needs a Vercel AI Gateway key in `D:\Projs\Sourcer\.env`:

```
AI_GATEWAY_API_KEY=vck_...
```

This file already contains the same key used by the Media-Sourcing project.
Keep it private — it's a secret and is not meant to be committed. The header of
the Subtitles page shows whether the key is detected.

`ffmpeg` is required (extraction, burn-in, muxing). Uploaded videos + working
files are kept per-session under `subtitles_work/`.
