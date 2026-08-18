TikTok Tools v7

Fixes:
- Large creator ZIP downloads no longer POST hundreds of full video objects.
- Creator selections are referenced by server-side creator_job_id + indices.
- Request-size (413) failures return JSON.
- Browser JSON parsing reports HTML/proxy responses clearly.
- Background creator loading and ZIP jobs remain progressive.
- API keys remain browser-persisted via localStorage.
