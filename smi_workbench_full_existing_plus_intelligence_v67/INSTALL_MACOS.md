# macOS installation notes

## Base app, safest install

```bash
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

This installs the app without forcing SciPy/scikit-learn/UMAP builds. Knowledge Space still works with the built-in PCA/fallback projection. UMAP will show as not installed until optional dependencies are installed.

## Optional advanced Knowledge Space dependencies

Use Python 3.11 or 3.12 when possible. Then run:

```bash
python -m pip install --upgrade pip setuptools wheel
python -m pip install --only-binary=:all: scipy scikit-learn umap-learn
```

If that command says no matching distribution, do not remove `--only-binary`. It means wheels are not available for your Python/macOS combination. Use Python 3.12 or a conda environment instead.

## Conda option for UMAP

```bash
conda create -n smi-workbench python=3.12 -y
conda activate smi-workbench
python -m pip install -r requirements.txt
conda install -c conda-forge scipy scikit-learn umap-learn -y
```

## Large-scale PDF settings

Set these before starting uvicorn:

```bash
export SMI_API_TIMEOUT=900
export SMI_PDF_BATCH_SIZE=100
export SMI_PDF_MIN_BATCH_SIZE=10
export SMI_PDF_BATCH_RETRIES=2
export SMI_PDF_BATCH_RETRY_SLEEP_SECONDS=5
```

If a 100-reference PDF batch fails, v23 retries it and then splits it into smaller sub-batches automatically. The Outputs page shows each saved PDF package ZIP separately.

For 20k-100k search results, keep Knowledge Space abstract-only and download PDFs only for a screened subset. Full PDF acquisition at that scale may take a long time and can be limited by the upstream PDF service.

## Recommended v30 large-scale settings

For a 1,000-record all-step test:

```bash
export CONNECT_API_KEY="your-real-key"
export SMI_API_TIMEOUT=900

export SMI_AI_SCREEN_CHUNKING=1
export SMI_AI_SCREEN_CHUNK_SIZE=100
export SMI_AI_SCREEN_MIN_CHUNK_SIZE=25
export SMI_AI_SCREEN_RETRIES=1
export SMI_AI_SCREEN_RETRY_SLEEP_SECONDS=5

export SMI_PDF_BATCH_SIZE=1
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_BATCH_RETRIES=2
export SMI_PDF_BATCH_RETRY_SLEEP_SECONDS=5

python -m uvicorn app:app --reload --port 8000
```

`SMI_PDF_BATCH_SIZE=1` is slower, but gives the best row-level mapping when the API returns PDFs as ZIP packages.


## v35 note

v35 removes t-SNE from the app. You no longer need to set `SMI_TSNE_MAX_RECORDS`. Use UMAP for exploratory maps and PCA as the fastest fallback.

## v36 faster run settings

After activating the virtual environment and installing requirements, use this balanced performance block before starting the app:

```bash
export CONNECT_API_KEY="your-real-key"
export SMI_API_TIMEOUT=900

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

python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Do not use `--reload` for speed testing. If the upstream API becomes unstable, set `SMI_AI_SCREEN_PARALLEL=0` and `SMI_PDF_PARALLEL=0`.

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

## PDF package-first row-level matching

Default for this build:

```bash
export SMI_PDF_BATCH_SIZE=5
export SMI_PDF_MIN_BATCH_SIZE=1
export SMI_PDF_MAX_WORKERS=2
export SMI_ROW_LEVEL_PDFS=0
export SMI_PDF_TEXT_PROBE=1
```

This keeps the run-level PDF package and maps row-level PDFs only when the match is confident. Use `SMI_ROW_LEVEL_PDFS=1` only when you need slower one-article-at-a-time downloads.
