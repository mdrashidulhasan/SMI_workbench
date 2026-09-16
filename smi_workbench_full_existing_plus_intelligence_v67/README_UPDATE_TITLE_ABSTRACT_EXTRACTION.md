# Title + Abstract Extraction Fix

This build keeps the v37 workflow base and applies a focused extraction-quality fix.

Changed:

- Intelligence extraction uses title + abstract only for association, cohort, chemical, dataset, geospatial, and article-method rows.
- Chemical Watchlist now requires same-paper title/abstract evidence and no longer uses ambiguous metal element symbols such as `As`, `Cd`, `Hg`, or `Pb` as aliases.
- Study design classification prioritizes human epidemiology designs such as time-series, cohort, case-crossover, cross-sectional, and case-control before animal/in-vitro labels.
- Pregnancy/prenatal wording is treated as exposure window/context, not automatically as a health outcome.
- Cohort extraction captures sample sizes such as `8,035 mother-child pairs` and improves ECHO extraction.
- Evidence sentences prefer METHODS/RESULTS/CONCLUSIONS text over title-only evidence.
- PM2.5 / NO2 / O3 formatting artifacts are cleaned more aggressively.

Kept from the current v37-based build:

- v37 workflow behavior
- EDT time display
- AI screening count
- Auto Variables removed from Knowledge Space
- Targeted PDFs removed
- Package-first PDF matching
- Cleaner UI notices
