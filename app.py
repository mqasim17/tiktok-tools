from flask import Flask, render_template, request, jsonify, Response, send_file
import io
import re
import zipfile
import requests
from urllib.parse import urlparse

app = Flask(__name__)

BASE = "https://api.scrapecreators.com"
TIMEOUT = 90

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

            data = api_get("/v3/tiktok/profile/videos", key, params)
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
            info = get_direct_info(key, url, get_transcript=True, cache_age=cache_age, region=region)
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
            info = get_direct_info(key, url, get_transcript=False, cache_age=cache_age, region=region)
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

@app.post("/api/download")
def download():
    body = request.get_json(force=True)
    key = (body.get("api_key") or "").strip()
    links = parse_links(body.get("links") or "")
    media_type = body.get("media_type") or "audio"
    region = (body.get("region") or "").strip()
    cache_age = (body.get("cache_age") or "30d").strip()
    want_zip = bool(body.get("zip"))

    if not key:
        return jsonify(success=False, error="API key is required."), 400
    if not links:
        return jsonify(success=False, error="Paste at least one TikTok video URL."), 400
    if media_type not in {"audio", "video"}:
        return jsonify(success=False, error="media_type must be audio or video."), 400

    files = []
    errors = []

    for idx, url in enumerate(links, 1):
        try:
            info = get_direct_info(key, url, get_transcript=False, cache_age=cache_age, region=region)
            media_url = info["audio_url"] if media_type == "audio" else info["video_url"]
            if not media_url:
                raise RuntimeError(f"No {media_type} URL was returned for this TikTok.")
            ext = ".mp3" if media_type == "audio" else ".mp4"
            safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", info["title"]).strip("._-")[:80] or f"video_{idx}"
            filename = f"{idx:02d}_{safe_title}{ext}"
            media = requests.get(media_url, stream=True, timeout=TIMEOUT)
            media.raise_for_status()
            content = media.content
            files.append((filename, content))
        except Exception as e:
            errors.append({"index": idx, "url": url, "error": str(e)})

    if not files:
        return jsonify(success=False, error="No files could be downloaded.", errors=errors), 502

    if not want_zip and len(files) == 1:
        filename, content = files[0]
        mimetype = "audio/mpeg" if media_type == "audio" else "video/mp4"
        return Response(
            content,
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, content in files:
            z.writestr(filename, content)
        if errors:
            z.writestr(
                "download_errors.txt",
                "\n".join(f"{e['index']}: {e['url']} — {e['error']}" for e in errors)
            )
    buf.seek(0)
    zip_name = f"tiktok_{media_type}s.zip"
    return send_file(buf, as_attachment=True, download_name=zip_name, mimetype="application/zip")

@app.get("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
