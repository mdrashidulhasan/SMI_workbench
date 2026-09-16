# v52 runtime fixes

This update keeps the v37-based workflow and the latest dynamic-lens/title-abstract extraction changes, and fixes two runtime crashes seen in v51/v37-derived builds.

Changes:

- Fixed `KeyError: nitrogen dioxide (NO2)` during intelligence generation by using a safe exposure-type lookup for normalized exposure labels.
- Added normalized exposure labels to the exposure-type mapping, including `nitrogen dioxide (NO2)` and `ozone (O3)`.
- Fixed Knowledge Map crash caused by `selected_project` being referenced before assignment.

No search, deduplication, AI screening, PDF, or Knowledge Space workflow order was changed.
