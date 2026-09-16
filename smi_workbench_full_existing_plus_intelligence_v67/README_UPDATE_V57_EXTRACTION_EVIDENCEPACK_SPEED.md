# v57 update

This build keeps the v37-based workflow and adds only targeted fixes requested after v56:

- Improved title/abstract extraction for tree-canopy, heat/temperature, mortality, decedent, IRR/CI, and longitudinal neighborhood/ecological study patterns.
- Added negative binomial generalized estimating equations as a primary statistical method.
- Captures sample sizes such as "220,711 decedents".
- Uses results sentences with IRR/CI as evidence when available instead of background sentences.
- Allows SMI_AI_SCREEN_MAX_WORKERS up to 100 when explicitly set by the user.
- Speeds up Evidence Pack opening by caching retrieved records/module rows and by not rebuilding PDF/citation matching on every Evidence Pack page load.

To load PDF status inside Evidence Pack pages, start the app with:

```bash
export SMI_EVIDENCE_PACK_LOAD_PDF_STATUS=1
```

Otherwise, use the Outputs page for PDF/package links.
