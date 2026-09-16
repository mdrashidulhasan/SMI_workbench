# Update: ALL AI Screening Uses Chunking

This build keeps the v37-based workflow and the recent UI/extraction updates, but fixes the AI screening path for ALL runs.

## Changed

- If AI screening limit is ALL, the workflow now retrieves concrete deduplicated records page by page.
- It then splits those records into chunks using `SMI_AI_SCREEN_CHUNK_SIZE`.
- It runs chunks in parallel using `SMI_AI_SCREEN_MAX_WORKERS`.
- It refuses to send ALL as one giant AI screening API request if concrete records cannot be retrieved.
- The worker cap now allows 5 workers.

## Recommended ALL settings

```bash
export SMI_API_TIMEOUT=900
export SMI_AI_SCREEN_CHUNKING=1
export SMI_AI_SCREEN_CHUNK_SIZE=125
export SMI_AI_SCREEN_INPUT_PAGE_SIZE=500
export SMI_AI_SCREEN_MIN_CHUNK_SIZE=25
export SMI_AI_SCREEN_PARALLEL=1
export SMI_AI_SCREEN_MAX_WORKERS=5
export SMI_AI_SCREEN_RETRIES=1
```

For an ALL run with 8,840 records and chunk size 125, logs should show about 71 chunks and `max_workers=5`.
