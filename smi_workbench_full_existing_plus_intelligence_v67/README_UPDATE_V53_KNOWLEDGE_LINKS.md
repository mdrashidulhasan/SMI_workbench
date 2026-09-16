# v53 Knowledge Space paper links and lens cleanup

This update keeps the v37-based workflow and v52 runtime fixes, and changes only the Knowledge Space UI/filtering layer.

Changes:
- Adds direct Evidence Pack links from Knowledge Map dots.
- Adds a related-papers table below the Knowledge Map with Cluster, Paper, Exposure(s), Outcome(s), and Open Evidence Pack.
- Cleans topic-lens cluster summaries by merging duplicate labels across clusters.
- For a lens such as Pregnancy, labels are normalized to forms such as `pregnancy + particulate matter`, `pregnancy + heat`, and `pregnancy + PFAS`.
- Tiny one-paper lens fragments are grouped into an "other <topic>-related topics" row when there are many small fragments.
- Adds dynamic lens recognition for EV battery topics such as electric vehicle battery, lithium-ion battery, battery recycling, battery waste, cobalt, nickel, lithium, manganese, and battery fire emissions.

Unchanged:
- Search, deduplication, AI screening, PDF workflow, extraction generation, and Knowledge Space generation logic.
