# v60 update: clinical subtype extraction fixes

This update improves the Clinical Epidemiology / Health Equity extraction module so broad clinical searches do not force every record into a generic clinical epidemiology cohort row.

## Added / improved

- Subtype-aware classification inside the existing clinical module:
  - clinical epidemiology / health equity cohort
  - real-world cohort study
  - clinical registry / study design paper
  - randomized placebo-controlled clinical trial / translational mechanistic study
  - pharmacy education / simulation evaluation study
- Improved extraction for:
  - Recruitment Site
  - Enrollment Site
  - Study Site
  - Data Source
  - Study Period
  - Comparator
  - Predictor or Exposure
  - Health Outcome
  - Sample Size
  - Statistical Methods
- Added rules for examples involving:
  - CURE-CKD Registry / two US health systems / AA and AI/AN CKD care delivery
  - standardized patient vs pharmacist evaluator simulation studies
  - SGLT2/dapagliflozin randomized placebo-controlled trials
  - STEADY diabetic foot ulcer registry design papers
- Reduced false generic outputs such as:
  - Population = cohort participants
  - Predictor = diabetes
  - Outcome = diabetes
  - Evidence sentence = background sentence

## Evidence-sentence priority

The clinical module now prefers subtype-specific methods/results/design sentences over background sentences.
