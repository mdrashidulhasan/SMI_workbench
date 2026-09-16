# v3 title/abstract extraction refinement

This update keeps the v37 workflow base and v2 title/abstract extraction changes, with one refinement:

- The `statistical_methods` field now reports primary statistical models only.
- Ancillary analysis details such as lag windows, natural cubic splines, stratified analyses, two-pollutant models, sensitivity analyses, and exposure-response checks are no longer concatenated into the main Statistical Methods field.
- Example: an abstract that says "Generalized additive quasi-Poisson models were used..." now returns `generalized additive quasi-Poisson models` instead of a long list of supporting analyses.
