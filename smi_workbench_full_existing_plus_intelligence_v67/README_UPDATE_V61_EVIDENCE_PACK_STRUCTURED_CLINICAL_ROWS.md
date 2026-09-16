# v61 Evidence Pack structured clinical row display

This update fixes the Evidence Pack display for clinical epidemiology / health equity papers.

## What changed

- Evidence Packs opened from a source/citation table now promote the matching structured extraction row when one exists, instead of showing only Source Title and Citation in the Selected extracted row card.
- The "All extracted facts from this paper" table now includes expandable structured fields for every related extraction row.
- Clinical rows now use a clearer item label based on evidence type and health outcome, rather than collapsing all predictors/population/comparator terms into the Item column.
- The clinical extraction engine still writes the full structured fields: evidence type, population, comparator, predictor/exposure, health outcome, study design, effect direction/estimate, study period, data source, recruitment site, enrollment site, study site, methods, and evidence sentence.

## Why

The v60 extractor was already detecting structured facts for papers such as AI/AN diabetes kidney-event studies, but the Evidence Pack page could hide those fields in a compact module summary. This update makes the structured row visible in the Evidence Pack UI.
