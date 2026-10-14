"""YTTOMP4 backend v2.1.0 — SELF-HOSTED COBALT (no daily cookies).

Same plugin contract as v1 (WordPress plugin v1.0.2 works unchanged):
    POST /api/convert  { url, format, quality } -> { job_id, status }
    GET  /api/status/{job_id} -> { status, download_url, title, ... }
    GET  /api/health (public) -> { status: ok, version }

Why v2.1.0 (what v2.0.0 got wrong):
    v2.0.0 used the OLD Cobalt v7 API (POST /api/json with
    isAudioOnly/aFormat). That API was shut down on 11 Nov 2024, so
    every conversion failed with "cobalt v7 api has been shut down".
    v2.1.0 uses the CURRENT Cobalt v10 API (POST to the instance base
    URL with downloadMode/audioFormat) against OUR OWN self-hosted
    instance. Verified live 2026-10-14: public instances
    (api.cobalt.tools + 3 others) all return
    error.api.auth.jwt.missing — they now require auth and cannot be
    used free. Self-hosted Cobalt has no auth by default.

How it works (no cookies, no daily manual work):
    1. PRIMARY: Self-hosted Cobalt v10 (COBALT_API_URL, default
       https://yttomp4-cobalt.onrender.com). Backend sends the
       YouTube URL, Cobalt returns a direct download URL
       (tunnel / redirect / picker). No yt-dlp, no ffmpeg, no
       cookies for this path.
    2. FALLBACK: local yt-dlp (no cookies). Used only if Cobalt fails.
       This path can still hit YouTube's bot-check on datacenter IPs —
       that is a YouTube-side block, not a bug in this code.
    3. Metadata (title / author / thumbnail) comes from YouTube oEmbed,
       which needs no key and no cookies.

Honest limits:
    - Self-hosted Cobalt on Render Free sleeps when idle (cold start)
      and YouTube may still block a datacenter IP. If Cobalt fails
      AND yt-dlp is bot-blocked, the job fails with a clear error.
    - Cobalt tunnel/redirect URLs are temporary — the visitor should
      download promptly; do not store them for days.
    - Only convert videos you have the rights to download/convert,
      and respect YouTube's Terms of Service and copyright law.
    - Set COBALT_API_URL to your own Cobalt instance URL.
      Set COBALT_API_KEY only if your instance needs one
      (self-hosted default does not).
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yttomp4-backend")

# ---------------- Settings (from Render Environment variables) ----------------

def _get(name, default):
    return os.environ.get(name, default)


def _get_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


API_KEY = _get("API_KEY", "change-me-to-a-long-random-key")
PUBLIC_BASE_URL = _get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
DOWNLOAD_DIR = _get("DOWNLOAD_DIR", "downloads")
CORS_ORIGINS = [o.strip() for o in _get("CORS_ORIGINS", "*").split(",") if o.strip()]
MAX_DURATION_SECONDS = _get_int("MAX_DURATION_SECONDS", 3600)
MAX_FILE_AGE_HOURS = _get_int("MAX_FILE_AGE_HOURS", 24)
MAX_CONCURRENT_JOBS = _get_int("MAX_CONCURRENT_JOBS", 3)
RATE_LIMIT_CONVERT_PER_MIN = _get_int("RATE_LIMIT_CONVERT_PER_MIN", 30)
RATE_LIMIT_STATUS_PER_MIN = _get_int("RATE_LIMIT_STATUS_PER_MIN", 120)
YTDLP_TIMEOUT_SECONDS = _get_int("YTDLP_TIMEOUT_SECONDS", 600)
COBALT_API_URL = _get("COBALT_API_URL", "https://yttomp4-cobalt.onrender.com").strip().rstrip("/")
COBALT_API_KEY = _get("COBALT_API_KEY", "").strip()
COBALT_TIMEOUT_SECONDS = _get_int("COBALT_TIMEOUT_SECONDS", 60)
VERSION = "2.1.0"

# ---------------- Shared validation ----------------

ALLOWED_FORMATS = {"mp4", "mp3", "shorts", "thumbnail"}
ALLOWED_QUALITIES = {
    "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p",
    "320kbps", "256kbps", "192kbps", "128kbps",
}
ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtube-nocookie.com",
    "www.youtube-nocookie.com", "youtu.be", "www.youtu.be",
}
_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:[^/]+/.+/|(?:v|e(?:mbed)?)/|.*[?&]v=)"
    r"|youtu\.be/|youtube\.com/shorts/|youtube\.com/live/)"
    r"([a-zA-Z0-9_-]{11})"
)
_QUALITY_HEIGHT = {
    "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720,
    "480p": 480, "360p": 360, "240p": 240, "144p": 144,
}
_AUDIO_QUALITY = {"320kbps": "0", "256kbps": "2", "192kbps": "4", "128kbps": "6"}
_COBALT_QUALITY = {
    "2160p": "2160", "1440p": "1440", "1080p": "1080", "720p": "720",
    "480p": "480", "360p": "360", "240p": "240", "144p": "144",
}


class ConversionError(Exception):
    pass


def extract_video_id(url):
    if not url or len(url) > 2048:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if host not in ALLOWED_HOSTS:
        return None
    match = _VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def _clean_error(text):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    text = " ".join(text.strip().split())
    if len(text) > 500:
        text = text[:500] + "…"
    return text or "Conversion failed."


def _format_duration(seconds):
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _http_json(method, url, payload=None, timeout=30):
    """Small stdlib JSON client. Returns parsed JSON dict. Raises on error."""
    data = None
    headers = {"Accept": "application/json", "User-Agent": "YTTOMP4-Backend/2.1.0"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise ConversionError(f"Upstream HTTP {exc.code}: {_clean_error(body)}")
    except urllib.error.URLError as exc:
        raise ConversionError(f"Could not reach upstream service: {_clean_error(str(exc.reason or exc))}")
    except TimeoutError:
        raise ConversionError("Upstream service timed out. Please try again.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ConversionError("Upstream service returned an unexpected response.")
    if not isinstance(parsed, dict):
        raise ConversionError("Upstream service returned an unexpected response.")
    return parsed


def fetch_metadata_oembed(url, video_id):
    """Title/author/thumbnail via YouTube oEmbed — no key, no cookies."""
    thumb = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
    try:
        oembed_url = (
            "https://www.youtube.com/oembed?url="
            + urllib.parse.quote(url, safe="")
            + "&format=json"
        )
        data = _http_json("GET", oembed_url, timeout=15)
        return {
            "title": (str(data.get("title") or "")[:255]),
            "author": (str(data.get("author_name") or "")[:255]),
            "duration": "",
            "thumbnail": (str(data.get("thumbnail_url") or "") or thumb),
        }
    except ConversionError:
        return {"title": "", "author": "", "duration": "", "thumbnail": thumb}
    except Exception:
        return {"title": "", "author": "", "duration": "", "thumbnail": thumb}


# ---------------- Cobalt (primary, no cookies) ----------------

def _pick_from_picker(picker, want_audio):
    """Pick the best URL from a Cobalt picker list."""
    if not isinstance(picker, list):
        return ""
    # Prefer matching type, else first item with a URL.
    for item in picker:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        typ = str(item.get("type") or "").lower()
        if not url:
            continue
        if want_audio and "audio" in typ:
            return url
        if (not want_audio) and "video" in typ:
            return url
    for item in picker:
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])
    return ""


def cobalt_convert(url, fmt, quality):
    """Ask self-hosted Cobalt v10 for a direct download URL.

    v10 API: POST to the instance BASE URL (no /api/json), with
    downloadMode / audioFormat / videoQuality. Raises ConversionError.
    """
    if not COBALT_API_URL:
        raise ConversionError("Cobalt API URL is not configured.")
    want_audio = fmt == "mp3"
    payload = {
        "url": url,
        "filenameStyle": "basic",
        "downloadMode": "audio" if want_audio else "video",
    }
    if want_audio:
        payload["audioFormat"] = "mp3"
    else:
        payload["videoQuality"] = _COBALT_QUALITY.get(quality, "720")
    # Self-hosted Cobalt needs no key by default. If a key is configured,
    # send it as Api-Key (Cobalt v10 auth header).
    endpoint = COBALT_API_URL.rstrip("/") + "/"
    if COBALT_API_KEY:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data, method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {COBALT_API_KEY}",
                "User-Agent": "YTTOMP4-Backend/2.1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=COBALT_TIMEOUT_SECONDS) as resp:
                parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise ConversionError(f"Cobalt failed (Upstream HTTP {exc.code}: {_clean_error(body)})")
        except Exception as exc:
            raise ConversionError(f"Cobalt request failed: {_clean_error(str(exc))}")
        if not isinstance(parsed, dict):
            raise ConversionError("Cobalt returned an unexpected response.")
    else:
        parsed = _http_json("POST", endpoint, payload, timeout=COBALT_TIMEOUT_SECONDS)

    status = str(parsed.get("status") or "").lower()
    if status in ("redirect", "tunnel", "stream"):
        direct = str(parsed.get("url") or "")
        if not direct.startswith(("http://", "https://")):
            raise ConversionError("Cobalt returned no download URL.")
        return direct
    if status == "picker":
        direct = _pick_from_picker(parsed.get("picker"), want_audio)
        if direct.startswith(("http://", "https://")):
            return direct
        raise ConversionError("Cobalt returned no usable download option.")
    if status == "error":
        err = parsed.get("error") or parsed.get("text") or ""
        # Cobalt v10 error objects are like {"code": "...", "context": {...}}
        if isinstance(err, dict):
            err = str(err.get("code") or err)
        raise ConversionError(f"Cobalt error: {_clean_error(str(err))}")
    raise ConversionError(f"Cobalt returned unexpected status: {_clean_error(status)}")


# ---------------- yt-dlp fallback (no cookies) ----------------

def _run(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ConversionError(
            "yt-dlp is not installed on this server. "
            "Install it (pip install yt-dlp) and restart the backend."
        )
    except subprocess.TimeoutExpired:
        raise ConversionError("Conversion timed out. Try a shorter video.")


def find_output_file(job_id):
    download_dir = Path(DOWNLOAD_DIR)
    candidates = sorted(download_dir.glob(f"{job_id}.*"))
    for ext in (".mp4", ".mp3", ".jpg", ".webp", ".png", ".m4a"):
        for path in candidates:
            if path.suffix.lower() == ext and path.is_file():
                return path
    for path in candidates:
        if path.is_file() and path.suffix.lower() not in (".part", ".temp", ".ytdl"):
            return path
    return None


def ytdlp_convert(url, fmt, quality, job_id, metadata):
    download_dir = Path(DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(download_dir / f"{job_id}.%(ext)s")
    if fmt == "thumbnail":
        cmd = ["yt-dlp", "--skip-download", "--write-thumbnail",
               "--convert-thumbnails-to", "jpg",
               "--no-playlist", "--no-warnings",
               "-o", output_template, url]
    elif fmt == "mp3":
        cmd = ["yt-dlp", "-x", "--audio-format", "mp3",
               "--audio-quality", _AUDIO_QUALITY.get(quality, "0"),
               "--no-playlist", "--no-warnings",
               "-o", output_template, url]
    else:
        height = _QUALITY_HEIGHT.get(quality, 1080)
        format_selector = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )
        cmd = ["yt-dlp", "-f", format_selector,
               "--merge-output-format", "mp4",
               "--no-playlist", "--no-warnings",
               "-o", output_template, url]
    result = _run(cmd, timeout=YTDLP_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ConversionError("Conversion failed: " + _clean_error(result.stderr))
    output = find_output_file(job_id)
    if output is None:
        raise ConversionError("Conversion finished but no file was produced.")
    return {
        "filename": output.name,
        "download_url": f"{PUBLIC_BASE_URL}/files/{output.name}",
        "title": metadata["title"],
        "author": metadata["author"],
        "duration": metadata["duration"],
        "thumbnail": metadata["thumbnail"],
    }


def convert(url, fmt, quality, job_id):
    video_id = extract_video_id(url) or ""
    metadata = fetch_metadata_oembed(url, video_id)
    # Thumbnail: no conversion needed — return the thumbnail URL directly.
    if fmt == "thumbnail":
        thumb = metadata["thumbnail"] or f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
        return {
            "filename": "",
            "download_url": thumb,
            "title": metadata["title"],
            "author": metadata["author"],
            "duration": "",
            "thumbnail": thumb,
        }
    # Primary: Cobalt (no cookies, no daily work).
    try:
        direct = cobalt_convert(url, fmt, quality)
        logger.info("Job %s: Cobalt OK (%s)", job_id, fmt)
        return {
            "filename": "",
            "download_url": direct,
            "title": metadata["title"],
            "author": metadata["author"],
            "duration": metadata["duration"],
            "thumbnail": metadata["thumbnail"],
        }
    except ConversionError as exc:
        logger.warning("Job %s: Cobalt failed, trying yt-dlp fallback: %s", job_id, exc)
        cobalt_error = str(exc)
    # Fallback: local yt-dlp (no cookies). May still hit YouTube bot-check.
    try:
        return ytdlp_convert(url, fmt, quality, job_id, metadata)
    except ConversionError as exc:
        raise ConversionError(
            f"Cobalt failed ({cobalt_error}). Fallback also failed: {exc}"
        )


# ---------------- API ----------------

app = FastAPI(title="YTTOMP4 Backend", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

jobs = {}
jobs_lock = threading.Lock()
job_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)
rate = {}
rate_lock = threading.Lock()


def _check_rate_limit(request, endpoint, limit):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    key = (ip, endpoint)
    with rate_lock:
        stamps = [t for t in rate.get(key, []) if now - t < 60]
        if len(stamps) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.")
        stamps.append(now)
        rate[key] = stamps


def _require_api_key(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing API key.")
    token = authorization[len("Bearer "):].strip()
    if not token or token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")


class ConvertRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    format: str = "mp4"
    quality: str = "1080p"


def _run_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return
    acquired = job_semaphore.acquire(timeout=300)
    if not acquired:
        with jobs_lock:
            job["status"] = "failed"
            job["error"] = "Server is busy. Please try again."
        return
    try:
        result = convert(job["url"], job["format"], job["quality"], job_id)
        with jobs_lock:
            job.update({"status": "completed", "title": result["title"],
                        "author": result["author"], "duration": result["duration"],
                        "thumbnail": result["thumbnail"], "filename": result.get("filename", ""),
                        "download_url": result["download_url"]})
        logger.info("Job %s completed", job_id)
    except ConversionError as exc:
        with jobs_lock:
            job["status"] = "failed"
            job["error"] = str(exc)
        logger.warning("Job %s failed: %s", job_id, exc)
    except Exception:
        logger.exception("Job %s crashed", job_id)
        with jobs_lock:
            job["status"] = "failed"
            job["error"] = "Unexpected server error during conversion."
    finally:
        job_semaphore.release()


def _cleanup_loop():
    while True:
        time.sleep(3600)
        try:
            cutoff = time.time() - MAX_FILE_AGE_HOURS * 3600
            download_dir = Path(DOWNLOAD_DIR)
            if download_dir.exists():
                for path in download_dir.iterdir():
                    try:
                        if path.is_file() and path.stat().st_mtime < cutoff:
                            path.unlink(missing_ok=True)
                    except OSError:
                        continue
            with jobs_lock:
                stale = [jid for jid, job in jobs.items() if job["created_at"] < cutoff]
                for jid in stale:
                    jobs.pop(jid, None)
        except Exception:
            logger.exception("Cleanup loop error (ignored).")


@app.on_event("startup")
def _startup():
    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    if API_KEY in ("", "change-me-to-a-long-random-key"):
        logger.warning("API_KEY is still the default placeholder — set a real one in Render Environment!")
    threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.get("/api/health")
def health():
    return {"status": "ok", "version": VERSION, "mode": "self-hosted-cobalt"}


@app.get("/")
def root():
    return {"service": "YTTOMP4 Backend", "version": VERSION,
            "mode": "self-hosted-cobalt",
            "health": "/api/health", "convert": "POST /api/convert",
            "status": "GET /api/status/{job_id}"}


@app.post("/api/convert")
def start_conversion(body: ConvertRequest, request: Request, _auth: None = Depends(_require_api_key)):
    _check_rate_limit(request, "convert", RATE_LIMIT_CONVERT_PER_MIN)
    fmt = (body.format or "mp4").lower().strip()
    quality = (body.quality or "1080p").lower().strip()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="Invalid format.")
    if quality not in ALLOWED_QUALITIES:
        raise HTTPException(status_code=400, detail="Invalid quality.")
    video_id = extract_video_id(body.url)
    if video_id is None:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"job_id": job_id, "status": "processing", "url": body.url,
                        "format": fmt, "quality": quality, "video_id": video_id,
                        "title": "", "author": "", "duration": "", "thumbnail": "",
                        "filename": "", "download_url": "", "error": "",
                        "created_at": time.time()}
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/status/{job_id}")
def get_status(job_id: str, request: Request, _auth: None = Depends(_require_api_key)):
    _check_rate_limit(request, "status", RATE_LIMIT_STATUS_PER_MIN)
    if not re.fullmatch(r"[a-zA-Z0-9_.\-]+", job_id or "") or len(job_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid job ID.")
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] == "completed":
        return {"status": "completed", "download_url": job["download_url"],
                "title": job["title"], "author": job["author"],
                "duration": job["duration"], "thumbnail": job["thumbnail"],
                "downloads": [{"quality": job["quality"], "format": job["format"].upper(),
                              "size": "", "url": job["download_url"]}]}
    if job["status"] == "failed":
        return {"status": "failed", "error": job["error"] or "Conversion failed."}
    return {"status": "processing"}


@app.get("/files/{filename}")
def download_file(filename: str):
    if not re.fullmatch(r"[a-f0-9]{32}\.(mp4|mp3|jpg|webp|png|m4a)", filename or ""):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = Path(DOWNLOAD_DIR) / filename
    try:
        resolved = path.resolve()
        base = Path(DOWNLOAD_DIR).resolve()
        if base not in resolved.parents and resolved != base:
            raise HTTPException(status_code=400, detail="Invalid filename.")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="File not found.")
    except HTTPException:
        raise
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(str(resolved), filename=filename, media_type="application/octet-stream")


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"message": exc.detail})
