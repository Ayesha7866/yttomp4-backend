# YTTOMP4 Backend

The conversion backend for the **YTTOMP4 Converter Engine** WordPress plugin.

The plugin (WordPress) sends conversion jobs here. This backend downloads /
converts with `yt-dlp` + `ffmpeg` and returns a download link.

```
WordPress plugin  →  POST /api/convert  →  this backend  →  yt-dlp + ffmpeg
                 ←  GET /api/status/{job_id} →  { download_url }
```

## What you need before deploying

| Thing | Why |
|---|---|
| A VPS (not shared hosting) | This backend needs Python + ffmpeg + disk space. Shared WordPress hosting cannot run it. A small Hostinger / Hetzner / DigitalOcean VPS (2 GB RAM+) is enough to start. |
| A domain or subdomain for the API | e.g. `api.yttomp4.in.net`. The plugin's **API Base URL** is this address. |
| HTTPS on that domain | Use Caddy (automatic HTTPS) or Nginx + Certbot. Never expose the API over plain `http://` in production — the API key would travel unencrypted. |

## Quick start (Docker — recommended)

```bash
# 1. On your VPS, install Docker (Ubuntu):
curl -fsSL https://get.docker.com | sh

# 2. Upload this folder to the VPS, e.g. /opt/yttomp4-backend

# 3. Create your real .env:
cp .env.example .env
nano .env   # set API_KEY (long random) + PUBLIC_BASE_URL (https://api.yourdomain.com)

# 4. Start it:
docker compose up -d --build

# 5. Check health:
curl http://localhost:8000/api/health
# -> {"status":"ok","version":"1.0.0"}
```

Then point your domain/subdomain at the VPS and put HTTPS in front of it
(see "HTTPS" below), and paste the public URL + key into the plugin.

## Without Docker

```bash
sudo apt update && sudo apt install -y python3 python3-pip ffmpeg
pip install -r requirements.txt   # use a venv on newer Ubuntu: python3 -m venv venv && source venv/bin/activate
cp .env.example .env              # fill in API_KEY + PUBLIC_BASE_URL
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Keep `yt-dlp` updated — YouTube changes break old versions often:

```bash
pip install -U yt-dlp
# or in Docker: rebuild the image
docker compose up -d --build
```

## Connect the WordPress plugin

In WordPress admin → **YTTOMP4 Converter → Settings**:

| Plugin field | Value from this backend |
|---|---|
| API Base URL | Your `PUBLIC_BASE_URL`, e.g. `https://api.yttomp4.in.net` (no trailing `/`) |
| API Key | Your `API_KEY` from `.env` |

Save → **Test Connection** → then convert a video on the homepage.

## API contract

All `/api/convert` and `/api/status/*` calls require:

```
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

### POST /api/convert

```bash
curl -X POST https://api.yourdomain.com/api/convert \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","format":"mp4","quality":"1080p"}'
```

Response:

```json
{ "job_id": "a1b2c3...", "status": "processing" }
```

`format`: `mp4`, `mp3`, `shorts`, `thumbnail`.
`quality`: `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`,
`240p`, `144p`, `320kbps`, `256kbps`, `192kbps`, `128kbps`.

### GET /api/status/{job_id}

```bash
curl https://api.yourdomain.com/api/status/a1b2c3... \
  -H "Authorization: Bearer YOUR_API_KEY"
```

```json
{
  "status": "completed",
  "download_url": "https://api.yourdomain.com/files/a1b2c3....mp4",
  "title": "Video Title",
  "author": "Channel Name",
  "duration": "04:28",
  "thumbnail": "https://i.ytimg.com/.../hqdefault.jpg",
  "downloads": [{ "quality": "1080p", "format": "MP4", "size": "", "url": "https://..." }]
}
```

`status` is `processing`, `completed` or `failed` (`failed` includes an `error` message).

### GET /api/health (public, no key)

```json
{ "status": "ok", "version": "1.0.0" }
```

### GET /files/{filename}

Serves the finished file. Filename must be `{32-hex-job-id}.{mp4|mp3|jpg|webp|png|m4a}`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | *(must set)* | Bearer key the plugin sends. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL, used to build `download_url`. |
| `CORS_ORIGINS` | `*` | Allowed browser origins, comma-separated. |
| `DOWNLOAD_DIR` | `downloads` | Where finished files live. |
| `MAX_DURATION_SECONDS` | `3600` | Reject videos longer than this. |
| `MAX_FILE_AGE_HOURS` | `24` | Finished files + jobs deleted after this. |
| `MAX_CONCURRENT_JOBS` | `3` | Parallel conversions (each uses CPU + bandwidth). |
| `RATE_LIMIT_CONVERT_PER_MIN` | `30` | Per-IP convert starts / minute. |
| `RATE_LIMIT_STATUS_PER_MIN` | `120` | Per-IP status checks / minute. |
| `YTDLP_TIMEOUT_SECONDS` | `600` | Max seconds per conversion. |

## HTTPS (production)

Do not expose port 8000 directly. Put Caddy in front — it gets a free
certificate automatically:

```caddy
# Caddyfile
api.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
docker run -d --name caddy --network host \
  -v $PWD/Caddyfile:/etc/caddy/Caddyfile \
  -v caddy_data:/data caddy
```

Or use Nginx + Certbot with `proxy_pass http://127.0.0.1:8000;`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Plugin: "API key rejected (401)" | `API_KEY` in `.env` ≠ key in plugin settings. Copy it exactly, no spaces. Restart backend after changing `.env`. |
| Plugin: "Could not reach server" | Backend down, wrong Base URL, or firewall blocking the port. `curl` the `/api/health` URL from your own computer. |
| Job fails: "Could not read this video" | Video is private / age-restricted / region-blocked / removed. Try a public video. |
| Job fails after YouTube change | `pip install -U yt-dlp` (or rebuild Docker image). |
| Server slow / out of disk | Lower `MAX_CONCURRENT_JOBS`, lower `MAX_FILE_AGE_HOURS`, add disk/RAM. Conversions are CPU-heavy. |
| `yt-dlp is not installed` | Rebuild the Docker image, or `pip install yt-dlp` on a manual install. |

## Security notes

- Keep `API_KEY` secret. Anyone with it can spend your server's CPU/bandwidth.
- Keep HTTPS on. The key travels in a header on every call.
- Jobs are in-memory in v1: a restart clears pending jobs. Finished files
  survive in `downloads/` (Docker volume). For multi-server / zero-downtime
  setups, move jobs to Redis + a real queue later.
- One video per job (`--no-playlist`): playlist URLs convert only the single video.

## Legal note

Only convert videos you have the rights to download/convert, and respect
YouTube's Terms of Service and applicable copyright law. This backend is a
tool — your content policy decides what gets converted.
