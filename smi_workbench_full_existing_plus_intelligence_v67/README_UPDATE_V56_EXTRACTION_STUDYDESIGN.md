# v56 Extraction Study-Design Fix

This update keeps the v55 workflow and UI behavior, and fixes title/abstract extraction issues observed in PFAS pregnancy cohort abstracts.

Changes:
- Prevents false `experimental animal` labels caused by substring matches such as `rat` inside words like concentrations/gestation/arterial. Animal design now requires explicit animal-model terms with word boundaries.
- Recognizes named pregnancy cohorts such as the Puerto Rico PROTECT cohort as observational pregnancy cohort studies.
- Improves population text for PROTECT-style pregnancy cohort abstracts.
- Adds maternal blood pressure, systolic/diastolic BP, pulse pressure, and mean arterial pressure as outcomes.
- Allows method sentences to serve as evidence when exposure/outcome context is available elsewhere in the same title/abstract, reducing title-only evidence rows.
- Keeps extraction title/abstract-only; no PDF/full-text extraction was added.
