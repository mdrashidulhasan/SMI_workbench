# v64 UI scope cleanup

This update removes the three user-facing AI screening scope text boxes from the New Run page:

- Study type outside scope
- Exposure outside scope
- Outcome outside scope

New runs now write an empty AI-screening `exclusion_criteria` block instead of the old environmental-health-only screening criteria. This prevents broad clinical, health services, registry, oncology, treatment, and education/simulation papers from being screened with environmental exposure/outcome assumptions.

The New Run page also removes the `Download all PDFs / no limit` control. Users can still set a numeric row-level PDF download limit, but the UI no longer exposes an unlimited PDF option.
