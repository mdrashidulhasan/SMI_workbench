# v66 clinical extraction expansion

This update expands the title/abstract clinical extractor and fixes recent broad clinical examples.

Changes:
- Added Hybrid Type 2 effectiveness-implementation / stepped-wedge protocol extraction.
- Added longitudinal genetic epidemiology / twin cohort extraction.
- Improved structured RECOVER-style SDoH/long-COVID cohort extraction.
- Strengthened translational oncology / multi-omics biomarker extraction.
- Strengthened pediatric oncology / HCT / azacitidine treatment-outcomes extraction.
- Prevented generic clinical-trial fallbacks from inserting SGLT2/placebo labels unless those terms are present.
- Improved structured abstract section handling for DESIGN, SETTING, PARTICIPANTS, MEASUREMENTS, and DATA AND METHODS.
- Added more conservative site handling for recruitment, enrollment, and study site fields.

For existing runs, regenerate intelligence outputs to rebuild the rows with these rules.
