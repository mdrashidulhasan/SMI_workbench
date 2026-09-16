# Update: dynamic Knowledge Map buttons and title/abstract extraction cleanup

This build keeps the v37-based workflow and the previous title/abstract extraction fixes, then adds the following focused changes:

- Knowledge Cluster Map buttons are now generated from the current run instead of fixed Heat/PFAS/PM2.5 buttons.
- The map shows up to five suggested topic buttons from extracted exposures, outcomes, cohorts, cluster labels, and current-run terms such as microplastics/nanoplastics when present.
- Filtered cluster legends now summarize only matching records, so a Heat or Microplastics lens does not list unrelated dominant cluster labels from the full cluster.
- Cohort rows now carry source_title when available, and the UI tries to recover the source title from retrieved article metadata when a row only has DOI/PMID citation data.
- Exposure labels are normalized and de-duplicated, e.g. PM2.5; nitrogen dioxide (NO2); ozone (O3).
- Biospecimens are reported only when explicitly stated in the title/abstract, e.g. serum, plasma, urine, blood. Generic guessed biospecimen text was removed.
- Microplastics, nanoplastics, and plastic additives were added as recognized exposure/topic terms for extraction and Knowledge Space.

No search, deduplication, AI-screening, or PDF API workflow order was changed in this update.
