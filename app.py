"""
Sourcer — local web UI for yt-dlp.

Features:
  - Probe a URL for metadata + available qualities
  - Download video (quality/container selectable) or extract MP3 (bitrate selectable)
  - Trim to a specific time range (fast keyframe cut or precise re-encoded cut)
  - Persistent settings: proxy, cookies (paste or from-browser), player-client fallbacks
  - Automatic retry across YouTube player clients when bot-checks / 403s appear
  - One-click yt-dlp self-update (most recurring failures are a stale yt-dlp)

YouTube stream access (2026): YouTube withholds media streams unless yt-dlp
presents a PO token AND solves the `n` signature challenge. Sourcer handles both
automatically — see potoken.py for the auto-managed bgutil token provider, and
base_opts() for the Node-backed challenge solver (yt-dlp-ejs).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
import yt_dlp
from yt_dlp.utils import DownloadError, download_range_func
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = ROOT / "downloads"
CONFIG_FILE = DATA_DIR / "config.json"
COOKIES_FILE = DATA_DIR / "cookies.txt"
SUBS_DIR = ROOT / "subtitles_work"
DATA_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)
SUBS_DIR.mkdir(exist_ok=True)


def _load_env():
    """Minimal .env loader (no external dep) for AI_GATEWAY_API_KEY etc."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

import subtitles as subs  # noqa: E402
import potoken  # noqa: E402  PO-token provider lifecycle (YouTube stream access)

# YouTube's `n` signature challenge must be solved for format URLs to work; yt-dlp
# does this via an external JS runtime (needs Node >= 22 + the yt-dlp-ejs scripts).
# Detect Node once so we only enable the runtime when it's actually usable.
_NODE_PATH = shutil.which("node")

app = FastAPI(title="Sourcer")


@app.middleware("http")
async def _no_cache(request, call_next):
    """Local dev tool: never let the browser serve stale HTML/JS/CSS, so UI
    edits always show up on a normal refresh (no hard-reload needed)."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "proxy": "",                      # e.g. http://user:pass@host:port or socks5://...
    "cookies_mode": "none",           # none | file | browser
    "cookies_browser": "firefox",     # used when cookies_mode == "browser"
    # Retry order. Only clients that honour account cookies are useful here —
    # the mobile app clients (android/ios) ignore cookies entirely, so on a
    # bot-checked video they fail no matter what you set, wasting retries and
    # masking the fact that a web client + cookies would have worked.
    "player_clients": ["default", "web_safari", "mweb", "tv"],  # retry order
    "concurrent_fragments": 4,
    "rate_limit_kbps": 0,             # 0 = unlimited
}

_settings_lock = threading.Lock()


# Client lists that predate the cookie-aware defaults; upgraded on load so
# existing installs don't keep retrying clients that ignore cookies.
_LEGACY_CLIENT_LISTS = (
    ["default", "android", "ios", "tv"],
    ["default", "android", "ios"],
    ["android", "ios"],
)


def load_settings() -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if saved.get("player_clients") in _LEGACY_CLIENT_LISTS:
                saved["player_clients"] = list(DEFAULT_SETTINGS["player_clients"])
            return {**DEFAULT_SETTINGS, **saved}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(s: dict) -> None:
    with _settings_lock:
        CONFIG_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- yt-dlp opts

BOT_CHECK_PATTERNS = re.compile(
    r"sign in to confirm|not a bot|captcha|confirm your age|age.restricted|"
    r"http error 403|http error 429|unable to extract|po.token|"
    r"failed to extract any player response",
    re.IGNORECASE,
)

# YouTube handed back the page but withheld the actual media streams, leaving
# only storyboard thumbnails. This is the GVS PO-token signature — cookies get
# you identity but not streams — NOT a stale-cookie / bot problem, so it needs
# its own message. (Kept out of BOT_CHECK_PATTERNS so it isn't misreported.)
NO_FORMATS_PATTERNS = re.compile(
    r"requested format is not available|only images are available|"
    r"no video formats found|only storyboards",
    re.IGNORECASE,
)


def base_opts(settings: dict) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": int(settings.get("concurrent_fragments", 4)),
        "restrictfilenames": False,
        "windowsfilenames": True,
    }
    if _NODE_PATH:
        # Enable the Node JS runtime so yt-dlp can solve YouTube's n-sig challenge
        # (via yt-dlp-ejs). Without it, format URLs come back missing/throttled
        # even when a PO token was obtained.
        opts["js_runtimes"] = {"node": {"path": _NODE_PATH}}
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    if settings.get("rate_limit_kbps"):
        opts["ratelimit"] = int(settings["rate_limit_kbps"]) * 1024
    mode = settings.get("cookies_mode", "none")
    if mode == "file" and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    elif mode == "browser":
        opts["cookiesfrombrowser"] = (settings.get("cookies_browser", "firefox"),)
    return opts


def client_variants(settings: dict):
    """Yield ydl-opt overlays for each player client in the configured retry order."""
    seen = set()
    for client in settings.get("player_clients", DEFAULT_SETTINGS["player_clients"]):
        if client in seen:
            continue
        seen.add(client)
        if client == "default":
            yield client, {}
        else:
            yield client, {"extractor_args": {"youtube": {"player_client": [client]}}}


def run_with_fallbacks(settings: dict, extra_opts: dict, url: str, download: bool):
    """Run yt-dlp, retrying across player clients on bot-check style failures.

    Returns (info, client_used). Raises DownloadError with an actionable
    message if every variant fails.
    """
    last_err = None
    no_formats = False
    for client, overlay in client_variants(settings):
        opts = {**base_opts(settings), **extra_opts, **overlay}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                return info, client
        except DownloadError as e:
            last_err = e
            if NO_FORMATS_PATTERNS.search(str(e)):
                no_formats = True
                continue  # a different client might still surface real streams
            if BOT_CHECK_PATTERNS.search(str(e)):
                continue  # try next client
            raise
    msg = str(last_err) if last_err else "unknown error"
    had_cookies = settings.get("cookies_mode", "none") != "none"
    if no_formats:
        # Streams withheld on every client — a PO token is required. Cookies
        # alone can't fix this (unless the account has YouTube Premium).
        hint = (
            " — YouTube returned no downloadable streams for this video (only "
            "storyboard thumbnails), on every client. This means it now requires a "
            "PO token to release the media, which cookies alone don't provide. "
            "Fixes: 1) run a PO-token provider (bgutil) so yt-dlp mints tokens "
            "automatically — the durable fix; 2) use cookies from a YouTube Premium "
            "account, which are exempt from the PO-token requirement; 3) try a "
            "different network/proxy, as flagged IPs are hit with this first."
        )
    elif had_cookies:
        hint = (
            " — Bot check hit even with cookies. Your cookies have most likely "
            "gone stale: open youtube.com in the same browser/account, make sure "
            "you're still logged in, then re-export and re-save the cookies. "
            "(Tip: 'Read from browser' mode re-reads live cookies every run, so it "
            "never goes stale.) If it still fails, your IP may be flagged — set a proxy."
        )
    else:
        hint = (
            " — YouTube wants a signed-in session. In Settings, set Cookies: either "
            "paste a cookies.txt export, or use 'Read from browser' (Firefox is most "
            "reliable on Windows) so a logged-in session is re-read on every download."
        )
    raise DownloadError(msg + hint)


# ---------------------------------------------------------------- jobs

jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _update_job(job_id: str, **kw):
    with _jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw)


class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"            # video | audio
    quality: str = "best"          # video: max height ("1080") or "best"; audio: mp3 kbps
    container: str = "mp4"         # video container: mp4 | mkv | webm
    start: float | None = None     # trim start, seconds
    end: float | None = None       # trim end, seconds
    precise_cut: bool = False      # re-encode at cut points (accurate but slower)
    reencode_h264: bool = True     # transcode video to H.264/AAC (Premiere/editor safe)


def _progress_hook_factory(job_id: str):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            _update_job(
                job_id,
                status="downloading",
                progress=round(pct, 1),
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            _update_job(job_id, status="processing", progress=100)
    return hook


def _postprocessor_hook_factory(job_id: str):
    def hook(d):
        if d["status"] == "started":
            name = d.get("postprocessor", "")
            label = "Converting to MP3" if "ExtractAudio" in name else "Processing"
            _update_job(job_id, status="processing", detail=label)
    return hook


def _build_download_opts(req: DownloadRequest, job_id: str) -> dict:
    opts: dict = {
        "outtmpl": str(DOWNLOAD_DIR / "%(title).150B [%(id)s].%(ext)s"),
        "progress_hooks": [_progress_hook_factory(job_id)],
        "postprocessor_hooks": [_postprocessor_hook_factory(job_id)],
        "overwrites": True,
    }

    if req.mode == "audio":
        opts["format"] = "bestaudio/best"
        quality = req.quality if req.quality and req.quality != "best" else "192"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            },
            {"key": "FFmpegMetadata"},
        ]
    else:
        container = req.container or "mp4"
        hf = f"[height<={int(req.quality)}]" if req.quality and req.quality != "best" else ""
        if req.reencode_h264:
            # Re-encode path: grab the highest-quality source at the requested
            # resolution regardless of codec — YouTube only serves VP9/AV1 above
            # 1080p, so this is the only way to keep 4K. It gets transcoded to
            # H.264/AAC after download (see _reencode_to_h264) so Premiere and
            # other editors can read it. Output is always MP4 here.
            opts["format"] = f"bestvideo{hf}+bestaudio/best{hf}/best"
            opts["merge_output_format"] = "mp4"
        elif container == "mp4":
            # No re-encode, but still target MP4: prefer native H.264 (avc1) +
            # AAC (mp4a) so it opens without transcoding. Caps at 1080p, since
            # that's YouTube's H.264 ceiling; the fallbacks keep it working.
            opts["format"] = (
                f"bestvideo{hf}[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                f"bestvideo{hf}[ext=mp4]+bestaudio[ext=m4a]/"
                f"best{hf}[ext=mp4]/"
                f"bestvideo{hf}+bestaudio/best{hf}/best"
            )
            opts["merge_output_format"] = container
        else:
            opts["format"] = f"bestvideo{hf}+bestaudio/best{hf}/best"
            opts["merge_output_format"] = container

    if req.start is not None or req.end is not None:
        start = req.start or 0
        end = req.end if req.end is not None else float("inf")
        opts["download_ranges"] = download_range_func(None, [(start, end)])
        # Audio is re-encoded to MP3 anyway, so precise cutting is free there;
        # keyframe-snapped cuts can overshoot by a full DASH fragment.
        opts["force_keyframes_at_cuts"] = bool(req.precise_cut) or req.mode == "audio"

    return opts


def _video_codec(path: Path) -> str | None:
    """Return the video stream's codec name (e.g. 'h264', 'vp9') via ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def _reencode_to_h264(path: Path, job_id: str) -> Path:
    """Transcode a video file to H.264/AAC MP4 in place (editor-compatible).

    No-op if the video is already H.264 (e.g. a native ≤1080p download). YouTube
    only offers VP9/AV1 above 1080p, and Premiere can't read those, so 4K clips
    must be re-encoded. Returns the final path (may differ if the extension was
    not already .mp4).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return path
    if (_video_codec(path) or "").lower() == "h264":
        return path  # already editable — don't re-encode and lose quality

    _update_job(job_id, status="processing", detail="Re-encoding to H.264")
    out = path.with_name(path.stem + ".h264.mp4")
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(path),
         "-c:v", "libx264", "-profile:v", "high", "-crf", "18",
         "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart",
         str(out)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(
            "H.264 re-encode failed: " + (proc.stderr or "unknown ffmpeg error")[-500:]
        )
    # Replace the original with the transcoded MP4, keeping the .mp4 name.
    final = path.with_suffix(".mp4")
    try:
        path.unlink()
    except OSError:
        pass
    out.replace(final)
    return final


def _run_download(job_id: str, req: DownloadRequest):
    settings = load_settings()
    try:
        info, client = run_with_fallbacks(
            settings, _build_download_opts(req, job_id), req.url, download=True
        )
        filepath = None
        rd = info.get("requested_downloads") or []
        if rd:
            filepath = rd[0].get("filepath")
        if not filepath:
            filepath = info.get("filepath") or info.get("_filename")
        if filepath and Path(filepath).exists() and req.mode == "video" and req.reencode_h264:
            filepath = str(_reencode_to_h264(Path(filepath), job_id))
        if filepath and Path(filepath).exists():
            p = Path(filepath)
            _update_job(
                job_id,
                status="done",
                progress=100,
                file=p.name,
                size=p.stat().st_size,
                client=client,
                detail=None,
            )
        else:
            _update_job(job_id, status="error",
                        error="Download finished but output file was not found.")
    except Exception as e:  # noqa: BLE001 — surface everything to the UI
        _update_job(job_id, status="error", error=str(e))


# ---------------------------------------------------------------- API

@app.post("/api/probe")
def probe(body: dict):
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    settings = load_settings()
    try:
        info, client = run_with_fallbacks(
            settings, {"skip_download": True, "playlist_items": "1"}, url, download=False
        )
    except DownloadError as e:
        raise HTTPException(422, str(e)) from e
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise HTTPException(422, "Playlist has no entries")
        info = entries[0]

    heights = sorted(
        {
            f["height"]
            for f in info.get("formats", [])
            if f.get("height") and f.get("vcodec") not in (None, "none")
        },
        reverse=True,
    )
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "heights": heights,
        "client_used": client,
    }


@app.post("/api/download")
def start_download(req: DownloadRequest):
    if not req.url.strip():
        raise HTTPException(400, "url is required")
    if req.mode not in ("video", "audio"):
        raise HTTPException(400, "mode must be 'video' or 'audio'")
    if req.start is not None and req.end is not None and req.end <= req.start:
        raise HTTPException(400, "end must be greater than start")
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "url": req.url,
            "mode": req.mode,
            "status": "queued",
            "progress": 0,
            "created": time.time(),
        }
    threading.Thread(target=_run_download, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        return sorted(jobs.values(), key=lambda j: j["created"], reverse=True)


@app.get("/files/{name}")
def get_file(name: str):
    path = (DOWNLOAD_DIR / name).resolve()
    if not path.is_file() or DOWNLOAD_DIR.resolve() not in path.parents:
        raise HTTPException(404, "file not found")
    return FileResponse(path, filename=path.name)


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    s["has_cookies_file"] = COOKIES_FILE.exists()
    s["ytdlp_version"] = yt_dlp.version.__version__
    s["node_available"] = bool(_NODE_PATH)
    s["pot_provider"] = potoken.status()
    return s


@app.get("/api/pot-status")
def pot_status():
    return potoken.status()


@app.post("/api/pot-restart")
def pot_restart():
    """Force a fresh ensure() — used by the Settings 'retry' button."""
    return potoken.ensure()


@app.post("/api/settings")
def update_settings(body: dict):
    s = load_settings()
    for key in ("proxy", "cookies_mode", "cookies_browser",
                "player_clients", "concurrent_fragments", "rate_limit_kbps"):
        if key in body:
            s[key] = body[key]
    save_settings(s)
    return {"ok": True}


@app.post("/api/cookies")
def set_cookies(body: dict):
    text = body.get("text", "")
    if not text.strip():
        if COOKIES_FILE.exists():
            COOKIES_FILE.unlink()
        return {"ok": True, "has_cookies_file": False}
    if "# Netscape HTTP Cookie File" not in text and "\t" not in text:
        raise HTTPException(
            400,
            "That doesn't look like a Netscape-format cookies.txt export. "
            "Use a browser extension like 'Get cookies.txt LOCALLY' and paste the file contents.",
        )
    COOKIES_FILE.write_text(text, encoding="utf-8")
    s = load_settings()
    s["cookies_mode"] = "file"
    save_settings(s)
    return {"ok": True, "has_cookies_file": True}


@app.post("/api/update-ytdlp")
def update_ytdlp():
    # Track the *nightly* channel, not stable. When YouTube changes something,
    # the fix lands in master within a day or two but the next stable tag can be
    # weeks out — so upgrading to stable is usually a no-op that leaves the app
    # broken (every format 403s). `--pre` picks up the nightly builds yt-dlp
    # publishes to PyPI, which is what the project itself recommends when
    # extraction is failing. The [default] extra keeps the optional deps
    # (websockets, pycryptodomex, ...) in sync with the new build.
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "--pre",
         "yt-dlp[default]"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": result.stderr[-2000:]},
        )
    return {
        "ok": True,
        "output": result.stdout[-500:],
        "note": "Restart the app for the new version to load.",
    }


# ================================================================ subtitles

# Each upload gets a session dir under SUBS_DIR holding the source video,
# the cues JSON, and rendered outputs.
subs_jobs: dict[str, dict] = {}
_subs_lock = threading.Lock()


def _subs_update(job_id: str, **kw):
    with _subs_lock:
        if job_id in subs_jobs:
            subs_jobs[job_id].update(kw)


def _find_source(session: str) -> Path:
    sdir = SUBS_DIR / session
    if not sdir.is_dir():
        raise HTTPException(404, "session not found")
    for f in sdir.glob("source.*"):
        return f
    raise HTTPException(404, "source video missing")


@app.post("/api/subtitles/upload")
async def subs_upload(file: UploadFile = File(...)):
    if not (file.content_type or "").startswith("video") and not (
        file.filename or ""
    ).lower().endswith((".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")):
        raise HTTPException(400, "Please upload a video file.")
    session = uuid.uuid4().hex[:12]
    sdir = SUBS_DIR / session
    sdir.mkdir(parents=True, exist_ok=True)
    ext = (Path(file.filename or "video.mp4").suffix or ".mp4").lower()
    dest = sdir / f"source{ext}"
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    try:
        w, h = subs.video_dimensions(str(dest))
        dur = subs.probe_duration(str(dest))
        audio = subs.has_audio_stream(str(dest))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"Could not read video: {e}")
    return {
        "session": session, "width": w, "height": h,
        "duration": dur, "has_audio": audio, "filename": file.filename,
    }


@app.get("/api/subtitles/video/{session}")
def subs_video(session: str):
    src = _find_source(session)
    return FileResponse(src)


class TranscribeReq(BaseModel):
    session: str


def _run_transcribe(job_id: str, session: str):
    try:
        src = _find_source(session)
        sdir = SUBS_DIR / session

        def prog(i, n):
            _subs_update(job_id, status="transcribing", progress=round(i / n * 100),
                         detail=f"Transcribing audio ({i + 1}/{n})")

        _subs_update(job_id, status="extracting", detail="Extracting audio…")
        words = subs.transcribe_video(str(src), sdir, progress=prog)
        if not words:
            _subs_update(job_id, status="error",
                         error="No speech was detected in this video.")
            return
        cues = subs.build_cues(words)
        (sdir / "cues.json").write_text(json.dumps(cues), encoding="utf-8")
        _subs_update(job_id, status="done", progress=100, cues=cues,
                     word_count=len(words), detail=None)
    except Exception as e:  # noqa: BLE001
        _subs_update(job_id, status="error", error=str(e))


@app.post("/api/subtitles/transcribe")
def subs_transcribe(req: TranscribeReq):
    _find_source(req.session)  # validates session
    job_id = uuid.uuid4().hex[:12]
    with _subs_lock:
        subs_jobs[job_id] = {"id": job_id, "session": req.session,
                             "status": "queued", "progress": 0,
                             "created": time.time()}
    threading.Thread(target=_run_transcribe, args=(job_id, req.session),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/subtitles/job/{job_id}")
def subs_job(job_id: str):
    with _subs_lock:
        job = subs_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


class Cue(BaseModel):
    start: float
    end: float
    text: str
    words: list[dict] | None = None


class KeywordsReq(BaseModel):
    cues: list[Cue]


@app.post("/api/subtitles/keywords")
def subs_keywords(req: KeywordsReq):
    """Mark emphasis-worthy words (*keyword* syntax) in the given cues.

    Uses the AI gateway when configured, else a capitalization/number
    heuristic. Pure text transform — safe to re-run (old marks are replaced).
    """
    cues = [c.model_dump() for c in req.cues]
    cues, engine = subs.detect_keywords(cues)
    return {"cues": cues, "engine": engine}


class RenderReq(BaseModel):
    session: str
    cues: list[Cue]
    style: dict = {}
    mode: str = "burn"          # burn | soft
    crf: int = 20
    export_ratio: str = "source"  # "source" or "W:H" (e.g. "9:16")
    export_fit: str = "crop"      # crop (fill) | pad (letterbox)
    pad_color: str = "#000000"
    sync_offset: float = 0.0      # shift all cues (seconds); + = later, - = earlier


def _style_from_dict(d: dict) -> subs.SubtitleStyle:
    s = subs.SubtitleStyle()
    for k, v in d.items():
        if hasattr(s, k) and v is not None:
            setattr(s, k, v)
    return s


def _run_render(job_id: str, req: RenderReq):
    try:
        src = _find_source(req.session)
        sdir = SUBS_DIR / req.session
        cues = [c.model_dump() for c in req.cues]
        cues = [c for c in cues if c["text"].strip() and c["end"] > c["start"]]
        if not cues:
            _subs_update(job_id, status="error", error="No subtitle cues to render.")
            return
        # Hand-edited text wins over stale word timings (see reconcile_words).
        cues = subs.reconcile_words(cues)
        # Apply the global sync offset (shift the whole track), clamping at 0.
        off = req.sync_offset or 0.0
        if off:
            for c in cues:
                c["start"] = max(0.0, c["start"] + off)
                c["end"] = max(0.0, c["end"] + off)
                for wd in (c.get("words") or []):
                    wd["s"] = max(0.0, wd["s"] + off)
                    wd["e"] = max(0.0, wd["e"] + off)
        style = _style_from_dict(req.style)
        w, h = subs.video_dimensions(str(src))
        # Resolve the export frame (may differ from source when reframing).
        pre_filter, ow, oh = subs.reframe_filter(
            w, h, req.export_ratio, req.export_fit, req.pad_color
        )

        stem = re.sub(r"[^\w\-]+", "_", (req.session))
        if req.mode == "soft":
            _subs_update(job_id, status="rendering", detail="Muxing soft subtitles…")
            srt = sdir / "subs.srt"
            srt.write_text(subs.to_srt(cues), encoding="utf-8")
            out = DOWNLOAD_DIR / f"subtitled_{stem}.mp4"
            subs.mux_soft_subs(str(src), str(srt), str(out))
        else:
            detail = "Burning subtitles (re-encoding video)…"
            if pre_filter:
                detail = f"Reframing to {req.export_ratio} & burning subtitles…"
            _subs_update(job_id, status="rendering", detail=detail)
            ass = sdir / "subs.ass"
            # Build the ASS against the OUTPUT frame so positions/sizes match.
            ass.write_text(subs.build_ass(cues, style, ow, oh), encoding="utf-8")
            out = DOWNLOAD_DIR / f"subtitled_{stem}.mp4"
            subs.burn_subtitles(str(src), str(ass), str(out), crf=req.crf,
                                pre_filter=pre_filter)
        # sidecar files for download too
        (DOWNLOAD_DIR / f"subtitled_{stem}.srt").write_text(
            subs.to_srt(cues), encoding="utf-8")
        _subs_update(job_id, status="done", progress=100, file=out.name,
                     size=out.stat().st_size, srt=f"subtitled_{stem}.srt",
                     detail=None)
    except Exception as e:  # noqa: BLE001
        _subs_update(job_id, status="error", error=str(e))


@app.post("/api/subtitles/render")
def subs_render(req: RenderReq):
    _find_source(req.session)
    job_id = uuid.uuid4().hex[:12]
    with _subs_lock:
        subs_jobs[job_id] = {"id": job_id, "session": req.session,
                             "status": "queued", "progress": 0,
                             "created": time.time()}
    threading.Thread(target=_run_render, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


# ------------------------------------------------------------ user presets

SUB_PRESETS_FILE = DATA_DIR / "subtitle_presets.json"


def _load_sub_presets() -> dict:
    try:
        return json.loads(SUB_PRESETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sub_presets(presets: dict) -> None:
    SUB_PRESETS_FILE.write_text(json.dumps(presets, indent=2), encoding="utf-8")


class SubPresetReq(BaseModel):
    name: str
    style: dict = {}
    export: dict = {}


@app.get("/api/subtitles/presets")
def subs_presets_list():
    return {"presets": _load_sub_presets()}


@app.post("/api/subtitles/presets")
def subs_preset_save(req: SubPresetReq):
    name = req.name.strip()[:40]
    if not name:
        raise HTTPException(400, "preset name required")
    presets = _load_sub_presets()
    presets[name] = {"style": req.style, "export": req.export}
    _save_sub_presets(presets)
    return {"ok": True, "presets": presets}


@app.delete("/api/subtitles/presets/{name}")
def subs_preset_delete(name: str):
    presets = _load_sub_presets()
    presets.pop(name, None)
    _save_sub_presets(presets)
    return {"ok": True, "presets": presets}


@app.get("/api/subtitles/status")
def subs_status():
    has_deepgram = bool((os.environ.get("DEEPGRAM_API_KEY") or "").strip())
    has_gateway = bool((os.environ.get("AI_GATEWAY_API_KEY") or "").strip())
    return {
        "gateway_configured": has_deepgram or has_gateway,
        # Which transcription engine will drive word timing (sync quality).
        "sync_engine": "deepgram" if has_deepgram else ("gemini" if has_gateway else None),
    }


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")


@app.on_event("startup")
def _startup():
    # Bring the PO-token provider up in the background so downloads work without
    # any manual step; never blocks server startup on Docker.
    potoken.ensure_async()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
