TikTok Tools v7

Fixes:
- Large creator ZIP downloads no longer POST hundreds of full video objects.
- Creator selections are referenced by server-side creator_job_id + indices.
- Request-size (413) failures return JSON.
- Browser JSON parsing reports HTML/proxy responses clearly.
- Background creator loading and ZIP jobs remain progressive.
- API keys remain browser-persisted via localStorage.


v10 fix: added the missing background bulk ZIP worker. ZIP downloads use up to 8 concurrent media downloads and write a low-compression ZIP for faster completion.

v11 final:
- Media/transcript preparation uses up to 8 concurrent Video Info requests.
- API keys are remembered in browser localStorage and automatically sent with every request, eliminating post-restart key races.
- Creator pagination remains cursor-safe and backgrounded.
- Bulk media uses up to 8 concurrent downloads with reusable HTTP sessions.
- ZIP is ZIP_STORED for maximum speed because MP4/MP3 are already compressed.
- API routes always return JSON errors for /api/* paths.

v12 fix: restored the Flask @app.post('/api/media-info') route decorator. The Media Downloader Prepare Downloads endpoint now maps to the existing concurrent media-info handler.

v13: Media Downloader improvements. The media-info call now requests an untrimmed Video Info response so the documented `added_sound_music_info.play_url` audio URL is retained. Direct native download links were added for individual video/audio files, using a streaming TikTok-CDN proxy so files do not need to be buffered in the browser. ZIP downloads remain available for batches.

v14 fix: media URL validation now accepts the TikTok CDN families actually returned by Scrape Creators, including tiktokcdn-us.com, tiktokcdn-eu.com, tiktokcdn-in.com, tiktokcdn.com, tiktokv.eu, tiktokv.us, tiktokv.com, and known TikTok media delivery families.

v15 addition: Video Research tab. One Video Info request (with transcript) supplies title, duration, and engagement statistics; the Comments endpoint is paginated and costs 1 credit per request. The research job runs in the background and can collect all available top-level comments or a selected limit, with TXT/JSON exports. Comment replies are not fetched in this version because the API documents them as a separate 1-credit-per-request endpoint per comment thread.

v16: research polling is lightweight; full comments/transcripts/statistics are returned only once at job completion. Research copy/export helpers are isolated from existing tools.
