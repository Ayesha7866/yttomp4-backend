"""YTTOMP4 backend — FLAT version (no folders, no coding knowledge needed).

Everything lives in this ONE file: settings + converter + API.
Upload this file + Dockerfile + requirements.txt to GitHub (all at root)
and Render will deploy it.

Contract (must match the WordPress plugin YTTOMP4 Converter Engine):
    POST /api/convert  { url, format, quality } -> { job_id, status }
    GET  /api/status/{job_id} -> { status, download_url, title, ... }
    GET  /api/health (public) -> { status: ok }

Legal note: only convert videos you have the rights to download/convert,
and respect YouTube's Terms of Service and applicable copyright law.

Cookies (v1.0.1): YouTube blocks datacenter IPs ("Sign in to confirm
you're not a bot"). If a cookies.txt file is available, its path is passed
to yt-dlp via --cookies. Set COOKIES_FILE in Render Environment, or add a
Render Secret File named cookies.txt (mounted at /etc/secrets/cookies.txt).
NEVER commit cookies.txt to GitHub — it is a login session. Use a spare
YouTube account, never your main account.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
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
COOKIES_FILE = _get("COOKIES_FILE", "").strip()
VERSION = "1.0.1"

# ---------------- Converter (yt-dlp + ffmpeg) ----------------

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


class ConversionError(Exception):
    pass


def _cookies_args():
    """Return yt-dlp --cookies args if a usable cookies.txt exists, else [].

    Priority: COOKIES_FILE env var, then Render Secret File
    /etc/secrets/cookies.txt, then ./cookies.txt (local testing only).
    The file must exist and be non-empty; its contents are never logged.
    """
    candidates = []
    if COOKIES_FILE:
        candidates.append(COOKIES_FILE)
    candidates.append("/etc/secrets/cookies.txt")
    candidates.append("cookies.txt")
    for path in candidates:
        try:
            p = Path(path)
            if p.is_file() and p.stat().st_size > 0:
                return ["--cookies", str(p)]
        except OSError:
            continue
    return []


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


def fetch_metadata(url):
    result = _run(
        ["yt-dlp", *_cookies_args(), "--dump-json", "--no-download",
         "--no-playlist", "--no-warnings", url],
        timeout=60,
    )
    if result.returncode != 0:
        raise ConversionError("Could not read this video: " + _clean_error(result.stderr))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ConversionError("Could not read this video's information.")
    duration = data.get("duration") or 0
    if duration and duration > MAX_DURATION_SECONDS:
        raise ConversionError(
            f"Video is too long ({int(duration // 60)} min). "
            f"Maximum allowed is {MAX_DURATION_SECONDS // 60} minutes."
        )
    return {
        "title": (data.get("title") or "")[:255],
        "author": (data.get("uploader") or data.get("channel") or "")[:255],
        "duration": _format_duration(duration),
        "thumbnail": data.get("thumbnail") or "",
    }


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


def convert(url, fmt, quality, job_id):
    download_dir = Path(DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    metadata = fetch_metadata(url)
    output_template = str(download_dir / f"{job_id}.%(ext)s")
    cookie_args = _cookies_args()
    if fmt == "thumbnail":
        cmd = ["yt-dlp", *cookie_args, "--skip-download", "--write-thumbnail",
               "--convert-thumbnails-to", "jpg",
               "--no-playlist", "--no-warnings",
               "-o", output_template, url]
    elif fmt == "mp3":
        cmd = ["yt-dlp", *cookie_args, "-x", "--audio-format", "mp3",
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
        cmd = ["yt-dlp", *cookie_args, "-f", format_selector,
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
        "title": metadata["title"],
        "author": metadata["author"],
        "duration": metadata["duration"],
        "thumbnail": metadata["thumbnail"],
    }


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
        download_url = f"{PUBLIC_BASE_URL}/files/{result['filename']}"
        with jobs_lock:
            job.update({"status": "completed", "title": result["title"],
                        "author": result["author"], "duration": result["duration"],
                        "thumbnail": result["thumbnail"], "filename": result["filename"],
                        "download_url": download_url})
        logger.info("Job %s completed: %s", job_id, result["filename"])
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
    return {"status": "ok", "version": VERSION}


@app.get("/")
def root():
    return {"service": "YTTOMP4 Backend", "version": VERSION,
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
