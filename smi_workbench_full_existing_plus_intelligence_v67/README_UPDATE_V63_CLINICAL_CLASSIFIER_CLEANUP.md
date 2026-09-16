# v63 clinical classifier cleanup

This update strengthens the title/abstract clinical extraction layer so broad clinical searches do not force unrelated papers into one generic `clinical epidemiology cohort` row.

Key changes:

- Renamed the displayed clinical module label to `Clinical / Health Services / Health Equity`.
- Added/strengthened article-type routing for:
  - Clinical / Health Services Research
  - Real-world Observational Treatment Study
  - Clinical Trial / Intervention Study
  - Clinical Registry / Protocol / Design Paper
  - Surgical Comparative Effectiveness Study
  - Clinical Oncology / Transplant Treatment Outcomes
  - Translational Oncology / Multi-omics Biomarker Study
  - Education / Simulation Evaluation Study
  - Health Equity Cohort
- Restricted `Health Equity` labeling to papers that actually mention equity/disparities/race/ethnicity/SDOH/underserved/access/utilization patterns.
- Added oncology/transplant treatment outcome extraction for HCT/AZA/AML/MDS papers.
- Added translational oncology biomarker extraction for LUAD/NSCLC immune landscape, multi-omics, single-cell, machine-learning, DPIS, and TPX2 papers.
- Improved Evidence Pack item labels so long clinical outcome lists are shown under structured fields instead of crowding the ITEM column.
- Added cleanup for journal/source fragments such as `Frontiers in immunology` and section labels such as `STUDY DESIGN:`.
- Continued conservative handling of recruitment/enrollment/study-site fields: they are filled only when explicitly supported by title/abstract/available metadata.

For existing runs, use Generate intelligence outputs again so the corrected rows are rebuilt.
