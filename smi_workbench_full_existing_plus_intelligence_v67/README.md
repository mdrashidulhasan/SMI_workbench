# SMI Workbench v52

This build keeps the v37-based workflow and adds the v52 runtime fixes for normalized exposure labels and Knowledge Map routing.

# SMI Workbench v12: Existing App + Intelligence + Knowledge Space

This package keeps the existing SMI Workbench features and adds a new **Knowledge Space** layer.

## Title + Abstract Extraction Fix

This build keeps the v37 workflow base and adds a focused extraction-quality update: association, cohort, chemical, dataset, geospatial, and article-method extraction are grounded in title + abstract only, with stricter sentence/evidence validation.


## Preserved app features

- Dashboard
- New Run
- Stop Run
- Clear Form
- Clear Results
- Approvals and resume workflow
- Outputs / PDFs with original API PDF policy
- Citation table CSV
- Statistics
- Risk Assessment
- Intelligence modules for associations, datasets, geospatial data, cohorts, and chemicals

## New in v12

The app now adds a **Knowledge Space** section that implements the systematic plan:

- Knowledge vectors for each article
- Cosine similarity between article vectors
- Hierarchical/agglomerative clustering using cosine similarity
- PCA coordinates for a 2D cluster map
- Novelty scores
- Support/dispute labels based on effect-direction differences inside clusters
- Priority Review Queue
- Exposure x Outcome evidence matrix
- Downloadable JSON/CSV/Markdown outputs

## New output files

When Intelligence Extraction runs, the workflow also creates:

```text
13_knowledge_vectors.json / .csv
14_article_similarity.csv
15_knowledge_clusters.json / .csv
16_novelty_scores.csv
17_support_dispute_scores.csv
18_priority_review_queue.csv
19_knowledge_map_summary.md
21_exposure_outcome_matrix.csv
22_knowledge_map_points.json
```

## Run locally in live API mode

```bash
cd ~/Downloads
unzip -o smi_workbench_full_existing_plus_intelligence_v12.zip
cd smi_workbench_full_existing_plus_intelligence_v12

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

unset SMI_DEMO_MODE
unset SMI_ALLOW_DEMO_RUNS
unset SMI_SHOW_GENERATED_PDFS
unset SMI_ALLOW_GENERATED_PDF_FALLBACK
export CONNECT_API_KEY="PASTE_YOUR_REAL_KEY_HERE"
export SMI_API_TIMEOUT=300

python -m uvicorn app:app --reload --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## Optional local demo mode

Demo mode is explicit only. It will not run unless both variables are set:

```bash
export SMI_DEMO_MODE=1
export SMI_ALLOW_DEMO_RUNS=1
python -m uvicorn app:app --reload --port 8000
```

Demo mode uses generated records and does not retrieve original API paper PDFs.

## How to use Knowledge Space

1. Run a normal SMI workflow.
2. The workflow generates intelligence outputs and Knowledge Space outputs after PDF download.
3. Open **Knowledge Dashboard**.
5. Open **Cluster Map** to see article clusters.
6. Open **Priority Queue** to identify articles that may be novel, conflicting, or important to review first.
7. Download all Knowledge Space outputs from **Knowledge Reports**.

## Notes

- The Knowledge Space scores are triage aids, not scientific conclusions.
- Cosine similarity and clustering depend on extracted fields and accepted variables.
- PCA is used for visualization; clustering is based on the higher-dimensional vectors.
- Human review remains required for final scientific interpretation.


## v13 limit regression fix

This version keeps the v12 Knowledge Space features but restores/strengthens the v11 hard-limit behavior. The search limit is now also passed to the deduplication endpoint using common API aliases (`limit`, `max_results`, `max_records`, and `number_of_records`). If the live API still times out during deduplication for a limited exploratory run, the workflow transparently records a limited passthrough result and continues with the same cap enforced during AI screening, retrieval, PDF download, intelligence extraction, and Knowledge Space generation.

To force a deduplication timeout to fail instead of using passthrough, set:

```bash
export SMI_DEDUP_TIMEOUT_PASSTHROUGH=0
```

## v14 PDF citation mapping fix

This version fixes citation-table PDF mapping. The app no longer assigns individual PDFs to citation rows by row order. It now links an individual PDF only when the file/API metadata confidently matches the record by PMID, PMCID, DOI, or strong title overlap. If the API returns a ZIP package but an individual article PDF cannot be mapped, the row shows the package download only instead of a misleading paper-specific PDF.

## v16 association-table cleanup

This version tightens the exposure-health association extraction table.

Changes:

- Reduces cartesian-product row inflation from one paper.
- Creates association rows only when the same sentence or title contains an exposure, outcome, and relationship language.
- Prevents false extraction of `lead` from words such as `leading`.
- Treats pregnancy as an exposure window/population context unless the text explicitly says pregnancy outcomes.
- Repairs common PM2.5/NO2/O3/SO2 text-extraction artifacts.
- Avoids invalid effect estimates such as `or 1999`.
- Uses more conservative confidence labels: title-only/inferred rows are low confidence; sentence-supported rows are medium; rows with clear effect estimates can be high.
- Improves study-design labeling for perspective/review articles and cohort papers.


## v16 PDF batching fix

This version keeps the v15 association-table fixes and adds robust PDF batching. When the PDF limit is larger than the API can comfortably handle in one request, the workflow splits the PDF-download step into smaller batches. The default batch size is 100 and can be changed with `SMI_PDF_BATCH_SIZE`. This means a request for 25 PDFs is processed as 100-sized batches instead of one large PDF API call. If one batch fails, later batches still continue, and the run records partial success rather than losing all downloaded PDFs.

Recommended live settings:

```bash
export CONNECT_API_KEY="your-real-key"
export SMI_API_TIMEOUT=300
export SMI_PDF_BATCH_SIZE=100
```

## v17 update: PDF batch size 100

The default live API PDF batch size is now **100 PDFs per batch**. You can still override it before starting the app:

```bash
export SMI_PDF_BATCH_SIZE=100
```

For example, a PDF limit of 1,000 will be processed as 10 batches of 100 instead of 100 batches of 10.


## v18 citation/PDF QA fixes

- Renames package-only rows to: `PDF package available; individual article PDF not mapped`.
- Keeps preview/download buttons only for confidently matched individual PDFs.
- Adds `pdf_match_type` and `scope_warning` to the citation CSV.
- Adds a QA flag column to the Outputs / PDFs page for obvious search-noise records.
- Improves PM2.5 cleanup for titles where API text separated `PM` and trailing `2.5` / `2 .5`.


## v19 citation/PDF table clarification

This version makes the PDF table stricter:

- Run-level ZIP packages are shown once above the citation table.
- Package ZIPs are no longer repeated as row-level PDF files.
- Package-only rows show "No article-specific PDF mapped; run-level PDF package available".
- Package-only rows show "No row-level PDF" in the actions column.
- Row-level Preview/Download buttons are shown only for confidently matched individual PDFs.
- Citation CSV leaves row-level PDF download fields blank for package-only rows while preserving `pdf_package_url`.

This avoids the misleading appearance that the same ZIP file is the verified PDF for every article row.

## New in v20

- Association extraction now includes `sample_size`, `population_size`, and `statistical_methods` columns.
- A new article-level methods file is generated for every run:
  - `23_article_methods.json`
  - `23_article_methods.csv`
- Knowledge Space now uses abstract-like text only for vectors, clustering, similarity, novelty, and priority scores. Titles are kept only as display labels.
- Knowledge vectors now include article-level sample size, population size, and statistical method metadata when extractable from abstracts.
- Auto-variable suggestions now use a larger stop-word filter and clean noisy phrases before suggesting variables.
- The Knowledge Space summary reports that the text source is `abstract_only`.

## v21 Knowledge Space scale update

This version focuses on large-scale Knowledge Space workflows for approximately 20k-100k literature records.

Changes:

- Literature search is unbounded by default. The search limit field was removed from the web form because several API deployments ignore that value. Use downstream screening, retrieval, and PDF limits to control workload.
- Knowledge Space remains abstract-only. Titles are display labels, not vector text.
- Added UMAP coordinates and downloadable `24_umap_points.json/.csv`.
- Added a projection selector on the Knowledge Map: UMAP or PCA.
- Added topic filtering on the Knowledge Map. For example, search `heat`, `heat asthma`, `PFAS`, or `PM2.5 cardiovascular` to view only that knowledge subspace.
- Expanded stopword removal for including transition words such as however, although, therefore, whereas, nevertheless, furthermore, respectively, and other manuscript boilerplate.
- Added large-scale safeguards. For large runs, the app avoids creating a full all-pairs similarity matrix and uses scalable clustering instead.

Optional environment controls:

```bash
export SMI_KNOWLEDGE_FULL_PAIRWISE_MAX=3000   # full pairwise similarity only up to this many records
export SMI_UMAP_MAX_RECORDS=100000            # skip UMAP above this many records
export SMI_KNOWLEDGE_MAX_CLUSTERS=120         # upper bound for large-run clusters
export SMI_AUTO_VARIABLE_MIN_COUNT=3          # corpus phrase count needed for auto-add
export SMI_AUTO_VARIABLE_MAX=500              # max variable suggestions to keep
```


## v22 install stability update

v22 removes `scikit-learn` and `umap-learn` from the base `requirements.txt` so macOS installs do not fail while building SciPy from source. The app still starts and Knowledge Space still runs with built-in PCA/fallback projections. Advanced UMAP is optional; install them with `requirements_knowledge_optional.txt` or conda after the base app is working.

## v23 large-scale PDF acquisition update

v23 keeps the abstract-only Knowledge Space workflow and strengthens large-scale PDF acquisition.

For 20k-100k literature searches, Knowledge Space does not require PDFs. Use PDFs only for the subset that needs full-text review. When PDFs are requested, v23 downloads them as batch/checkpoint artifacts rather than one huge package.

New behavior:

- Batch PDF requests are checkpointed after every batch.
- Failed large PDF batches are retried.
- Failed large batches are automatically split into smaller sub-batches down to `SMI_PDF_MIN_BATCH_SIZE`.
- The app writes `05_pdf_download_manifest.json`, `05_pdf_download_manifest.csv`, and `05_pdf_download_failures.csv`.
- The Outputs page lists every batch ZIP under **Large-scale PDF packages** instead of exposing only one package.

Recommended large-scale settings:

```bash
export SMI_API_TIMEOUT=900
export SMI_PDF_BATCH_SIZE=100
export SMI_PDF_MIN_BATCH_SIZE=10
export SMI_PDF_BATCH_RETRIES=2
export SMI_PDF_BATCH_RETRY_SLEEP_SECONDS=5
```

For Knowledge Space-only runs, disable or limit PDF download; abstracts are sufficient for the Knowledge Space map.


## v24 large-scale Knowledge Space safety update

Large 20k-100k discovery runs should use abstract-only Knowledge Space first and avoid downloading PDFs for every record. The New Run page now includes a checked-by-default option: `Skip PDF download for Knowledge Space / abstract-only large search`. When this is checked, the workflow writes a skipped PDF artifact, bypasses the post-PDF approval gate, and proceeds to intelligence extraction and Knowledge Space generation. PDF acquisition can still be run later on a smaller, screened subset.


## v25 PDF reliability update

This version changes PDF acquisition from large 100-reference requests to safer small requests by default.

- Default `SMI_PDF_BATCH_SIZE` is now `10`, not `100`. The PDF download limit is still the total number of PDFs to attempt.
- Default `SMI_PDF_MIN_BATCH_SIZE` is now `1`, so failed batches can split down to one article at a time.
- The workflow sends compact PDF reference payloads by default: PMID, PMCID, DOI, title, journal, and year. This avoids failures caused by posting 100 full retrieved records with long abstracts/nested metadata to `/pdf-download`.
- Set `SMI_PDF_FORCE_SINGLE=1` to request one article at a time when the API cannot handle batches reliably.
- Set `SMI_PDF_SEND_FULL_REFERENCES=1` only if your API deployment requires complete retrieved-record objects for PDF download.

Recommended reliable PDF settings for a total request of 100 PDFs:

```bash
export SMI_API_TIMEOUT=900
export SMI_PDF_BATCH_SIZE=10
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_BATCH_RETRIES=2
export SMI_PDF_BATCH_RETRY_SLEEP_SECONDS=5
# Optional most-conservative mode:
# export SMI_PDF_FORCE_SINGLE=1
```

For large Knowledge Space runs, keep PDF download skipped during discovery, then run PDF acquisition on a smaller screened/filter subset.

## v26 PDF-limit UI correction

This release corrects a confusing v24/v25 behavior: the New Run page checked
"Skip PDF download for Knowledge Space / abstract-only large search" by default
and disabled the PDF limit field. In v26:

- PDF download is not skipped by default.
- The PDF limit field always remains editable.
- The skip-PDF checkbox only controls whether the PDF stage is enabled when the
  run starts.
- PDF batch defaults remain conservative: total PDF limit is separate from API
  batch size.

For reliable PDF acquisition, start with PDF limit 25-100 and keep:

```bash
export SMI_PDF_BATCH_SIZE=10
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_BATCH_RETRIES=2
```

For maximum reliability, one article at a time:

```bash
export SMI_PDF_FORCE_SINGLE=1
```

## v27 - Large-scale AI screening / Knowledge Space stability

v27 addresses failures that occurred before record retrieval when running 1,000-record large-scale tests.

Changes:

- The literature search remains unbounded.
- The AI screening limit is now treated as the downstream working-set limit.
- That same working-set limit is passed to deduplication.
- The `/ai-screening` request now sends common limit aliases: `limit`, `max_results`, `max_records`, and `number_of_records`.
- The New Run page adds `Skip AI screening for abstract-only Knowledge Space`.
- If AI screening fails or times out, the workflow can pass the deduplicated/search reference set to record retrieval so abstract-only Knowledge Space can continue.
- Passthrough behavior can be disabled with `SMI_AI_SCREEN_TIMEOUT_PASSTHROUGH=0`.

Recommended large-scale Knowledge Space settings:

```bash
export SMI_API_TIMEOUT=900
export SMI_AI_SCREEN_TIMEOUT_PASSTHROUGH=1
export SMI_KNOWLEDGE_FULL_PAIRWISE_MAX=3000
export SMI_UMAP_MAX_RECORDS=100000
export SMI_KNOWLEDGE_MAX_CLUSTERS=120
```

For the New Run page:

- AI screening / working-set limit: 1000
- Record retrieval limit: 1000
- Skip PDF download: checked for Knowledge Space tests
- If the live AI-screening endpoint still fails, check `Skip AI screening for abstract-only Knowledge Space`.

## v28 strict all-steps large-scale mode

v28 removes the UI skip path for AI screening/PDF download and makes the workflow strict again.

Key behavior:

- No step is skipped from the New Run page.
- The AI screening / working-set limit is applied at literature search, deduplication, AI screening, record retrieval, PDF download, intelligence extraction, and Knowledge Space.
- This prevents the API from creating an unbounded search set and then failing at AI screening for a requested 1,000-record run.
- Deduplication and AI-screening passthrough are disabled by default. If a live API step fails, the run fails clearly at that step.
- PDF acquisition remains batch/checkpoint based.

Recommended first all-step large-scale test:

```bash
export SMI_API_TIMEOUT=900
export SMI_PDF_BATCH_SIZE=10
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_BATCH_RETRIES=2
```

Then run:

- AI screening / working-set limit: 1000
- Record retrieval limit: 1000
- PDF download limit: 100 or 1000 depending on available time

For 20k-100k all-step runs, the live API endpoints must be able to process that working-set size. If an endpoint times out, reduce the working-set size or run multiple batches.

## v30 large-scale stability update

v30 is based on the v28 rollback baseline and adds two fixes without skipping workflow stages.

### 1. Chunked AI screening

The workflow still runs every stage: literature search, deduplication, AI screening, record retrieval, PDF download, intelligence extraction, Knowledge Space, summary, and archive. For large runs, AI screening is now split into smaller chunks instead of sending one 1,000+ record request to the AI-screening API.

Default settings:

```bash
export SMI_AI_SCREEN_CHUNKING=1
export SMI_AI_SCREEN_CHUNK_SIZE=100
export SMI_AI_SCREEN_MIN_CHUNK_SIZE=25
export SMI_AI_SCREEN_RETRIES=1
export SMI_AI_SCREEN_RETRY_SLEEP_SECONDS=5
```

If a chunk fails, the workflow retries it and then splits it into smaller chunks down to the minimum chunk size. If a minimum-size chunk still fails, the run fails clearly. No AI screening step is skipped.

### 2. Safer row-level PDF mapping from ZIP packages

The PDF stage still downloads API-provided original PDFs and ZIP packages. v30 improves row-level mapping when a ZIP is produced from a single requested article. In that case, the extracted PDF is tagged with that article's PMID, PMCID, DOI, and title so the citation table can map it to the correct row.

For the safest row-level mapping from ZIP packages, run PDF acquisition one article at a time:

```bash
export SMI_PDF_BATCH_SIZE=1
# or
export SMI_PDF_FORCE_SINGLE=1
```

Multi-record ZIPs are not mapped by row order by default because order-based mapping can create wrong PDF links. To allow order mapping only when a ZIP has the same number of PDFs as records in the batch, set:

```bash
export SMI_PDF_ALLOW_BATCH_ORDER_MAPPING=1
```

Use that only if you trust the API ZIP order.

## v31 chemical watchlist validation update

v31 keeps the v30 chunked AI-screening and PDF mapping behavior, and tightens the Chemical Watchlist extractor.

Changes:

- Removes the title-word fallback that caused ordinary words such as Targeted, Science, Colour, Itching, Consensus, Association, and Japan to appear as chemicals.
- Rejects likely gene/pathway terms such as JAK2-STAT3 unless they are explicitly whitelisted chemicals.
- Extracts validated chemical names/classes from a curated dictionary and aliases, including GenX, PFAS, PFOS, PFBS, PFHxS, PFOA, F-53B / 6:2 Cl-PFESA, TBBPA, BPA, phthalates, antimicrobial chemicals, PAHs, and selected metals/metal oxides.
- Suppresses generic PFAS when a more specific PFAS is present in the same record.
- Requires a chemical plus exposure, market, replacement, hazard, product-additive, CASRN, or DTXSID signal before writing a watchlist row.
- Makes priority scores run only after chemical validation.
- Keeps CASRN and DTXSID fields, but no longer invents chemical names when only generic title words are present.

This makes 11_chemical_watchlist.csv more conservative and reviewable for toxicity-testing prioritization.


## v32 update

- Fixes dashboard/statistics display where successful AI screening could show 0 records when the API returned screening IDs or chunk results without embedded record lists.
- Adds processed_record_count and screened_record_count to AI screening outputs.
- Keeps v31 Chemical Watchlist validation fixes and v30 chunked AI screening / PDF ZIP mapping behavior.


## v33 traceability update

v33 adds source-paper traceability for extracted intelligence rows:

- Every intelligence module table now shows a Source paper column.
- Every extracted row has an Open Evidence Pack action.
- Evidence Pack pages show title, authors, PMID/DOI/PMCID, journal/date, abstract, PDF status, and the selected extracted row.
- Evidence Pack pages also list all other extracted facts from the same source article across Associations, Research Datasets, Geospatial Data, Cohorts, and Chemical Watchlist.
- The Citation/PDF table now includes an Open Evidence Pack action for each paper.
- The Review Queue now includes an Open Evidence Pack action for each pending/accepted/rejected item.

This is intended to make chemical, dataset, cohort, and exposure-outcome extractions immediately traceable back to the original paper for reviewer verification.

## v34 update - sample size and population size visibility

This version improves Evidence Pack / association table methods fields:

- Association rows now show clearer column labels, including **Sample Size**, **Population Size**, and **Statistical Methods**.
- Evidence Pack selected rows also show human-readable field names instead of raw snake_case keys.
- Sample-size extraction now handles phrases such as:
  - `using 627,828 ACS cases`
  - `351,661 participants`
  - `among 798 children`
  - `0.5 million Chinese adults`
  - `44,874 births were analyzed`
- Population-size extraction is kept separate from sample size. It is used only when the abstract reports a larger source, eligible, catchment, database, registry, or underlying population. If that larger denominator is not reported, the value is shown as `not reported` rather than silently hidden or copied from sample size.
- The article-level methods table (`23_article_methods.csv/json`) uses the same improved extraction.


## v35 update - faster Knowledge Space by removing t-SNE

This version keeps the v34 Evidence Pack, sample-size, population-size, chemical-watchlist, chunked AI-screening, and PDF-mapping behavior, but removes t-SNE from the Knowledge Space workflow.

Changes:

- Knowledge Map projection choices are now **UMAP** and **PCA** only.
- Knowledge Space no longer imports or runs t-SNE.
- The workflow no longer writes `25_tsne_points.json` or `25_tsne_points.csv`.
- `SMI_TSNE_MAX_RECORDS` is no longer used.
- UMAP remains the preferred map for large-scale exploration.
- PCA remains the fastest fallback when optional UMAP dependencies are not installed.

This should make Knowledge Space generation faster and avoid slow sampled t-SNE calculations during larger runs.

## v36 performance update

v36 keeps the v35 Knowledge Space change that removed t-SNE and adds conservative optional parallel processing for the slowest stages.

New environment variables:

```bash
export SMI_AI_SCREEN_PARALLEL=1
export SMI_AI_SCREEN_MAX_WORKERS=2
export SMI_PDF_PARALLEL=1
export SMI_PDF_MAX_WORKERS=2
```

Defaults are conservative: two workers, capped internally at four workers. This is intentional because the upstream API can timeout if overloaded.

Recommended balanced settings:

```bash
export SMI_AI_SCREEN_CHUNKING=1
export SMI_AI_SCREEN_CHUNK_SIZE=250
export SMI_AI_SCREEN_MIN_CHUNK_SIZE=50
export SMI_AI_SCREEN_RETRIES=1
export SMI_AI_SCREEN_RETRY_SLEEP_SECONDS=2
export SMI_AI_SCREEN_PARALLEL=1
export SMI_AI_SCREEN_MAX_WORKERS=2

export SMI_PDF_BATCH_SIZE=5
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_BATCH_RETRIES=1
export SMI_PDF_BATCH_RETRY_SLEEP_SECONDS=2
export SMI_PDF_PARALLEL=1
export SMI_PDF_MAX_WORKERS=2

export SMI_KNOWLEDGE_FULL_PAIRWISE_MAX=2000
export SMI_UMAP_MAX_RECORDS=50000
export SMI_KNOWLEDGE_MAX_CLUSTERS=80
```

If the API starts failing or timing out, disable parallelism first:

```bash
export SMI_AI_SCREEN_PARALLEL=0
export SMI_PDF_PARALLEL=0
```

Start without `--reload` for performance testing:

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

## PDF workflow

This build removes the extra PDF-harvest page and the PDF deferral checkbox. PDF download runs during the main workflow according to the PDF download limit.


## Row-level PDF mode

This build keeps the v37 workflow but enables row-level PDF mode by default. During the main PDF stage, the app requests one article per PDF call so the returned PDF or ZIP can be safely mapped back to that citation row. This is slower than package mode, but it avoids showing only a run-level PDF package for every row.

Recommended PDF settings:

```bash
export SMI_ROW_LEVEL_PDFS=1
export SMI_PDF_BATCH_SIZE=1
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_MAX_WORKERS=2
export SMI_API_TIMEOUT=900
```

To prioritize speed over row-level mapping, set `SMI_ROW_LEVEL_PDFS=0`, but rows may show only a run-level package.


## PDF behavior

This build keeps row-level article PDF links and also creates a run-level ZIP package from the downloaded row-level PDFs.

## Package-first row-level PDF matching update

This build keeps the v37-based workflow, EDT time display, AI screening count, Auto Variables removal, and Targeted PDFs removal. The PDF stage now uses **package-first matching** by default:

- Downloads API PDF packages/batches first.
- Keeps the run-level ZIP package available.
- Extracts PDFs from the package.
- Attempts row-level PDF mapping using PMID, PMCID, DOI, strong title overlap, PDF filename, PDF metadata, and optional first-page text probe.
- Does not assign PDFs by row order.
- Leaves rows unmapped when the match is not confident.

Recommended package-first settings:

```bash
export SMI_PDF_BATCH_SIZE=5
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_MAX_WORKERS=2
export SMI_ROW_LEVEL_PDFS=0
export SMI_PDF_TEXT_PROBE=1
```

For maximum row-level coverage, you can still force slower one-article-at-a-time PDF requests:

```bash
export SMI_ROW_LEVEL_PDFS=1
export SMI_PDF_BATCH_SIZE=1
export SMI_PDF_MIN_BATCH_SIZE=1
```


## v3 title/abstract extraction refinement

Statistical Methods now reports primary statistical models only. Supporting details such as lag windows, splines, stratified analyses, two-pollutant models, and sensitivity analyses are not appended to the main Statistical Methods field.

## Dynamic Knowledge Map and extraction cleanup

This build uses dynamic Knowledge Map topic buttons generated from the current run instead of fixed buttons. It also normalizes exposure labels, removes generic biospecimen guessing, and improves source-title propagation for cohort/extraction rows.

## v53 Knowledge Space update

- Knowledge Map dots now link directly to Evidence Packs.
- Knowledge Map includes a related-papers table for the current lens/filter.
- Lens cluster summaries merge duplicate labels and group small fragments.
- Dynamic topic buttons remain limited to up to 5 and are generated from the current run.



## v55 Knowledge Map Pagination

Related papers below the Knowledge Map are now paginated for speed. Use Show papers for a cluster, then Next/Previous to page through the results. Clear cluster selection resets the view.
