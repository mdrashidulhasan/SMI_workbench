# Knowledge Space Summary

Generated: 2026-08-21T14:50:11Z

## What this layer does

This layer converts each article into an abstract-only knowledge vector using extracted exposures, outcomes, datasets, cohorts, populations, study designs, sample size, population size, statistical methods, effect direction, publication year, and hashed abstract-text features.
The map is useful because reviewers can see thematic neighborhoods, filter to a topic such as heat, find related papers quickly, detect emerging or isolated topics, and prioritize clusters that contain policy-relevant or conflicting evidence.

## Large-scale behavior

- Articles vectorized: 71
- Large-scale mode: no
- Clustering method: agglomerative_cosine
- Similarity output mode: full_pairwise
- PCA projection: computed
- UMAP projection: umap_not_installed
- Knowledge text source: abstracts only

## Counts

- Clusters detected: 23
- Priority queue items: 71

## How people benefit

- Search 20k-100k records without reading them linearly.
- Filter to a concept such as heat, PFAS, PM2.5, asthma, pregnancy, or cardiovascular outcomes and view only that subspace.
- See which papers are central, which are outliers, and which clusters represent emerging evidence.
- Locate datasets, cohorts, exposures, outcomes, and statistical methods that recur across a large literature.
- Build a faster systematic-review triage queue instead of manually sorting thousands of citations.

## Clusters

- Cluster 1: heat/temperature + cardiovascular (10 article(s))
- Cluster 2: particulate matter + pregnancy (10 article(s))
- Cluster 3: particulate matter + cardiovascular (7 article(s))
- Cluster 4: PFAS + pregnancy (6 article(s))
- Cluster 5: PFAS + cardiovascular (4 article(s))
- Cluster 6: heat/temperature + pregnancy (4 article(s))
- Cluster 7: particulate matter + asthma (4 article(s))
- Cluster 8: air pollution + cardiovascular (3 article(s))
- Cluster 9: heat/temperature + asthma (3 article(s))
- Cluster 10: PM2.5 + cardiovascular (3 article(s))
- Cluster 11: particulate matter + heart disease (2 article(s))
- Cluster 12: heat/temperature + mortality (2 article(s))
- Cluster 13: Mixed environmental health topic (2 article(s))
- Cluster 14: particulate matter + oxidative stress (2 article(s))
- Cluster 15: PM2.5 (1 article(s))
- Cluster 16: PM2.5 + immune (1 article(s))
- Cluster 17: PFAS + birth weight (1 article(s))
- Cluster 18: nitrogen dioxide (NO2) + cardiovascular (1 article(s))
- Cluster 19: nitrogen dioxide (NO2) + asthma (1 article(s))
- Cluster 20: air pollution + asthma (1 article(s))

## Highest priority items

- Rank 1: Perfluoroalkyl/Polyfluoroalkyl Substances, Hemostatic/Inflammatory Biomarkers, and Incident Hypertension: The Study of Women's Health Across the Nation. — score 0.6268 (may conflict with cluster direction; contains chemical or chemical-class signal)
- Rank 2: Association of PFAS exposure with maternal blood pressure and hypertension during pregnancy in the PROTECT cohort. — score 0.6052 (may conflict with cluster direction; contains chemical or chemical-class signal)
- Rank 3: Prenatal PFAS exposure and early childhood sleep quality in the ECHO Cohort. — score 0.5556 (may conflict with cluster direction; contains dataset/cohort/geospatial signal; contains chemical or chemical-class signal)
- Rank 4: Individual and neighborhood factors influencing pediatric asthma exacerbations during urban heatwaves. — score 0.5453 (may conflict with cluster direction)
- Rank 5: A comprehensive study on the association of personal air pollution exposure with thyroid hormones in pregnant women. — score 0.5012 (may conflict with cluster direction)
- Rank 6: Weather Information Seeking and Heat-Health Protective Actions During Pregnancy: An Exploratory Study. — score 0.4603 (may conflict with cluster direction)
- Rank 7: A prospective twin cohort investigates placental transfer efficiency as a potential determinant of fetal growth across a 300-xenobiotic maternal-fetal exposure landscape. — score 0.4523 (novel compared with prior/cluster records; contains chemical or chemical-class signal)
- Rank 8: Prenatal Air Pollution Exposure and Autism Spectrum Disorder in the ECHO Consortium. — score 0.4186 (may conflict with cluster direction; contains dataset/cohort/geospatial signal)
- Rank 9: Effects of per- and polyfluoroalkyl substances on maternal thyroid function: a stratified analysis by maternal iodine status and thyroid autoantibody status. — score 0.4025 (novel compared with prior/cluster records; contains chemical or chemical-class signal)
- Rank 10: Genetic susceptibility to respiratory health effects from outdoor air pollution: a structured narrative review. — score 0.3808 (may conflict with cluster direction)
- Rank 11: Imaging and Molecular Biomarkers of PFAS-Related Vascular Aging: A Narrative Review. — score 0.3768 (contains chemical or chemical-class signal)
- Rank 12: Integrated Computational and In Vivo Evidence Prioritizes MYH6 as a Candidate Node in PFOS-Associated HCM-like Cardiac Remodeling. — score 0.3722 (contains chemical or chemical-class signal)
- Rank 13: Targeted inhibition of JAK2 phosphorylation by GenX disrupts the JAK2-STAT3 pathway and induces cardiovascular toxicity in zebrafish: Insights from network toxicology and molecular dynamics. — score 0.3722 (contains chemical or chemical-class signal)
- Rank 14: Case-Crossover Analysis of Short-Term Particulate Matter and Health Outcomes in Patients With Atrial Fibrillation. — score 0.368 (contains dataset/cohort/geospatial signal)
- Rank 15: The Impact of Heat and Bushfire Smoke on Health System Utilisation in Australia. — score 0.3606 (contains dataset/cohort/geospatial signal)