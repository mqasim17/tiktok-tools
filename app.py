from flask import Flask, render_template, request, jsonify, Response, send_file
import io
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

app = Flask(__name__)

BASE = "https://api.scrapecreators.com"
API_TIMEOUT = 90
DOWNLOAD_TIMEOUT = 120

# Server-side pool for the current process. The browser stores the user's own
# keys in localStorage and restores them automatically after Render restarts.
API_KEYS = []
API_KEY_INDEX = 0
KEY_LOCK = threading.Lock()

# In-memory bulk download jobs for the current Render instance.
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 60 * 60

def normalize_keys(raw):
    seen = set()
    out = []
    for line in re.split(r"[\r\n,]+", raw or ""):
        key = line.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out

def set_api_keys(raw):
    global API_KEYS, API_KEY_INDEX
    keys = normalize_keys(raw)
    with KEY_LOCK:
        API_KEYS = keys
        API_KEY_INDEX = 0
    return len(keys)

def next_api_key():
    global API_KEY_INDEX
    with KEY_LOCK:
        if not API_KEYS:
            return None
        key = API_KEYS[API_KEY_INDEX % len(API_KEYS)]
        API_KEY_INDEX += 1
        return key

def key_count():
    with KEY_LOCK:
        return len(API_KEYS)

def require_key():
    key = next_api_key()
    if not key:
        raise RuntimeError("No API keys are loaded. Open API Keys and save at least one key.")
    return key

def api_get(path, api_key, params):
    response = requests.get(
        BASE + path,
        headers={"x-api-key": api_key},
        params=params,
        timeout=API_TIMEOUT,
    )
    try:
        data = response.json()
    except Exception:
        data = {"success": False, "error": response.text or f"HTTP {response.status_code}"}

    if not response.ok:
        message = (
            data.get("message")
            or data.get("error")
            or data.get("status_msg")
            or f"API returned HTTP {response.status_code}"
        )
        raise RuntimeError(message)

    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            data.get("status_msg") or data.get("message") or "Scrape Creators request failed"
        )
    return data

def first_url(obj):
    if isinstance(obj, dict):
        urls = obj.get("url_list")
        if isinstance(urls, list):
            for url in urls:
                if url:
                    return url
    return None

def parse_links(text):
    candidates = re.split(r"[\s,]+", text or "")
    seen = set()
    out = []
    for raw in candidates:
        url = raw.strip()
        if not url or url in seen:
            continue
        p = urlparse(url)
        if p.scheme not in {"http", "https"}:
            continue
        if p.netloc.lower() not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            continue
        seen.add(url)
        out.append(url)
    return out

def fmt_seconds(value):
    try:
        total = max(0, int(round(float(value))))
    except Exception:
        return ""
    return f"{total // 60}:{total % 60:02d}"

def fmt_ms(value):
    try:
        return fmt_seconds(float(value) / 1000.0)
    except Exception:
        return ""

def clean_vtt(text):
    if not text:
        return ""
    text = text.replace("\\n", "\n").replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n")]
    output = []
    timestamp = None
    for line in lines:
        if not line or line.upper() == "WEBVTT" or line.startswith("NOTE"):
            continue
        m = re.search(
            r"(\d{2}):(\d{2}):(\d{2})[.,]\d{1,3}\s*-->\s*"
            r"(\d{2}):(\d{2}):(\d{2})[.,]\d{1,3}",
            line,
        )
        if m:
            h, minute, sec = map(int, m.group(1, 2, 3))
            timestamp = f"[{h * 60 + minute:02d}:{sec:02d}]"
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        if timestamp:
            output.append(f"{timestamp} {line}")
            timestamp = None
        elif output:
            output[-1] += " " + line
    return "\n".join(output).strip()

def choose_highest_video_url(video):
    # Prefer the highest bitrate rendition exposed in bit_rate, then fall back
    # to play_addr. The Profile Videos API documents play_addr as the video
    # without watermark.
    candidates = []
    for item in video.get("bit_rate") or []:
        addr = item.get("play_addr") or {}
        url = first_url(addr)
        if not url:
            continue
        candidates.append({
            "url": url,
            "height": int(addr.get("height") or 0),
            "width": int(addr.get("width") or 0),
            "bitrate": int(item.get("bit_rate") or 0),
        })
    if candidates:
        candidates.sort(key=lambda x: (x["height"], x["width"], x["bitrate"]), reverse=True)
        return candidates[0]["url"], candidates[0]["height"], candidates[0]["bitrate"]

    addr = video.get("play_addr") or {}
    url = first_url(addr)
    return url, int(addr.get("height") or 0), 0

def extract_profile_video(item, fallback_handle):
    video = item.get("video") or {}
    author = item.get("author") or {}
    unique_id = author.get("unique_id") or author.get("uniqueId") or fallback_handle
    aweme_id = str(item.get("aweme_id") or item.get("id") or "")
    url = item.get("share_url") or item.get("url") or (
        f"https://www.tiktok.com/@{unique_id}/video/{aweme_id}" if aweme_id else ""
    )
    media_url, quality_height, bitrate = choose_highest_video_url(video)
    audio_url = first_url((item.get("added_sound_music_info") or {}).get("play_url"))
    cover = first_url(video.get("dynamic_cover")) or first_url(video.get("cover"))
    stats = item.get("statistics") or {}
    return {
        "id": aweme_id,
        "url": url,
        "title": (item.get("desc") or "Untitled TikTok").strip(),
        "duration": fmt_seconds(video.get("duration") or item.get("duration") or 0),
        "cover": cover,
        "video_url": media_url,
        "audio_url": audio_url,
        "quality_height": quality_height,
        "bitrate": bitrate,
        "plays": stats.get("play_count"),
        "likes": stats.get("digg_count"),
    }

def extract_video_info(data):
    aweme = data.get("aweme_detail") or {}
    video = aweme.get("video") or {}
    music = aweme.get("added_sound_music_info") or {}
    video_url = first_url(video.get("download_no_watermark_addr"))
    if not video_url and not bool(video.get("has_watermark")):
        video_url = first_url(video.get("play_addr"))
    if not video_url:
        video_url = first_url(video.get("play_addr"))
    return {
        "id": str(aweme.get("aweme_id") or data.get("id") or ""),
        "title": (aweme.get("desc") or "Untitled TikTok").strip(),
        "duration": fmt_ms(video.get("duration")),
        "video_url": video_url,
        "audio_url": first_url(music.get("play_url")),
        "transcript": clean_vtt(data.get("transcript") or ""),
        "cached": bool(data.get("cached")),
        "credits_charged": data.get("credits_charged"),
    }

def get_video_info(url, transcript=False, region="", cache_age="30d"):
    key = require_key()
    params = {
        "url": url,
        "get_transcript": "true" if transcript else "false",
        "trim": "true",
        "cache_max_age": cache_age,
    }
    if region:
        params["region"] = region
    data = api_get("/v2/tiktok/video", key, params)
    return extract_video_info(data)

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/healthz")
def healthz():
    return jsonify(status="ok")

@app.get("/api/keys/status")
def api_keys_status():
    return jsonify(success=True, count=key_count())

@app.post("/api/keys")
def api_keys_save():
    body = request.get_json(silent=True) or {}
    count = set_api_keys(body.get("keys") or "")
    if count == 0:
        return jsonify(success=False, error="Add at least one API key."), 400
    return jsonify(success=True, count=count)

@app.post("/api/videos")
def creator_videos():
    body = request.get_json(silent=True) or {}
    handle = (body.get("handle") or "").strip().lstrip("@")
    sort_by = body.get("sort_by") or "latest"
    region = (body.get("region") or "US").strip()
    target = body.get("count") or "10"
    if not handle:
        return jsonify(success=False, error="Enter a TikTok username."), 400

    if target == "all":
        target_count = None
        max_pages = 500
    else:
        try:
            target_count = max(1, min(int(target), 2000))
            max_pages = 500
        except Exception:
            return jsonify(success=False, error="Invalid video count."), 400

    items, cursor, pages, charged = [], None, 0, 0
    try:
        while pages < max_pages and (target_count is None or len(items) < target_count):
            params = {"handle": handle, "sort_by": sort_by, "trim": "true"}
            if region:
                params["region"] = region
            if cursor is not None:
                params["max_cursor"] = str(cursor)

            data = api_get("/v3/tiktok/profile/videos", require_key(), params)
            charged += int(data.get("credits_charged") or 0)
            batch = data.get("aweme_list") or []
            for item in batch:
                parsed = extract_profile_video(item, handle)
                if parsed["url"]:
                    items.append(parsed)
                    if target_count is not None and len(items) >= target_count:
                        break

            pages += 1
            if not data.get("has_more") or target_count is not None and len(items) >= target_count:
                break
            next_cursor = data.get("max_cursor")
            if next_cursor is None or str(next_cursor) == str(cursor):
                break
            cursor = next_cursor

        return jsonify(
            success=True,
            videos=items,
            loaded=len(items),
            pages=pages,
            credits_charged=charged,
            complete=(target_count is None and not data.get("has_more")) or (
                target_count is not None and len(items) >= target_count
            ),
        )
    except Exception as e:
        return jsonify(success=False, error=str(e), loaded=len(items), pages=pages), 502

@app.post("/api/transcripts")
def bulk_transcripts():
    body = request.get_json(silent=True) or {}
    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()
    if not links:
        return jsonify(success=False, error="Paste at least one TikTok URL."), 400

    results, charged = [], 0
    for idx, url in enumerate(links, 1):
        try:
            info = get_video_info(url, transcript=True, region=region, cache_age=cache_age)
            credits = int(info.get("credits_charged") or 0)
            charged += credits
            results.append({
                "index": idx,
                "url": url,
                "success": True,
                "title": info["title"],
                "duration": info["duration"],
                "transcript": info["transcript"],
                "cached": info["cached"],
                "credits_charged": credits,
            })
        except Exception as e:
            results.append({"index": idx, "url": url, "success": False, "error": str(e)})
    return jsonify(success=True, results=results, credits_charged=charged)

@app.post("/api/media-info")
def bulk_media_info():
    body = request.get_json(silent=True) or {}
    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()
    if not links:
        return jsonify(success=False, error="Paste at least one TikTok URL."), 400

    results, charged = [], 0
    for idx, url in enumerate(links, 1):
        try:
            info = get_video_info(url, transcript=False, region=region, cache_age=cache_age)
            credits = int(info.get("credits_charged") or 0)
            charged += credits
            results.append({
                "index": idx,
                "url": url,
                "success": True,
                "title": info["title"],
                "duration": info["duration"],
                "video_url": info["video_url"],
                "audio_url": info["audio_url"],
                "cached": info["cached"],
                "credits_charged": credits,
            })
        except Exception as e:
            results.append({"index": idx, "url": url, "success": False, "error": str(e)})
    return jsonify(success=True, results=results, credits_charged=charged)

def clean_filename(text, fallback):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._-")
    return (name[:90] or fallback)

def download_to_file(url, destination):
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(destination, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

def new_job():
    job_id = uuid.uuid4().hex
    job = {
        "status": "queued",
        "total": 0,
        "done": 0,
        "failed": 0,
        "message": "Queued",
        "file": None,
        "created": time.time(),
        "errors": [],
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    return job_id

def set_job(job_id, **updates):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)

def get_job(job_id):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id) or {})

def cleanup_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        old_ids = [jid for jid, job in JOBS.items() if job.get("created", 0) < cutoff]
        for jid in old_ids:
            path = JOBS[jid].get("file")
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            JOBS.pop(jid, None)

def bulk_zip_worker(job_id, items, media_type):
    temp_dir = tempfile.mkdtemp(prefix=f"tiktok_{job_id}_")
    files = []
    try:
        total = len(items)
        set_job(job_id, status="running", total=total, done=0, failed=0, message="Downloading…")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for idx, item in enumerate(items, 1):
                url = item.get("audio_url") if media_type == "audio" else item.get("video_url")
                if not url:
                    set_job(job_id, failed=get_job(job_id).get("failed", 0) + 1)
                    with JOBS_LOCK:
                        JOBS[job_id]["errors"].append({
                            "index": idx,
                            "url": item.get("url", ""),
                            "error": f"No {media_type} URL returned",
                        })
                    continue
                ext = ".mp3" if media_type == "audio" else ".mp4"
                name = clean_filename(item.get("title"), f"video_{idx}")
                path = os.path.join(temp_dir, f"{idx:04d}_{name}{ext}")
                futures[pool.submit(download_to_file, url, path)] = (idx, item.get("url", ""), path)

            done = 0
            failed = get_job(job_id).get("failed", 0)
            for future in as_completed(futures):
                idx, original_url, path = futures[future]
                try:
                    future.result()
                    files.append((idx, path))
                    done += 1
                except Exception as exc:
                    failed += 1
                    with JOBS_LOCK:
                        JOBS[job_id]["errors"].append({
                            "index": idx,
                            "url": original_url,
                            "error": str(exc),
                        })
                set_job(job_id, done=done, failed=failed, message=f"Downloaded {done} / {total}")

        files.sort(key=lambda x: x[0])
        if not files:
            raise RuntimeError("No media files could be downloaded.")

        zip_path = os.path.join(tempfile.gettempdir(), f"tiktok_{job_id}_{media_type}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for idx, path in files:
                archive.write(path, arcname=os.path.basename(path))
            errors = get_job(job_id).get("errors") or []
            if errors:
                archive.writestr(
                    "download_errors.txt",
                    "\n".join(
                        f"Video {e['index']}: {e['url']} — {e['error']}"
                        for e in errors
                    ),
                )

        set_job(
            job_id,
            status="done",
            file=zip_path,
            message=f"Ready. {len(files)} downloaded, {len(get_job(job_id).get('errors') or [])} failed.",
        )
    except Exception as exc:
        set_job(job_id, status="error", message=str(exc))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.post("/api/bulk-download")
def bulk_download():
    cleanup_jobs()
    body = request.get_json(silent=True) or {}
    items = [
        x for x in (body.get("items") or [])
        if x.get("success") is not False
    ]
    media_type = body.get("media_type") or "video"
    if media_type not in {"video", "audio"}:
        return jsonify(success=False, error="Invalid media type."), 400
    if not items:
        return jsonify(success=False, error="No downloadable videos were supplied."), 400

    job_id = new_job()
    thread = threading.Thread(
        target=bulk_zip_worker,
        args=(job_id, items, media_type),
        daemon=True,
    )
    thread.start()
    return jsonify(success=True, job_id=job_id, total=len(items))

@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    cleanup_jobs()
    job = get_job(job_id)
    if not job:
        return jsonify(success=False, error="Job not found or expired."), 404
    public = {k: v for k, v in job.items() if k != "file"}
    return jsonify(success=True, **public)

@app.get("/api/jobs/<job_id>/download")
def job_download(job_id):
    cleanup_jobs()
    job = get_job(job_id)
    if not job:
        return jsonify(success=False, error="Job not found or expired."), 404
    if job.get("status") != "done" or not job.get("file"):
        return jsonify(success=False, error="Job is not ready yet."), 409
    return send_file(
        job["file"],
        as_attachment=True,
        download_name=os.path.basename(job["file"]),
        mimetype="application/zip",
    )

@app.post("/api/proxy-download")
def proxy_download():
    body = request.get_json(silent=True) or {}
    url = (body.get("media_url") or "").strip()
    media_type = body.get("media_type") or "video"
    filename = clean_filename(body.get("filename"), "tiktok")
    if not url.startswith(("https://", "http://")):
        return jsonify(success=False, error="Invalid media URL."), 400
    try:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
            response.raise_for_status()
            content = response.content
        mimetype = "audio/mpeg" if media_type == "audio" else "video/mp4"
        return Response(
            content,
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return jsonify(success=False, error=f"Media download failed: {exc}"), 502

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
