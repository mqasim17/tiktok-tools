from flask import Flask, render_template, request, jsonify, Response, send_file
import io
import json
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
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

@app.errorhandler(413)
def request_too_large(_exc):
    return jsonify(success=False, error="Request body is too large. Large creator downloads use server-side job IDs instead of sending all video objects."), 413


BASE = "https://api.scrapecreators.com"
API_TIMEOUT = 90
DOWNLOAD_TIMEOUT = 120

# Server-side pool for the current process. The browser stores the user's own
# keys in localStorage and restores them automatically after Render restarts.
API_KEYS = []
API_KEY_INDEX = 0
KEY_LOCK = threading.Lock()

# File-backed job state. This avoids losing jobs when a request is handled by
# another Gunicorn worker/process during the same running instance.
JOB_ROOT = os.path.join(tempfile.gettempdir(), "tiktok_tool_jobs")
os.makedirs(JOB_ROOT, exist_ok=True)
JOB_TTL_SECONDS = 60 * 60
JOB_LOCKS = {}
JOB_LOCKS_LOCK = threading.Lock()
SESSION_LOCAL = threading.local()

def http_session():
    session = getattr(SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        SESSION_LOCAL.session = session
    return session


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
    session = http_session()
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(
                BASE + path,
                headers={"x-api-key": api_key},
                params=params,
                timeout=API_TIMEOUT,
            )
            try:
                data = response.json()
            except Exception:
                data = {"success": False, "error": (response.text or "")[:500]}

            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue

            if not response.ok:
                raise RuntimeError(
                    data.get("message")
                    or data.get("error")
                    or data.get("status_msg")
                    or f"API returned HTTP {response.status_code}"
                )

            if isinstance(data, dict) and data.get("success") is False:
                raise RuntimeError(
                    data.get("status_msg")
                    or data.get("message")
                    or data.get("error")
                    or "Scrape Creators request failed"
                )
            return data
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.35 * (2 ** attempt))
    raise RuntimeError(str(last_error) if last_error else "API request failed")

def api_keys_from_request(body):
    raw = body.get("keys") if isinstance(body, dict) else ""
    if raw is not None and str(raw).strip():
        set_api_keys(raw)
    return key_count()

def require_keys_from_request(body):
    if api_keys_from_request(body) == 0:
        raise RuntimeError("No API keys are loaded. Open API Keys and save at least one key.")

def api_get_rotating(path, params):
    with KEY_LOCK:
        keys = list(API_KEYS)
    if not keys:
        raise RuntimeError("No API keys are loaded.")
    last = None
    for key in keys:
        try:
            return api_get(path, key, params)
        except Exception as exc:
            last = exc
            msg = str(exc).lower()
            if not any(t in msg for t in ("401","403","429","unauthorized","forbidden","rate limit","quota","invalid api")):
                break
    raise RuntimeError(str(last) if last else "API request failed")


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

def get_video_info(url, transcript=False, region="", cache_age="30d", trim=None):
    params = {
        "url": url,
        "get_transcript": "true" if transcript else "false",
        "trim": "true" if (trim is True or trim is None and transcript) else "false",
        "cache_max_age": cache_age,
    }
    if region:
        params["region"] = region
    data = api_get_rotating("/v2/tiktok/video", params)
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
    raw = body.get("keys") or ""
    count = set_api_keys(raw)
    if count == 0:
        return jsonify(success=True, count=0)
    return jsonify(success=True, count=count)

def job_dir(job_id):
    path = os.path.join(JOB_ROOT, job_id)
    os.makedirs(path, exist_ok=True)
    return path

def job_lock(job_id):
    with JOB_LOCKS_LOCK:
        return JOB_LOCKS.setdefault(job_id, threading.Lock())

def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)

def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

def creator_job_path(job_id):
    return os.path.join(job_dir(job_id), "creator.json")

def creator_job_new():
    job_id = "creator_" + uuid.uuid4().hex
    job = {
        "status": "queued", "handle": "", "sort_by": "latest", "target": "10",
        "target_count": 10, "videos": [], "loaded": 0, "page": 0,
        "credits_charged": 0, "has_more": True, "message": "Queued",
        "error": None, "created": time.time(), "cancel_requested": False,
    }
    write_json(creator_job_path(job_id), job)
    return job_id

def creator_job_update(job_id, **updates):
    path = creator_job_path(job_id)
    lock = job_lock(job_id)
    with lock:
        job = read_json(path)
        if job is None:
            return
        job.update(updates)
        write_json(path, job)

def creator_job_get(job_id):
    return read_json(creator_job_path(job_id))

def creator_job_cancelled(job_id):
    job = creator_job_get(job_id) or {}
    return bool(job.get("cancel_requested"))

def cleanup_job_files():
    cutoff = time.time() - JOB_TTL_SECONDS
    try:
        for name in os.listdir(JOB_ROOT):
            path = os.path.join(JOB_ROOT, name)
            if not os.path.isdir(path):
                continue
            state = read_json(os.path.join(path, "job.json")) or read_json(os.path.join(path, "creator.json"))
            created = (state or {}).get("created", 0)
            if created and created < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                try:
                    with JOB_LOCKS_LOCK:
                        JOB_LOCKS.pop(name, None)
                except Exception:
                    pass
    except OSError:
        pass

def creator_fetch_worker(job_id):
    """Paginate the Profile Videos endpoint in the background and persist progress."""
    job = creator_job_get(job_id)
    if not job:
        return
    handle = job.get("handle", "")
    sort_by = job.get("sort_by", "latest")
    region = job.get("region", "US")
    target = job.get("target", "10")
    target_count = job.get("target_count")
    cursor = None
    videos = []
    page = 0
    credits = 0
    try:
        creator_job_update(job_id, status="running", message="Fetching page 1…")
        while True:
            if creator_job_cancelled(job_id):
                creator_job_update(job_id, status="cancelled", videos=videos, loaded=len(videos), page=page,
                                   credits_charged=credits, has_more=True, message=f"Stopped after {len(videos)} videos.")
                return
            params = {"handle": handle, "sort_by": sort_by, "trim": "true"}
            if region:
                params["region"] = region
            if cursor is not None:
                params["max_cursor"] = str(cursor)
            page += 1
            creator_job_update(job_id, page=page, message=f"Fetching page {page}…")
            try:
                data = api_get_rotating("/v3/tiktok/profile/videos", params)
            except Exception as exc:
                # Preserve partial results so the user can still download what was found.
                creator_job_update(
                    job_id,
                    status="error",
                    videos=videos,
                    loaded=len(videos),
                    page=page,
                    credits_charged=credits,
                    has_more=True,
                    error=str(exc),
                    message=f"Profile fetch failed on page {page}."
                )
                return
            credits += int(data.get("credits_charged") or 0)
            batch = data.get("aweme_list") or []
            for item in batch:
                parsed = extract_profile_video(item, handle)
                if parsed.get("url"):
                    videos.append(parsed)
                    if target_count is not None and len(videos) >= target_count:
                        break
            has_more = bool(data.get("has_more"))
            creator_job_update(
                job_id,
                videos=videos,
                loaded=len(videos),
                page=page,
                credits_charged=credits,
                has_more=has_more,
                message=f"Fetching page {page} — found {len(videos)} videos."
            )
            if target_count is not None and len(videos) >= target_count:
                videos = videos[:target_count]
                creator_job_update(job_id, status="done", videos=videos, loaded=len(videos), page=page,
                                   credits_charged=credits, has_more=has_more,
                                   message=f"Loaded {len(videos)} videos.")
                return
            if not has_more:
                creator_job_update(job_id, status="done", videos=videos, loaded=len(videos), page=page,
                                   credits_charged=credits, has_more=False,
                                   message=f"Loaded {len(videos)} videos. Profile exhausted.")
                return
            next_cursor = data.get("max_cursor")
            if next_cursor is None or str(next_cursor) == str(cursor):
                creator_job_update(job_id, status="done", videos=videos, loaded=len(videos), page=page,
                                   credits_charged=credits, has_more=has_more,
                                   message=f"Loaded {len(videos)} videos. Pagination cursor stopped.")
                return
            cursor = next_cursor
    except Exception as exc:
        creator_job_update(job_id, status="error", videos=videos, loaded=len(videos), page=page,
                           credits_charged=credits, error=str(exc), message="Creator fetch failed.")

@app.post("/api/creator-jobs")
def creator_job_start():
    body = request.get_json(silent=True) or {}
    handle = (body.get("handle") or "").strip().lstrip("@")
    sort_by = body.get("sort_by") or "latest"
    region = (body.get("region") or "US").strip()
    target = body.get("count") or "10"
    if not handle:
        return jsonify(success=False, error="Enter a TikTok username."), 400
    if target == "all":
        target_count = None
    else:
        try:
            target_count = max(1, min(int(target), 2000))
        except Exception:
            return jsonify(success=False, error="Invalid video count."), 400
    # Fail fast if no key is loaded; otherwise the background job would just error later.
    try:
        require_keys_from_request(body)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 400

    job_id = creator_job_new()
    creator_job_update(job_id, handle=handle, sort_by=sort_by, region=region, target=target, target_count=target_count)
    threading.Thread(target=creator_fetch_worker, args=(job_id,), daemon=True).start()
    return jsonify(success=True, job_id=job_id)

@app.get("/api/creator-jobs/<job_id>")
def creator_job_status(job_id):
    job = creator_job_get(job_id)
    if not job:
        return jsonify(success=False, error="Creator job not found or expired."), 404
    return jsonify(success=True, **job)

@app.post("/api/creator-jobs/<job_id>/cancel")
def creator_job_cancel(job_id):
    job = creator_job_get(job_id)
    if not job:
        return jsonify(success=False, error="Creator job not found or expired."), 404
    creator_job_update(job_id, cancel_requested=True)
    return jsonify(success=True)

@app.post("/api/videos")
def creator_videos_legacy():
    # Compatibility route: start a background job instead of blocking this request.
    body = request.get_json(silent=True) or {}
    with app.test_request_context(json=body):
        return creator_job_start()

@app.post("/api/transcripts")
def bulk_transcripts():
    body = request.get_json(silent=True) or {}
    try:
        require_keys_from_request(body)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 400

    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()
    if not links:
        return jsonify(success=False, error="Paste at least one TikTok URL."), 400

    results = [None] * len(links)
    charged = 0

    def one(index, url):
        try:
            info = get_video_info(url, transcript=True, region=region, cache_age=cache_age)
            credits = int(info.get("credits_charged") or 0)
            return index, {
                "index": index, "url": url, "success": True,
                "title": info["title"], "duration": info["duration"],
                "transcript": info["transcript"], "cached": info["cached"],
                "credits_charged": credits,
            }, credits
        except Exception as exc:
            return index, {"index": index, "url": url, "success": False, "error": str(exc)}, 0

    with ThreadPoolExecutor(max_workers=min(8, len(links))) as pool:
        futures = [pool.submit(one, i, u) for i, u in enumerate(links, 1)]
        for fut in as_completed(futures):
            idx, item, credits = fut.result()
            results[idx - 1] = item
            charged += credits

    return jsonify(success=True, results=results, credits_charged=charged)


@app.post("/api/media-info")
def bulk_media_info():
    body = request.get_json(silent=True) or {}
    try:
        require_keys_from_request(body)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 400

    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()
    if not links:
        return jsonify(success=False, error="Paste at least one TikTok URL."), 400

    results = [None] * len(links)
    charged = 0

    def one(index, url):
        try:
            info = get_video_info(url, transcript=False, region=region, cache_age=cache_age, trim=False)
            credits = int(info.get("credits_charged") or 0)
            return index, {
                "index": index, "url": url, "success": True,
                "title": info["title"], "duration": info["duration"],
                "video_url": info["video_url"], "audio_url": info["audio_url"],
                "cached": info["cached"], "credits_charged": credits,
            }, credits
        except Exception as exc:
            return index, {"index": index, "url": url, "success": False, "error": str(exc)}, 0

    with ThreadPoolExecutor(max_workers=min(8, len(links))) as pool:
        futures = [pool.submit(one, i, u) for i, u in enumerate(links, 1)]
        for fut in as_completed(futures):
            idx, item, credits = fut.result()
            results[idx - 1] = item
            charged += credits

    return jsonify(success=True, results=results, credits_charged=charged)


def clean_filename(text, fallback):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._-")
    return (name[:90] or fallback)

def download_to_file(url, destination):
    session = http_session()
    with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(destination, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


def bulk_job_path(job_id):
    return os.path.join(job_dir(job_id), "job.json")

def new_job():
    job_id = "job_" + uuid.uuid4().hex
    job = {
        "status": "queued", "total": 0, "done": 0, "failed": 0,
        "message": "Queued", "file": None, "created": time.time(), "errors": []
    }
    write_json(bulk_job_path(job_id), job)
    return job_id

def set_job(job_id, **updates):
    path = bulk_job_path(job_id)
    lock = job_lock(job_id)
    with lock:
        job = read_json(path)
        if job is None:
            return
        job.update(updates)
        write_json(path, job)

def get_job(job_id):
    return read_json(bulk_job_path(job_id))

def cleanup_jobs():
    cleanup_job_files()


def bulk_zip_worker(job_id, items, media_type):
    temp_dir = tempfile.mkdtemp(prefix=f"tiktok_zip_{job_id}_")
    files, errors = [], []
    try:
        total = len(items)
        set_job(job_id, status="running", total=total, done=0, failed=0,
                message=f"Downloading 0 / {total}…")

        futures = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, total))) as pool:
            for idx, item in enumerate(items, 1):
                url = (item.get("audio_url") if media_type == "audio" else item.get("video_url") or "").strip()
                if not url:
                    errors.append({"index": idx, "url": item.get("url",""), "error": f"No {media_type} URL returned"})
                    continue
                ext = ".mp3" if media_type == "audio" else ".mp4"
                filename = f"{idx:04d}_{clean_filename(item.get('title'), f'video_{idx}')}{ext}"
                path = os.path.join(temp_dir, filename)
                futures[pool.submit(download_to_file, url, path)] = (idx, filename, path, item.get("url",""))

            done = 0
            for fut in as_completed(futures):
                idx, filename, path, original_url = futures[fut]
                try:
                    fut.result()
                    files.append((idx, filename, path))
                    done += 1
                except Exception as exc:
                    errors.append({"index": idx, "url": original_url, "error": str(exc)})
                set_job(job_id, done=done, failed=len(errors), message=f"Downloaded {done} / {total}…")

        if not files:
            set_job(job_id, status="error", done=0, failed=len(errors), errors=errors,
                    message="No media files could be downloaded.")
            return

        files.sort(key=lambda x: x[0])
        zip_path = os.path.join(tempfile.gettempdir(), f"{job_id}_{media_type}.zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for _, filename, path in files:
                archive.write(path, arcname=filename)
            if errors:
                archive.writestr("download_errors.txt",
                                 "\n".join(f"Video {e['index']}: {e['url']} — {e['error']}" for e in errors))

        set_job(job_id, status="done", done=len(files), failed=len(errors), errors=errors,
                file=zip_path, message=f"ZIP ready: {len(files)} downloaded, {len(errors)} failed.")
    except Exception as exc:
        set_job(job_id, status="error", done=len(files), failed=len(errors), errors=errors,
                message=f"ZIP worker failed: {exc}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/api/bulk-download")
def bulk_download():
    cleanup_jobs()
    body = request.get_json(silent=True) or {}
    media_type = body.get("media_type") or "video"
    if media_type not in {"video", "audio"}:
        return jsonify(success=False, error="Invalid media type."), 400

    # Direct-media path: the browser sends only the minimal fields needed for download.
    items = []
    for x in (body.get("items") or []):
        if x.get("success") is False:
            continue
        items.append({
            "url": x.get("url", ""),
            "title": x.get("title", ""),
            "video_url": x.get("video_url", ""),
            "audio_url": x.get("audio_url", ""),
            "success": True,
        })
    if not items:
        return jsonify(success=False, error="No downloadable videos were supplied."), 400

    job_id = new_job()
    thread = threading.Thread(
        target=bulk_zip_worker,
        args=(job_id, items, media_type),
        daemon=True,
    )
    thread.start()
    return jsonify(success=True, job_id=job_id, total=len(items), source="items")

@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    cleanup_jobs()
    job = get_job(job_id)
    if not job:
        return jsonify(success=False, error="Job not found or expired.", status="missing"), 404
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


@app.post("/api/export-links")
def export_links():
    body = request.get_json(silent=True) or {}
    links = []
    seen = set()
    for item in (body.get("videos") or []):
        url = (item.get("url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            links.append(url)
    return Response("\n".join(links) + ("\n" if links else ""), mimetype="text/plain")

@app.get("/api/direct-download")
def direct_download():
    url = (request.args.get("url") or "").strip()
    media_type = (request.args.get("type") or "video").strip().lower()
    filename = clean_filename(request.args.get("filename") or "tiktok", "tiktok")
    if media_type not in {"video", "audio"}:
        return jsonify(success=False, error="Invalid media type."), 400
    try:
        if not is_trusted_tiktok_media_url(url):
            return jsonify(success=False, error="Unsupported TikTok media host returned by the API."), 400

        response = http_session().get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type") or ("audio/mpeg" if media_type == "audio" else "video/mp4")
        if media_type == "audio" and not filename.lower().endswith(".mp3"):
            filename += ".mp3"
        if media_type == "video" and not filename.lower().endswith(".mp4"):
            filename += ".mp4"

        def generate():
            try:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        yield chunk
            finally:
                response.close()

        return Response(
            generate(),
            mimetype=content_type.split(";")[0],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        try:
            response.close()
        except Exception:
            pass
        return jsonify(success=False, error=f"Direct download failed: {exc}"), 502


def is_trusted_tiktok_media_url(url):
    """Allow known TikTok media/CDN host families returned by Scrape Creators."""
    try:
        parsed = urlparse((url or "").strip())
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return False

    allowed_domains = (
        # TikTok CDN families
        "tiktokcdn.com",
        "tiktokcdn-us.com",
        "tiktokcdn-eu.com",
        "tiktokcdn-in.com",
        # TikTok video/API delivery domains
        "tiktokv.com",
        "tiktokv.eu",
        "tiktokv.us",
        # Music/media delivery families that can carry audio URLs
        "muscdn.com",
        "ttcdn-us.com",
        "ttlivecdn.com",
    )
    return any(host == d or host.endswith("." + d) for d in allowed_domains)

@app.post("/api/proxy-download")
def proxy_download():
    body = request.get_json(silent=True) or {}
    url = (body.get("media_url") or "").strip()
    media_type = body.get("media_type") or "video"
    filename = clean_filename(body.get("filename"), "tiktok")
    if not url or not is_trusted_tiktok_media_url(url):
        return jsonify(success=False, error="Unsupported TikTok media host returned by the API."), 400
    try:
        response = http_session().get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type") or ("audio/mpeg" if media_type == "audio" else "video/mp4")
        def generate():
            try:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        yield chunk
            finally:
                response.close()
        return Response(generate(), mimetype=content_type.split(";")[0],
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except Exception as exc:
        return jsonify(success=False, error=f"Media download failed: {exc}"), 502

@app.errorhandler(404)
def api_or_page_404(error):
    if request.path.startswith("/api/"):
        return jsonify(success=False, error="API route not found.", path=request.path), 404
    return error

@app.errorhandler(500)
def api_or_page_500(error):
    if request.path.startswith("/api/"):
        return jsonify(success=False, error="Internal server error."), 500
    return error

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
