from flask import Flask, render_template, request, jsonify, Response, send_file
import io
import re
import zipfile
import requests
from urllib.parse import urlparse

app = Flask(__name__)

BASE = "https://api.scrapecreators.com"
TIMEOUT = 90

# In-memory pool for the current Render instance.
# Keys are used round-robin, one API request at a time.
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


def set_api_keys(raw):
    global API_KEYS, API_KEY_INDEX
    API_KEYS = normalize_keys(raw)
    API_KEY_INDEX = 0


def next_api_key():
    global API_KEY_INDEX
    if not API_KEYS:
        return None
    key = API_KEYS[API_KEY_INDEX % len(API_KEYS)]
    API_KEY_INDEX += 1
    return key


def api_key_count():
    return len(API_KEYS)


def api_get(path, api_key, params):
    response = requests.get(
        BASE + path,
        headers={"x-api-key": api_key},
        params=params,
        timeout=TIMEOUT,
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
        raise RuntimeError(data.get("status_msg") or data.get("message") or "Scrape Creators request failed")

    return data


def require_key():
    key = next_api_key()
    if not key:
        raise RuntimeError("No API keys are loaded. Open API Keys and save at least one key.")
    return key


def parse_links(text):
    candidates = re.split(r"[\s,]+", text or "")
    seen = set()
    links = []
    for raw in candidates:
        url = raw.strip()
        if not url or url in seen:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            continue
        seen.add(url)
        links.append(url)
    return links


def format_seconds(value):
    try:
        seconds = float(value)
    except Exception:
        return ""
    whole = max(0, int(round(seconds)))
    return f"{whole // 60}:{whole % 60:02d}"


def format_ms(value):
    try:
        return format_seconds(float(value) / 1000.0)
    except Exception:
        return ""


def first_url(obj):
    if isinstance(obj, dict):
        urls = obj.get("url_list")
        if isinstance(urls, list) and urls:
            return urls[0]
    return None


def clean_vtt(text):
    """Turn WebVTT into readable [MM:SS] transcript lines."""
    if not text:
        return ""
    text = text.replace("\\n", "\n").replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n")]
    output = []
    timestamp = None
    for line in lines:
        if not line or line.upper() == "WEBVTT" or line.startswith("NOTE"):
            continue
        match = re.search(r"(\d{2}):(\d{2}):(\d{2})[.,]\d{1,3}\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,]\d{1,3}", line)
        if match:
            h, m, s = map(int, match.group(1, 2, 3))
            timestamp = f"[{h * 60 + m:02d}:{s:02d}]"
            continue
        # Ignore cue identifiers and settings-only lines.
        if "-->" in line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if timestamp:
            output.append(f"{timestamp} {line}")
            timestamp = None
        elif output:
            # Some VTT responses wrap a cue over multiple lines.
            output[-1] += " " + line
    return "\n".join(output).strip()


def extract_info(data):
    aweme = data.get("aweme_detail") or {}
    video = aweme.get("video") or {}
    music = aweme.get("added_sound_music_info") or {}

    video_url = first_url(video.get("download_no_watermark_addr"))
    if not video_url and not bool(video.get("has_watermark")):
        video_url = first_url(video.get("play_addr"))
    if not video_url:
        video_url = first_url(video.get("play_addr"))

    audio_url = first_url(music.get("play_url"))
    duration = format_ms(video.get("duration"))
    title = (aweme.get("desc") or "Untitled TikTok").strip()
    transcript_raw = data.get("transcript") or ""

    return {
        "id": str(aweme.get("aweme_id") or data.get("id") or ""),
        "title": title,
        "duration": duration,
        "video_url": video_url,
        "audio_url": audio_url,
        "transcript": clean_vtt(transcript_raw),
        "cached": bool(data.get("cached")),
        "credits_charged": data.get("credits_charged"),
    }


def get_video_info(url, *, transcript=False, region="", cache_age="30d"):
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
    return extract_info(data)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/keys/status")
def key_status():
    return jsonify(success=True, count=api_key_count())


@app.post("/api/keys")
def save_keys():
    body = request.get_json(silent=True) or {}
    raw = body.get("keys") or ""
    keys = normalize_keys(raw)
    if not keys:
        return jsonify(success=False, error="Add at least one API key."), 400
    set_api_keys(raw)
    return jsonify(success=True, count=len(keys))


@app.post("/api/videos")
def creator_videos():
    body = request.get_json(silent=True) or {}
    handle = (body.get("handle") or "").strip().lstrip("@")
    sort_by = body.get("sort_by") or "latest"
    region = (body.get("region") or "").strip()
    max_pages = min(max(int(body.get("max_pages") or 1), 1), 100)

    if not handle:
        return jsonify(success=False, error="Enter a TikTok username."), 400

    items = []
    cursor = None
    pages = 0
    try:
        while pages < max_pages:
            key = require_key()
            params = {"handle": handle, "sort_by": sort_by, "trim": "true"}
            if region:
                params["region"] = region
            if cursor is not None:
                params["max_cursor"] = str(cursor)

            data = api_get("/v3/tiktok/profile/videos", key, params)
            batch = data.get("aweme_list") or []
            for video in batch:
                video_obj = video.get("video") or {}
                cover = first_url(video_obj.get("dynamic_cover")) or first_url(video_obj.get("cover"))
                aweme_id = str(video.get("aweme_id") or video.get("id") or "")
                author = video.get("author") or {}
                unique_id = author.get("unique_id") or author.get("uniqueId") or handle
                url = video.get("share_url") or video.get("url") or (
                    f"https://www.tiktok.com/@{unique_id}/video/{aweme_id}" if aweme_id else ""
                )
                items.append({
                    "id": aweme_id,
                    "url": url,
                    "title": video.get("desc") or "Untitled TikTok",
                    "cover": cover,
                    "duration": format_seconds(video_obj.get("duration") or video.get("duration") or 0),
                })

            pages += 1
            if not data.get("has_more"):
                break
            next_cursor = data.get("max_cursor")
            if next_cursor is None or str(next_cursor) == str(cursor):
                break
            cursor = next_cursor

        return jsonify(success=True, videos=items, pages=pages)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 502


@app.post("/api/transcripts")
def bulk_transcripts():
    body = request.get_json(silent=True) or {}
    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()

    if not links:
        return jsonify(success=False, error="Paste at least one valid TikTok video URL."), 400
    if len(links) > 100:
        return jsonify(success=False, error="Maximum 100 links per batch."), 400

    results = []
    for index, url in enumerate(links, 1):
        try:
            info = get_video_info(url, transcript=True, region=region, cache_age=cache_age)
            results.append({
                "index": index,
                "url": url,
                "success": True,
                "title": info["title"],
                "duration": info["duration"],
                "transcript": info["transcript"],
                "cached": info["cached"],
                "credits_charged": info["credits_charged"],
            })
        except Exception as exc:
            results.append({"index": index, "url": url, "success": False, "error": str(exc)})

    return jsonify(success=True, results=results)


@app.post("/api/media-info")
def media_info():
    body = request.get_json(silent=True) or {}
    links = parse_links(body.get("links") or "")
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()

    if not links:
        return jsonify(success=False, error="Paste at least one valid TikTok video URL."), 400
    if len(links) > 100:
        return jsonify(success=False, error="Maximum 100 links per batch."), 400

    results = []
    for index, url in enumerate(links, 1):
        try:
            info = get_video_info(url, transcript=False, region=region, cache_age=cache_age)
            results.append({
                "index": index,
                "url": url,
                "success": True,
                "title": info["title"],
                "duration": info["duration"],
                "video_url": info["video_url"],
                "audio_url": info["audio_url"],
                "cached": info["cached"],
                "credits_charged": info["credits_charged"],
            })
        except Exception as exc:
            results.append({"index": index, "url": url, "success": False, "error": str(exc)})

    return jsonify(success=True, results=results)


@app.post("/api/proxy-download")
def proxy_download():
    body = request.get_json(silent=True) or {}
    media_url = (body.get("media_url") or "").strip()
    filename = (body.get("filename") or "tiktok_media").strip()
    media_type = body.get("media_type") or "video"

    if not media_url.startswith(("https://", "http://")):
        return jsonify(success=False, error="Invalid media URL."), 400
    if media_type not in {"audio", "video"}:
        return jsonify(success=False, error="Invalid media type."), 400

    try:
        r = requests.get(media_url, timeout=TIMEOUT, stream=True)
        r.raise_for_status()
        content = r.content
        mime = "audio/mpeg" if media_type == "audio" else "video/mp4"
        return Response(content, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except Exception as exc:
        return jsonify(success=False, error=f"Media download failed: {exc}"), 502


@app.post("/api/download")
def download_zip():
    body = request.get_json(silent=True) or {}
    media_type = body.get("media_type") or "video"
    items = body.get("media_items") or []

    if media_type not in {"audio", "video"}:
        return jsonify(success=False, error="Invalid media type."), 400
    if not items:
        return jsonify(success=False, error="No media items supplied."), 400

    files = []
    errors = []
    ext = ".mp3" if media_type == "audio" else ".mp4"

    for index, item in enumerate(items, 1):
        try:
            media_url = item.get("audio_url") if media_type == "audio" else item.get("video_url")
            if not media_url:
                raise RuntimeError(f"No {media_type} URL was returned")
            safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", item.get("title") or "Untitled TikTok").strip("._-")[:80] or f"video_{index}"
            filename = f"{index:02d}_{safe_title}{ext}"
            media = requests.get(media_url, timeout=TIMEOUT)
            media.raise_for_status()
            files.append((filename, media.content))
        except Exception as exc:
            errors.append(f"{index}: {item.get('url', '')} — {exc}")

    if not files:
        return jsonify(success=False, error="No files could be downloaded.", errors=errors), 502

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files:
            archive.writestr(filename, content)
        if errors:
            archive.writestr("download_errors.txt", "\n".join(errors))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"tiktok_{media_type}s.zip", mimetype="application/zip")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
