TikTok Tools v5 — background creator fetching and bulk downloading.

Render settings:
Build: pip install -r requirements.txt
Start: gunicorn app:app --bind 0.0.0.0:$PORT
Root Directory: blank
Instance: Free

Creator Videos now run as a background job. The browser polls progress while the server follows cursor pagination, so large accounts do not depend on one long HTTP request. Partial videos remain available if a later page fails.

Load choices: 10, 25, 50, 100, 250, 500, 1000, or All available.
Sort: Latest or Most popular.

Bulk ZIP creation is also a background job with progress and controlled concurrency.
API keys are kept in the browser localStorage and restored to the server pool on page load. The active server pool is in memory.
