# Title/Abstract Extraction Fix v2

This build keeps the v37 workflow base and the previous UI/PDF changes, then tightens the title/abstract-only extraction layer.

Focused fixes:

- Use the longest available abstract-like field from the retrieved record instead of a short snippet when both are present.
- Keep extraction title/abstract-only; PDF/body/full-text fields are intentionally ignored.
- Mark article-method rows as `yes; possibly incomplete` when the abstract appears truncated mid-sentence.
- Fix study design priority so explicit retrospective/prospective cohort language overrides model words such as generalized additive model.
- Avoid title-only evidence when an abstract methods/results sentence is available.
- Improve PM2.5, PM10, NO2, O3, and black-carbon cleanup from abstract text where subscripts are lost.
- Improve sample-size extraction for main cohort plus longitudinal subcohort examples.
- Add coronary inflammation / pericoronary fat attenuation index as a more specific outcome.
- Expand statistical method extraction for natural cubic splines, lag models, stratified analyses, two-pollutant models, sensitivity analyses, deep-learning-based pipelines, and spatiotemporal exposure models.

Kept from the previous build:

- v37 workflow behavior.
- EDT time display.
- AI screening count.
- Auto Variables removed from Knowledge Space.
- Targeted PDFs removed.
- Package-first PDF matching.
