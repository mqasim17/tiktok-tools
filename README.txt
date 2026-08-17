TikTok Tools v4 — bulk creator loader, transcript extractor, and media downloader.

Render:
Build: pip install -r requirements.txt
Start: gunicorn app:app --bind 0.0.0.0:$PORT
Root Directory: blank
Instance: Free

Features:
- Creator Videos: 10/25/50/100/250/500/1000/All, Latest or Popular.
- Bulk creator pagination using the Profile Videos endpoint.
- Highest available video rendition selected from profile video bit_rate/play_addr data.
- Bulk ZIP downloads run as background jobs with progress and controlled concurrency.
- Direct-link Bulk Transcript Extractor.
- Direct-link Media Downloader.
- API key pool rotates keys per API request.
- API keys are saved in browser localStorage and automatically restored to the server on page load.
- Server-side key pool is in memory; browser persistence prevents re-entry after normal Render restarts/sleeps on the same device/browser.
- Never store API keys in GitHub.

The Profile Videos endpoint costs 1 credit per request. The Video Info endpoint costs 1 credit per live request, with fresh cached responses eligible for 0-credit responses. The app does not use download_media=true for direct media links.
