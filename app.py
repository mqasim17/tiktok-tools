from flask import Flask, render_template, request, jsonify, Response, send_file
import io
import re
import zipfile
import requests
from urllib.parse import urlparse

app = Flask(__name__)

BASE = "https://api.scrapecreators.com"
TIMEOUT = 90


API_KEYS = []
API_KEY_INDEX = 0

def normalize_keys(raw):
    seen = set()
    out = []
    for line in re.split(r"[\r\n,]+", raw or ""):
        key = line.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out

def set_api_keys(keys):
    global API_KEYS, API_KEY_INDEX
    API_KEYS = normalize_keys("\n".join(keys))
    API_KEY_INDEX = 0

def next_api_key():
    global API_KEY_INDEX
    if not API_KEYS:
        return None
    key = API_KEYS[API_KEY_INDEX % len(API_KEYS)]
    API_KEY_INDEX += 1
    return key

def current_api_key_count():
    return len(API_KEYS)

def api_get(path, api_key, params):
    r = requests.get(
        BASE + path,
        headers={"x-api-key": api_key},
        params=params,
        timeout=TIMEOUT,
    )
    try:
        data = r.json()
    except Exception:
        data = {"success": False, "error": r.text or f"HTTP {r.status_code}"}
    if not r.ok:
        raise RuntimeError(
            data.get("message")
            or data.get("error")
            or data.get("status_msg")
            or f"API returned HTTP {r.status_code}"
        )
    return data

def clean_url(url):
    return (url or "").strip()

def is_tiktok_url(url):
    return bool(re.match(r"^https?://(?:www\.)?tiktok\.com/", url, re.I))

def format_duration_ms(ms):
    if ms is None:
        return ""
    try:
        seconds = float(ms) / 1000.0
    except Exception:
        return ""
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"

def nested(data, *keys):
    cur = data
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur

def first_url(obj):
    if isinstance(obj, dict):
        urls = obj.get("url_list")
        if isinstance(urls, list) and urls:
            return urls[0]
    return None

def extract_video_info(data):
    aweme = data.get("aweme_detail") or {}
    video = aweme.get("video") or {}
    music = aweme.get("added_sound_music_info") or {}

    # Prefer the no-watermark address when available.
    video_url = first_url(video.get("download_no_watermark_addr"))
    has_watermark = bool(video.get("has_watermark"))
    if not video_url and not has_watermark:
        video_url = first_url(video.get("play_addr"))
    if not video_url:
        video_url = first_url(video.get("play_addr"))

    audio_url = first_url(music.get("play_url"))

    duration_ms = video.get("duration")
    title = aweme.get("desc") or nested(aweme, "share_info", "share_desc") or "Untitled TikTok"

    return {
        "id": str(aweme.get("aweme_id") or data.get("id") or ""),
        "url": data.get("url") or "",
        "title": title.strip(),
        "duration_ms": duration_ms,
        "duration": format_duration_ms(duration_ms),
        "video_url": video_url,
        "audio_url": audio_url,
        "transcript": data.get("transcript") or "",
        "credits_charged": data.get("credits_charged"),
        "cached": bool(data.get("cached")),
        "cached_at": data.get("cached_at"),
    }

def get_direct_info(api_key, url, get_transcript=False, cache_age="30d", region=""):
    params = {
        "url": url,
        "get_transcript": "true" if get_transcript else "false",
        "trim": "true",
        "cache_max_age": cache_age,
    }
    if region:
        params["region"] = region
    data = api_get("/v2/tiktok/video", api_key, params)
    if not data.get("success", True):
        raise RuntimeError(data.get("status_msg") or data.get("message") or "TikTok API failed")
    return extract_video_info(data)

def parse_links(text):
    # One TikTok URL per line; also accepts comma/space-separated URLs.
    candidates = re.split(r"[\s,]+", text or "")
    seen = set()
    out = []
    for raw in candidates:
        u = clean_url(raw)
        if not u:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


@app.post("/api/keys")
def api_keys():
    body = request.get_json(force=True)
    keys = normalize_keys(body.get("keys") or "")
    if not keys:
        return jsonify(success=False, error="Add at least one API key."), 400
    set_api_keys(keys)
    return jsonify(success=True, count=current_api_key_count())

@app.get("/api/keys/status")
def api_keys_status():
    return jsonify(success=True, count=current_api_key_count())

@app.post("/api/videos")
def videos():
    body = request.get_json(force=True)
    key = (body.get("api_key") or "").strip()
    handle = (body.get("handle") or "").strip().lstrip("@")
    sort_by = body.get("sort_by") or "latest"
    region = (body.get("region") or "").strip()
    max_pages = min(max(int(body.get("max_pages") or 10), 1), 100)

    if not key or not handle:
        return jsonify(success=False, error="API key and username are required."), 400

    items, cursor, pages = [], None, 0
    try:
        while pages < max_pages:
            params = {"handle": handle, "sort_by": sort_by}
            if region:
                params["region"] = region
            if cursor is not None:
                params["max_cursor"] = str(cursor)

            request_key = key or next_api_key()
            if not request_key:
                raise RuntimeError("No API keys are configured.")
            data = api_get("/v3/tiktok/profile/videos", request_key, params)
            batch = data.get("aweme_list") or data.get("videos") or []
            for v in batch:
                video = v.get("video") or {}
                cover = first_url(video.get("cover")) or first_url(v.get("cover"))
                url = v.get("url") or v.get("share_url") or ""
                aweme_id = str(v.get("aweme_id") or v.get("id") or "")
                if not url and aweme_id:
                    author = v.get("author") or {}
                    uid = author.get("unique_id") or author.get("uniqueId") or handle
                    url = f"https://www.tiktok.com/@{uid}/video/{aweme_id}"
                items.append({
                    "id": aweme_id,
                    "url": url,
                    "desc": v.get("desc") or "",
                    "cover": cover,
                    "plays": (v.get("statistics") or {}).get("play_count"),
                    "likes": (v.get("statistics") or {}).get("digg_count"),
                    "duration": (video.get("duration") or v.get("duration") or 0),
                })

            pages += 1
            if not data.get("has_more"):
                break
            next_cursor = data.get("max_cursor")
            if next_cursor is None or str(next_cursor) == str(cursor):
                break
            cursor = next_cursor

        return jsonify(success=True, videos=items, pages=pages)
    except Exception as e:
        return jsonify(success=False, error=str(e)), 502

@app.post("/api/transcripts")
def transcripts():
    body = request.get_json(force=True)
    key = (body.get("api_key") or "").strip()
    links = parse_links(body.get("links") or "")
    language = (body.get("language") or "").strip()
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()

    if not key:
        return jsonify(success=False, error="API key is required."), 400
    if not links:
        return jsonify(success=False, error="Paste at least one TikTok video URL."), 400

    results = []
    for idx, url in enumerate(links, 1):
        try:
            request_key = key or next_api_key()
            if not request_key:
                raise RuntimeError("No API keys are configured.")
            info = get_direct_info(request_key, url, get_transcript=True, cache_age=cache_age, region=region)
            transcript = info["transcript"]
            # Language is supported by the dedicated transcript endpoint, but v2's
            # get_transcript response does not document a language parameter.
            # We therefore preserve the API transcript exactly as returned.
            results.append({
                "index": idx,
                "url": url,
                "success": True,
                "title": info["title"],
                "duration": info["duration"],
                "transcript": transcript,
                "cached": info["cached"],
                "credits_charged": info["credits_charged"],
            })
        except Exception as e:
            results.append({"index": idx, "url": url, "success": False, "error": str(e)})
    return jsonify(success=True, results=results)

@app.post("/api/media-info")
def media_info():
    body = request.get_json(force=True)
    key = (body.get("api_key") or "").strip()
    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()

    if not key:
        return jsonify(success=False, error="API key is required."), 400
    if not links:
        return jsonify(success=False, error="Paste at least one TikTok video URL."), 400

    results = []
    for idx, url in enumerate(links, 1):
        try:
            request_key = key or next_api_key()
            if not request_key:
                raise RuntimeError("No API keys are configured.")
            info = get_direct_info(request_key, url, get_transcript=False, cache_age=cache_age, region=region)
            results.append({
                "index": idx,
                "url": url,
                "success": True,
                "title": info["title"],
                "duration": info["duration"],
                "video_url": info["video_url"],
                "audio_url": info["audio_url"],
                "cached": info["cached"],
                "credits_charged": info["credits_charged"],
            })
        except Exception as e:
            results.append({"index": idx, "url": url, "success": False, "error": str(e)})
    return jsonify(success=True, results=results)

@app.post("/api/proxy-download")
def proxy_download():
    """Download a media URL already returned by /api/media-info.
    This does NOT call Scrape Creators again, saving another API credit.
    """
    body = request.get_json(force=True)
    media_url = (body.get("media_url") or "").strip()
    filename = (body.get("filename") or "tiktok_media").strip()
    media_type = body.get("media_type") or "audio"
    if not media_url.startswith(("https://", "http://")):
        return jsonify(success=False, error="Invalid media URL."), 400
    try:
        r = requests.get(media_url, timeout=TIMEOUT)
        r.raise_for_status()
        return Response(
            r.content,
            mimetype="audio/mpeg" if media_type == "audio" else "video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return jsonify(success=False, error=f"Media download failed: {e}"), 502

@app.post("/api/download")
def download():
    """Build a ZIP from media URLs already obtained by /api/media-info.
    If media_items are supplied, no Scrape Creators API request is made.
    """
    body = request.get_json(force=True)
    media_type = body.get("media_type") or "audio"
    want_zip = bool(body.get("zip"))
    supplied = body.get("media_items") or []
    key = (body.get("api_key") or "").strip()
    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()

    if media_type not in {"audio", "video"}:
        return jsonify(success=False, error="media_type must be audio or video."), 400
    if not supplied and not links:
        return jsonify(success=False, error="No media items supplied."), 400

    files, errors = [], []

    if supplied:
        for idx, item in enumerate(supplied, 1):
            try:
                media_url = item.get("audio_url") if media_type == "audio" else item.get("video_url")
                if not media_url:
                    raise RuntimeError(f"No {media_type} URL was returned.")
                ext = ".mp3" if media_type == "audio" else ".mp4"
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", item.get("title") or "").strip("._-")[:80] or f"video_{idx}"
                filename = f"{idx:02d}_{safe}{ext}"
                media = requests.get(media_url, timeout=TIMEOUT)
                media.raise_for_status()
                files.append((filename, media.content))
            except Exception as e:
                errors.append({"index": idx, "url": item.get("url", ""), "error": str(e)})
    else:
        # Fallback for programmatic callers: obtain media URLs once.
        if not key:
            return jsonify(success=False, error="API key is required."), 400
        for idx, url in enumerate(links, 1):
            try:
                request_key = key or next_api_key()
                if not request_key:
                    raise RuntimeError("No API keys are configured.")
                info = get_direct_info(request_key, url, get_transcript=False, cache_age=cache_age, region=region)
                media_url = info["audio_url"] if media_type == "audio" else info["video_url"]
                if not media_url:
                    raise RuntimeError(f"No {media_type} URL was returned.")
                ext = ".mp3" if media_type == "audio" else ".mp4"
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", info["title"]).strip("._-")[:80] or f"video_{idx}"
                media = requests.get(media_url, timeout=TIMEOUT)
                media.raise_for_status()
                files.append((f"{idx:02d}_{safe}{ext}", media.content))
            except Exception as e:
                errors.append({"index": idx, "url": url, "error": str(e)})

    if not files:
        return jsonify(success=False, error="No files could be downloaded.", errors=errors), 502
    if not want_zip and len(files) == 1:
        filename, content = files[0]
        return Response(content, mimetype="audio/mpeg" if media_type == "audio" else "video/mp4",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, content in files:
            z.writestr(filename, content)
        if errors:
            z.writestr("download_errors.txt", "\n".join(f"{e['index']}: {e['url']} — {e['error']}" for e in errors))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"tiktok_{media_type}s.zip", mimetype="application/zip")

@app.get("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
