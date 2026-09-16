# v58 update: PDF workers and PDF progress

This build keeps the v57 workflow and extraction behavior, with two PDF-stage UI/runtime updates:

- `SMI_PDF_MAX_WORKERS` is now capped at 20 instead of 4 when explicitly set.
- The live run status page now shows a PDF download count, similar to the AI screening count. It reads `outputs/05_pdf_download_manifest.json`, which is checkpointed after each completed PDF batch.

Recommended conservative settings:

```bash
export SMI_PDF_BATCH_SIZE=5
export SMI_PDF_MAX_WORKERS=2
export SMI_PDF_TEXT_PROBE=0
export SMI_API_TIMEOUT=900
```

Higher-worker settings can be tested if the API/VPN is stable, but too many PDF workers can cause API timeouts.
