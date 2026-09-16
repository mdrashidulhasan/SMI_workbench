# v59 Clinical epidemiology and site extraction update

Adds a Clinical Epidemiology / Health Equity intelligence module. The module is title/abstract/available-metadata based and is designed for clinical/EHR/cohort papers that may not have a traditional environmental exposure.

New extracted fields include:

- Recruitment Site
- Enrollment Site
- Study Site
- Data Source
- Study Period
- Comparator
- Predictor or Exposure

The module captures examples such as Providence health system electronic health records, American Indian or Alaska Native adults with diabetes, major adverse kidney events, social drivers of health, health care utilization, Kaplan-Meier analyses, and explicit recruitment/enrollment/study-site phrases.

Site fields remain `not specified` unless the location or site is explicitly present in title, abstract, or available non-PDF record metadata.
