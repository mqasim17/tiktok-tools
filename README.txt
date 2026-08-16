TikTok Tools — Render-ready

Features:
- Creator Videos: load a creator's videos, select videos, and send them to transcript/media tools.
- Bulk Transcript Extractor: paste multiple direct TikTok URLs and extract title, duration, and readable timestamped transcripts.
- Media Downloader: paste multiple direct TikTok URLs, prepare media URLs once, then download individual audio/video files or ZIPs without another Scrape Creators request.
- API Key Pool: save multiple user-owned Scrape Creators API keys and use them round-robin for separate API requests.

Render:
Build command: pip install -r requirements.txt
Start command: gunicorn app:app --bind 0.0.0.0:$PORT
Root directory: blank
Instance: Free

Notes:
- API keys are stored only in server memory for the current running instance. Render restarts/spin-downs clear the pool.
- Direct transcript/media modes do not load profile, likes, comments, followers, or engagement data.
- Scrape Creators' Video Info endpoint is used with get_transcript=true for transcript extraction. Cached responses can cost 0 credits.
