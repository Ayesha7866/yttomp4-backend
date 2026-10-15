"""YTTOMP4 backend v2.4.1 — INVIDIOUS DIRECT-LINK + FAIL FAST (FREE, no cookies).

v2.4.1 fix: when Invidious fails (e.g. per-video HTTP 500 from its
companion), v2.4.0 burned ~90s on yt-dlp/Cobalt fallbacks that ALWAYS
fail on Render, and the long request got killed into an HTML error
page, producing the frontend's "Unexpected token '<'" JSON error.
v2.4.1 fails fast with the Invidious error (clean JSON) unless
ENABLE_SLOW_FALLBACKS=1 is set (only for hosts YouTube doesn't block).
Bridge wait also dropped 60s -> 30s in plugin v1.0.4 as a safety net.

v2.4.0 — INVIDIOUS DIRECT-LINK (FREE, no cookies).

Why v2.4.0: v2.3.0 proved Render's IP is blocked for EVERY
server-side extraction path (all 6 yt-dlp clients + self-hosted
Cobalt all get YouTube bot-check / login errors from Render IPs).
v2.4.0 stops trying to download on the server. Instead it asks a
public Invidious instance for the video's stream URLs and returns
the direct Google CDN (googlevideo) URL. The VISITOR's browser then
downloads straight from Google — from the visitor's own residential
/ mobile IP, which YouTube does not block. The server never touches
the video bytes, so the server IP block no longer matters.

Verified 2026-10-15: invidious.f5.si /api/v1/videos/wEZKcOFVs8w
returned real metadata + 1 progressive MP4 formatStream (360p) +
23 adaptive formats. Google's redirect for the stream URL carries
ipbypass=yes (Google serves it regardless of client IP) — but that
was NOT provable from the dev VM (its egress proxy blocks video
streams), so the live visitor-browser test is the decisive one.

Honest limits:
    - Video is ~360p (itag 18 progressive), NOT the requested 720p/
      1080p — progressive streams above 360p are not offered. The
      returned downloads[] row reports the ACTUAL quality (360p).
    - Audio is M4A (itag 140, ~128kbps), NOT MP3 — the browser gets
      an .m4a file. downloads[] reports format M4A honestly.
    - Single Invidious instance dependency: only invidious.f5.si
      currently exposes a public API (api=true, cors=true) in the
      official instances list. If it goes down, jobs fall back to
      yt-dlp / Cobalt (which fail on Render) and fail honestly.
    - googlevideo URLs expire (a few hours) — each Convert mints a
      fresh one, so this is fine for on-demand downloads.

Same plugin contract as v1 (WordPress plugin v1.0.2 works unchanged):
    POST /api/convert  { url, format, quality } -> { job_id, status }
    GET  /api/status/{job_id} -> { status, download_url, title, ... }
    GET  /api/health (public) -> { status: ok, version }

Why v2.2.0 (what was tried before):
    - v1/v2.0/v2.1 yt-dlp with DEFAULT (web) client -> YouTube
      bot-check "Sign in to confirm you're not a bot" on Render IPs.
    - Public Cobalt -> v7 shut down Nov 2024; v10 public instances
      require JWT auth (error.api.auth.jwt.missing).
    - Self-hosted Cobalt on Render -> error.api.youtube.login
      (YouTube blocks Render IPs even via Cobalt).
    - BREAKTHROUGH verified live 2026-10-14: yt-dlp with
      --extractor-args "youtube:player_client=android" BYPASSES the
      bot-check with NO cookies. Full MP4 + MP3 downloads succeeded
      for Shorts ipimnzKYb80 (602 KB MP4 in ~2s) and V3L5rOwYdN4,
      plus bJzfl9pw6kk, from a datacenter IP.

How it works (no cookies, no payment, no daily work):
    1. PRIMARY: local yt-dlp with Android+iOS player clients
       (--extractor-args "youtube:player_client=android,ios").
       Tries android,ios -> android -> ios -> default web.
    2. FALLBACK: self-hosted Cobalt v10 (COBALT_API_URL).
       Used only if all yt-dlp clients fail.
    3. Metadata (title / author / thumbnail) comes from YouTube oEmbed,
       which needs no key and no cookies.

Honest limits:
    - Android client usually returns format 18 (360p combined
      video+audio) for Shorts — so a "720p" request still downloads
      a WORKING MP4, but at ~360p, not true 720p. That is the price
      of bypassing the bot-check free. Longer videos may give 240p.
    - YouTube can close this Android loophole at any time. If it
      does, jobs will fail with a clear error again.
    - Only convert videos you have the rights to download/convert,
      and respect YouTube's Terms of Service and copyright law.
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
# Slow fallbacks (yt-dlp + Cobalt) take ~90s and ALWAYS fail on Render
# (YouTube blocks Render IPs), and the long request makes the web
# server kill the PHP bridge with an HTML error page instead of JSON
# (the "Unexpected token '<'" bug). Default OFF: fail fast with the
# Invidious error so the frontend always gets clean JSON. Set to "1"
# only on a host whose IPs YouTube does NOT block.
ENABLE_SLOW_FALLBACKS = _get("ENABLE_SLOW_FALLBACKS", "0") == "1"
VERSION = "2.4.1"

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
    headers = {"Accept": "application/json", "User-Agent": "YTTOMP4-Backend/2.4.0"}
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
        # Cobalt 11: downloadMode is "auto" | "audio" | "mute" — "video" is invalid.
        "downloadMode": "audio" if want_audio else "auto",
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
                "User-Agent": "YTTOMP4-Backend/2.4.0",
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


def _ytdlp_once(url, fmt, quality, job_id, metadata, player_client, extra_args=None):
    """One yt-dlp attempt with given player_client + extractor args."""
    download_dir = Path(DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(download_dir / f"{job_id}.%(ext)s")
    base = ["yt-dlp", "--no-playlist", "--no-warnings", "--force-ipv4"]
    if player_client:
        args = f"youtube:player_client={player_client}"
        if extra_args:
            args += ";" + extra_args
        base += ["--extractor-args", args]
    if fmt == "thumbnail":
        cmd = base + ["--skip-download", "--write-thumbnail",
                      "--convert-thumbnails-to", "jpg",
                      "-o", output_template, url]
    elif fmt == "mp3":
        cmd = base + ["-x", "--audio-format", "mp3",
                      "--audio-quality", _AUDIO_QUALITY.get(quality, "0"),
                      "-o", output_template, url]
    else:
        height = _QUALITY_HEIGHT.get(quality, 1080)
        format_selector = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )
        cmd = base + ["-f", format_selector,
                      "--merge-output-format", "mp4",
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


def ytdlp_convert(url, fmt, quality, job_id, metadata):
    """yt-dlp with a wide client matrix (bypasses bot-check, no cookies)."""
    last_error = ""
    # (player_client, extra_extractor_args)
    attempts = [
        ("tv,web_embedded", "player_skip=webpage"),
        ("android,ios,mweb,web", "player_skip=webpage"),
        ("android,ios", None),
        ("android", None),
        ("ios", None),
        (None, None),
    ]
    for client, extra in attempts:
        try:
            result = _ytdlp_once(url, fmt, quality, job_id, metadata, client, extra)
            logger.info("yt-dlp OK with player_client=%s", client or "default")
            return result
        except ConversionError as exc:
            last_error = str(exc)
            logger.warning("yt-dlp player_client=%s failed: %s", client or "default", exc)
            # Remove any partial file before the next attempt.
            for path in Path(DOWNLOAD_DIR).glob(f"{job_id}.*"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
    raise ConversionError(last_error or "yt-dlp failed for all player clients.")


# Invidious instances that expose a public API. Verified 2026-10-15:
# invidious.f5.si is the ONLY instance in the official list with
# api=true + cors=true (nadeko etc. have the API disabled).
INVIDIOUS_INSTANCES = ["https://invidious.f5.si"]
INVIDIOUS_TIMEOUT_SECONDS = 20


def _quality_sort_key(entry):
    """Sort video streams best-first by height parsed from size 'WxH'."""
    try:
        height = int(str(entry.get("size", "")).split("x")[1])
    except (IndexError, ValueError):
        height = 0
    return height


def invidious_convert(url, fmt, quality, job_id, metadata):
    """Direct-link via Invidious: return a googlevideo URL for the
    visitor's browser to download. The server downloads nothing.
    Raises ConversionError when Invidious has no usable stream."""
    video_id = extract_video_id(url) or ""
    if not video_id:
        raise ConversionError("Invidious: could not extract video ID.")
    last_error = ""
    for base in INVIDIOUS_INSTANCES:
        data = None
        # One retry: the instance sometimes 500s transiently.
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    f"{base}/api/v1/videos/{video_id}",
                    headers={"Accept": "application/json",
                             "User-Agent": "YTTOMP4-Backend/2.4.1"},
                )
                with urllib.request.urlopen(req, timeout=INVIDIOUS_TIMEOUT_SECONDS) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception as exc:  # noqa: BLE001 - any fetch/parse failure
                last_error = f"{base}: {exc}"
                logger.warning("Job %s: Invidious %s attempt %d failed: %s",
                               job_id, base, attempt, exc)
                if attempt == 1:
                    time.sleep(2)
        if data is None:
            continue
        if isinstance(data, dict) and data.get("error"):
            last_error = f"{base}: {data.get('error')}"
            continue
        title = (data.get("title") or metadata["title"] or "").strip()
        author = (data.get("author") or metadata["author"] or "").strip()
        duration = ""
        try:
            length = int(data.get("lengthSeconds") or 0)
            if length:
                duration = f"{length // 60:02d}:{length % 60:02d}"
        except (TypeError, ValueError):
            pass
        thumbnail = metadata["thumbnail"] or ""
        for thumb in data.get("videoThumbnails") or []:
            if thumb.get("quality") in ("maxres", "maxresdefault"):
                thumbnail = thumb.get("url") or thumbnail
                break
        if fmt == "mp3":
            # Best audio-only adaptive format; prefer M4A (itag 140).
            audios = [a for a in (data.get("adaptiveFormats") or [])
                      if (a.get("type") or "").startswith("audio/")
                      and a.get("url")]
            if not audios:
                last_error = f"{base}: no audio streams"
                continue
            audios.sort(key=lambda a: (
                0 if "audio/mp4" in (a.get("type") or "") else 1,
                -(int(a.get("bitrate") or 0)),
            ))
            best = audios[0]
            kbps = int(best.get("bitrate") or 0) // 1000
            logger.info("Job %s: Invidious OK (audio %s)", job_id,
                        best.get("type"))
            return {
                "filename": "",
                "download_url": best["url"],
                "title": title,
                "author": author,
                "duration": duration or metadata["duration"],
                "thumbnail": thumbnail,
                "actual_format": "M4A",
                "actual_quality": f"{kbps}kbps" if kbps else "Audio",
            }
        # Video: best progressive (video+audio) formatStream.
        streams = [s for s in (data.get("formatStreams") or [])
                   if s.get("url")]
        if not streams:
            last_error = f"{base}: no progressive video streams"
            continue
        streams.sort(key=_quality_sort_key, reverse=True)
        best = streams[0]
        # Label by itag (18 -> 360p even for vertical Shorts, whose
        # size reads 360x640). Fall back to the height if itag unknown.
        itag_quality = {17: "144p", 36: "240p", 34: "360p", 43: "360p",
                        18: "360p", 35: "480p", 22: "720p"}
        try:
            label = itag_quality.get(int(best.get("itag") or 0), "")
        except (TypeError, ValueError):
            label = ""
        if not label:
            try:
                label = f"{int(str(best.get('size', '')).split('x')[1])}p"
            except (IndexError, ValueError):
                label = "Video"
        logger.info("Job %s: Invidious OK (video %s)", job_id,
                    best.get("size"))
        return {
            "filename": "",
            "download_url": best["url"],
            "title": title,
            "author": author,
            "duration": duration or metadata["duration"],
            "thumbnail": thumbnail,
            "actual_format": "MP4",
            "actual_quality": label,
        }
    raise ConversionError(f"Invidious failed: {last_error or 'no streams'}")


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
    # PRIMARY (v2.4.0): Invidious direct-link. The visitor's browser
    # downloads from Google's CDN on its own IP, so the server IP block
    # no longer matters. Fast (~2s) when the instance is up.
    try:
        return invidious_convert(url, fmt, quality, job_id, metadata)
    except ConversionError as exc:
        logger.warning("Job %s: Invidious failed: %s", job_id, exc)
        invidious_error = str(exc)
    # Slow fallbacks are OFF by default (v2.4.1): on Render they take
    # ~90s and always fail, and the long request gets killed into an
    # HTML error page (the "Unexpected token '<'" bug). Fail fast
    # instead so the frontend always gets clean JSON. Enable only on
    # a host whose IPs YouTube does not block (ENABLE_SLOW_FALLBACKS=1).
    if not ENABLE_SLOW_FALLBACKS:
        raise ConversionError(
            f"Invidious failed ({invidious_error}). "
            "Please try again in a moment — the video service is sometimes busy."
        )
    # Fallback 1: yt-dlp with multi-client matrix (works on unblocked hosts).
    try:
        return ytdlp_convert(url, fmt, quality, job_id, metadata)
    except ConversionError as exc:
        logger.warning("Job %s: yt-dlp failed, trying Cobalt: %s", job_id, exc)
        ytdlp_error = str(exc)
    # Fallback 2: self-hosted Cobalt.
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
        raise ConversionError(
            f"Invidious failed ({invidious_error}). "
            f"yt-dlp failed ({ytdlp_error}). Cobalt also failed: {exc}"
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
                        "download_url": result["download_url"],
                        "actual_format": result.get("actual_format", ""),
                        "actual_quality": result.get("actual_quality", "")})
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
    return {"status": "ok", "version": VERSION, "mode": "invidious-direct"}


@app.get("/")
def root():
    return {"service": "YTTOMP4 Backend", "version": VERSION,
            "mode": "invidious-direct",
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
                        "actual_format": "", "actual_quality": "",
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
        # Report the ACTUAL stream quality/format (Invidious: 360p / M4A),
        # not the requested one, so the download table tells the truth.
        return {"status": "completed", "download_url": job["download_url"],
                "title": job["title"], "author": job["author"],
                "duration": job["duration"], "thumbnail": job["thumbnail"],
                "downloads": [{"quality": job.get("actual_quality") or job["quality"],
                              "format": job.get("actual_format") or job["format"].upper(),
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
