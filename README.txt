TikTok Tools v2

Features:
1. Creator Videos: optional profile browsing using the profile-videos endpoint.
2. Bulk Transcripts: paste one or many TikTok URLs; the app calls the v2 Video Info endpoint with get_transcript=true, which can return title, duration, and transcript in the same request.
3. Audio / Video: paste one or many TikTok URLs; the app calls Video Info once per unique URL, then streams the returned media URLs and can create ZIPs.

Credit-saving design:
- Direct-link transcript/media modes do NOT load profiles, comments, likes, followers, or following.
- Video Info is 1 credit per live request.
- cache_max_age can make fresh cached requests cost 0 credits.
- download_media=true is intentionally NOT used because it adds 10 credits when media is found.
- Media bytes are fetched from the returned TikTok CDN URL after the API call; that download itself does not spend Scrape Creators credits.

Run:
pip install -r requirements.txt
python app.py
Open http://127.0.0.1:5000

For production deployment on Render, use a Python web service with:
Build: pip install -r requirements.txt
Start: gunicorn app:app
