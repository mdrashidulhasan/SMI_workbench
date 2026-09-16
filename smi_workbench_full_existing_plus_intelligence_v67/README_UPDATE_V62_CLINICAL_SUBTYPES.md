# v62 clinical subtype extraction update

This update expands the clinical extraction module so broad clinical/observational papers are not forced into one generic "clinical epidemiology cohort" row.

Added or improved subtype handling for:

- clinical epidemiology / health services cohorts, including advanced kidney disease, KRT decisions, conservative kidney management, administrative codes, clinical progress notes, large US health systems, and descriptive trend analyses;
- real-world prospective observational treatment studies, including ROSSINI-style routine clinical practice studies, drug/device/intervention exposure, OFF time endpoints, adverse events, interim analysis timing, and mixed-effects models for repeated measurements;
- surgical comparative effectiveness / orthopedic outcomes studies, including total knee arthroplasty, kinematic versus mechanical alignment, Forgotten Joint Score, range of motion, follow-up windows, and multicenter surgical site logic.

Also improved evidence-sentence selection so the clinical row favors actual methods/design/endpoint sentences over background motivation sentences, and strips common journal-title fragments from truncated evidence text.
