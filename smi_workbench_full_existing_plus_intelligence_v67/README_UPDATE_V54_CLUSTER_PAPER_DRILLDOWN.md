# v54 update: Knowledge Map cluster paper drill-down

This update keeps the v53/v37-based workflow behavior and adds a cleaner way to inspect all papers behind a Knowledge Space cluster.

Changes:

- Added a cluster selector to the Knowledge Cluster Map.
- Added a "Show papers" action next to each visible/filtered cluster row.
- Selecting a cluster filters the map to that cluster and shows all papers from that cluster in the related-papers table.
- The default unfiltered related-papers table still shows a lightweight preview of up to 200 papers.
- When a cluster is selected, the table no longer stops at 200; it shows all papers in the chosen cluster(s).
- Each paper row keeps its Evidence Pack link.

Unchanged:

- Literature search behavior.
- Deduplication.
- AI screening and ALL chunked AI behavior.
- Title/abstract extraction fixes.
- EDT display.
- AI screening count.
- Auto Variables removed.
- Targeted PDFs removed.
- Package-first PDF matching.
