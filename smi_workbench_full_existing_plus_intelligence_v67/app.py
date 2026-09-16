from __future__ import annotations

import csv
import datetime as dt
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import io
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

import yaml
from smi_intelligence import (
    DEFAULT_MODULES,
    MODULE_SPECS,
    SUMMARY_FILE,
    ARTICLE_METHODS_JSON,
    ARTICLE_METHODS_CSV,
    all_review_rows,
    generate_intelligence_outputs,
    module_from_short,
    read_module_rows,
    update_review_decision,
)
from smi_knowledge_space import (
    KNOWLEDGE_FILES,
    generate_knowledge_space_outputs,
    update_variable_decision,
)
from smi_validation import (
    list_validations,
    request_stop as request_validation_stop,
    resume_validation,
    start_validation,
    validation_export_file,
    validation_status,
    validation_view,
)
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_NAME = "SMI Workbench"
APP_FULL_NAME = "Scientific Market Intelligence"
BASE_DIR = Path(".").resolve()
ROOT_DIR = Path("./smi")
WORKFLOW_SCRIPT = Path("./smi_workflow.py")
CONFIG_DIR = Path("./generated_configs")

Path("static").mkdir(exist_ok=True)
Path("templates").mkdir(exist_ok=True)
Path("generated_configs").mkdir(exist_ok=True)

app = FastAPI(title="SMI Workbench")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _posit_connect_app_base_url(request: Request) -> str:
    """Return the runtime Posit Connect content base URL, or an empty string locally.

    Posit Connect proxies FastAPI content below a content-specific prefix and
    provides that prefix/URL in the ``RStudio-Connect-App-Base-URL`` request
    header.  Local uvicorn requests do not contain the header, so existing
    root-relative behavior remains unchanged.
    """
    value = str(request.headers.get("rstudio-connect-app-base-url", "") or "").strip()
    return value.rstrip("/")


def _location_already_has_app_base(location: str, app_base_url: str) -> bool:
    """Avoid adding the Connect prefix twice to an already-prefixed Location."""
    if not location or not app_base_url:
        return False
    try:
        base_path = urlsplit(app_base_url).path.rstrip("/")
    except Exception:
        base_path = ""
    return bool(base_path and (location == base_path or location.startswith(base_path + "/")))


@app.middleware("http")
async def posit_connect_base_path_redirects(request: Request, call_next):
    """Keep root-relative redirects inside the deployed Posit Connect content URL.

    Existing SMI routes intentionally remain unchanged (``/new``, ``/outputs``,
    etc.).  This middleware only adjusts a root-relative ``Location`` response
    when Connect supplies its runtime application base URL.  Local behavior is
    therefore identical to the existing app.
    """
    response = await call_next(request)
    app_base_url = _posit_connect_app_base_url(request)
    location = response.headers.get("location", "")
    if (
        app_base_url
        and location.startswith("/")
        and not location.startswith("//")
        and not _location_already_has_app_base(location, app_base_url)
    ):
        response.headers["location"] = app_base_url + location
    return response

# In-memory registry for workflow subprocesses launched from the web UI.
# This keeps the original app behavior intact while allowing users to stop
# long-running live API runs without closing the server.
ACTIVE_RUN_JOBS: Dict[str, Dict[str, Any]] = {}

# Evidence Pack pages should open quickly even for large runs.  These small
# in-process caches avoid re-reading multi-megabyte JSON outputs and rebuilding
# PDF/citation mappings every time a user opens one Evidence Pack.
_EVIDENCE_RECORD_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_EVIDENCE_MODULE_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_CITATION_TABLE_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

DISPLAY_TIMEZONE = os.environ.get("SMI_DISPLAY_TIMEZONE", "America/New_York")

def format_display_time(value: Any) -> str:
    """Format saved ISO timestamps for display only.

    Workflow files and logs are left unchanged; the app UI shows Eastern time by
    default. Non-date values are returned unchanged.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        raw = text.replace("Z", "+00:00") if text.endswith("Z") else text
        parsed = dt.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            # Naive timestamps created by the web app are local server time.
            parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
        target_tz = ZoneInfo(DISPLAY_TIMEZONE)
        return parsed.astimezone(target_tz).strftime("%b %d, %Y %I:%M:%S %p %Z")
    except Exception:
        return text


def format_status_markdown_times(text: str) -> str:
    """Display ISO timestamps inside status markdown in the UI timezone."""
    if not text:
        return ""
    iso_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?"
    return re.sub(iso_pattern, lambda m: format_display_time(m.group(0)), text)


templates.env.filters["display_time"] = format_display_time

def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "subproject"


def run_command(command: List[str]) -> Tuple[int, str, str]:
    process = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    return process.returncode, process.stdout, process.stderr

def detect_workflow_pause(output_text: str) -> dict:
    if "Paused at stage:" not in output_text:
        return {
            "paused": False,
            "stage": "",
            "message": "",
            "approval_checkpoint": "",
        }

    stage = ""

    for line in output_text.splitlines():
        if line.startswith("Paused at stage:"):
            stage = line.replace("Paused at stage:", "").strip()

    if stage == "summary":
        message = (
            "PDF download is complete. Human review is required before generating "
            "the summary. Please review the retrieved records, PDF download results, "
            "and available output files before approving."
        )
        checkpoint = "post-pdf-download"

    elif stage == "archive":
        message = (
            "The summary is complete. Final approval is required before archiving this run."
        )
        checkpoint = "final-approval"

    else:
        message = "The workflow is paused and needs human approval before continuing."
        checkpoint = "post-pdf-download"

    return {
        "paused": True,
        "stage": stage,
        "message": message,
        "approval_checkpoint": checkpoint,
    }
def read_text(path: Path, default: str = "") -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else default


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def _records_cache_signature(run_dir: Path) -> float:
    outputs = run_dir / "outputs"
    files = [
        outputs / "04_retrieved_records.json",
        outputs / "03_ai_screening.json",
        outputs / "02a_chunked_screening_input_records.json",
        outputs / "02_deduplication.json",
        outputs / "01_literature_search.json",
    ]
    return max((_mtime(path) for path in files), default=0.0)


def cached_best_records(run_dir: Path) -> List[Dict[str, Any]]:
    key = str(run_dir.resolve())
    sig = _records_cache_signature(run_dir)
    cached = _EVIDENCE_RECORD_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    records = load_records_with_best_abstracts_for_display(run_dir)
    _EVIDENCE_RECORD_CACHE[key] = (sig, records)
    return records


def cached_module_rows(run_dir: Path, module_name: str) -> List[Dict[str, Any]]:
    spec = MODULE_SPECS.get(module_name)
    if not spec:
        return []
    path = run_dir / "outputs" / spec["json"]
    sig = _mtime(path)
    key = f"{run_dir.resolve()}::{module_name}"
    cached = _EVIDENCE_MODULE_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    rows = read_module_rows(run_dir, module_name)
    _EVIDENCE_MODULE_CACHE[key] = (sig, rows)
    return rows


def evidence_pack_load_pdf_status() -> bool:
    # Loading PDF status can be slow for runs with large PDF packages because it
    # may rebuild citation/PDF matching. Keep Evidence Packs fast by default.
    return os.environ.get("SMI_EVIDENCE_PACK_LOAD_PDF_STATUS", "0") == "1"


def parse_status(status_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in status_text.splitlines():
        if line.startswith("|"):
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) == 2 and parts[0] not in ["Field", "---"] and not parts[0].startswith("---"):
                out[parts[0]] = parts[1]
    return out


def get_projects() -> List[str]:
    root = ROOT_DIR / "subprojects"
    if not root.exists():
        return []
    return sorted([p.name for p in root.iterdir() if p.is_dir()])


def get_runs(subproject: str) -> List[Path]:
    runs_dir = ROOT_DIR / "subprojects" / subproject / "runs"
    if not runs_dir.exists():
        return []
    runs = [p for p in runs_dir.iterdir() if p.is_dir()]
    return sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)


def get_selected_run(subproject: Optional[str], run_id: Optional[str]) -> Tuple[Optional[str], Optional[Path]]:
    projects = get_projects()
    selected_project = subproject or (projects[0] if projects else None)
    if not selected_project:
        return None, None
    runs = get_runs(selected_project)
    if run_id:
        candidate = ROOT_DIR / "subprojects" / selected_project / "runs" / run_id
        if candidate.exists():
            return selected_project, candidate
    return selected_project, (runs[0] if runs else None)


def output_record_count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in [
            "recordCount", "record_count", "sourceRecordCount", "source_record_count",
            "screenedRecordCount", "screened_record_count", "processedRecordCount", "processed_record_count",
            "requestedRecordCount", "requested_record_count", "attemptedRecordCount", "attempted_record_count",
            "inputRecordCount", "input_record_count", "count", "n", "total",
            "kept", "keptCount", "kept_count",
        ]:
            value = data.get(key)
            if isinstance(value, int):
                return value
        decision_counts = data.get("decisionCounts") or data.get("decision_counts")
        if isinstance(decision_counts, dict):
            return sum(v for v in decision_counts.values() if isinstance(v, int))
        for key in ["records", "results", "data", "items", "payload"]:
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict):
                nested = output_record_count(value)
                if nested:
                    return nested

        # Chunked AI screening may return only per-chunk IDs instead of embedded records.
        # In that case, show the number of records the AI-screening stage actually processed
        # rather than displaying 0.
        chunks = data.get("chunks")
        if isinstance(chunks, list):
            chunk_total = 0
            for chunk in chunks:
                chunk_total += output_record_count(chunk)
            if chunk_total:
                return chunk_total

        if data.get("chunked_ai_screening") and isinstance(data.get("applied_limit"), int):
            return data.get("applied_limit")
    return 0




def ai_screening_progress(run_dir: Optional[Path]) -> Dict[str, Any]:
    """Return a small UI-only AI screening count from existing output files.

    This does not change workflow behavior. v37 already writes
    03_ai_screening_partial.json after each completed chunk; the UI reads that
    file and displays the screened-record count.
    """
    if not run_dir:
        return {"available": False, "screened": 0, "total": 0, "percent": 0, "label": ""}
    outputs_dir = run_dir / "outputs"
    final_data = read_json(outputs_dir / "03_ai_screening.json", default=None)
    partial_data = read_json(outputs_dir / "03_ai_screening_partial.json", default=None)

    data = final_data if isinstance(final_data, dict) else partial_data
    if not isinstance(data, dict):
        return {"available": False, "screened": 0, "total": 0, "percent": 0, "label": ""}

    total = 0
    for key in ["requested_record_count", "processed_record_count", "screened_record_count", "applied_limit", "input_record_count"]:
        value = data.get(key)
        if isinstance(value, int) and value > 0:
            total = value
            break

    screened = 0
    chunks = data.get("chunks")
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            for key in ["requested_reference_count", "chunk_size", "screened_record_count", "processed_record_count", "recordCount", "record_count"]:
                value = chunk.get(key)
                if isinstance(value, int) and value > 0:
                    screened += value
                    break
    if screened <= 0:
        for key in ["screened_record_count", "processed_record_count"]:
            value = data.get(key)
            if isinstance(value, int) and value > 0:
                screened = value
                break

    if total <= 0:
        total = screened
    if total > 0:
        screened = min(screened, total) if screened else 0
        percent = int(round((screened / total) * 100))
    else:
        percent = 0

    return {
        "available": total > 0 or screened > 0,
        "screened": screened,
        "total": total,
        "percent": max(0, min(100, percent)),
        "label": f"{screened:,} / {total:,}" if total else f"{screened:,}",
    }

def count_json_file(run_dir: Path, file_name: str) -> int:
    return output_record_count(read_json(run_dir / "outputs" / file_name, default=None))


def pdf_download_progress(run_dir: Optional[Path]) -> Dict[str, Any]:
    """Return a UI-only PDF download count from the checkpoint manifest.

    The workflow writes 05_pdf_download_manifest.json after each completed PDF
    batch. This function reads that file so the status page can show progress
    without changing the PDF download logic.
    """
    if not run_dir:
        return {"available": False, "processed": 0, "total": 0, "percent": 0, "label": ""}

    outputs_dir = run_dir / "outputs"
    manifest = read_json(outputs_dir / "05_pdf_download_manifest.json", default=None)
    final_data = read_json(outputs_dir / "05_pdf_download.json", default=None)

    data = manifest if isinstance(manifest, dict) else final_data
    if not isinstance(data, dict) or data.get("enabled") is False:
        return {"available": False, "processed": 0, "total": 0, "percent": 0, "label": ""}

    total = 0
    for key in ["requested_reference_count", "requestedRecordCount", "reference_count", "total_references"]:
        value = data.get(key)
        if isinstance(value, int) and value > 0:
            total = value
            break

    processed = 0
    for key in ["completed_reference_count", "processed_reference_count", "processedRecordCount"]:
        value = data.get(key)
        if isinstance(value, int) and value > 0:
            processed = value
            break

    # Final 05_pdf_download.json may not have completed_reference_count in older
    # runs. If the stage completed, use requested_reference_count as processed.
    if processed <= 0 and isinstance(final_data, dict):
        status = str(final_data.get("status", "")).lower()
        if status in {"success", "partial_success", "unavailable"}:
            value = final_data.get("requested_reference_count")
            if isinstance(value, int) and value > 0:
                processed = value
                total = total or value

    if total <= 0:
        total = processed

    if total > 0:
        processed = min(processed, total) if processed else 0
        percent = int(round((processed / total) * 100))
    else:
        percent = 0

    pdf_files = 0
    for source in [manifest, final_data]:
        if isinstance(source, dict):
            value = source.get("successful_pdf_count")
            if isinstance(value, int) and value > pdf_files:
                pdf_files = value

    packages = 0
    for source in [manifest, final_data]:
        if isinstance(source, dict):
            value = source.get("package_zip_count")
            if isinstance(value, int) and value > packages:
                packages = value

    failed = 0
    for source in [manifest, final_data]:
        if isinstance(source, dict):
            value = source.get("failed_batch_count")
            if isinstance(value, int) and value > failed:
                failed = value

    details = []
    if pdf_files:
        details.append(f"PDF files matched/found: {pdf_files:,}")
    if packages:
        details.append(f"packages: {packages:,}")
    if failed:
        details.append(f"failed batches: {failed:,}")

    return {
        "available": total > 0 or processed > 0,
        "processed": processed,
        "total": total,
        "percent": max(0, min(100, percent)),
        "label": f"{processed:,} / {total:,}" if total else f"{processed:,}",
        "details": "; ".join(details),
    }


def count_pdf_downloads(data: Any) -> int:
    if isinstance(data, list):
        return sum(count_pdf_downloads(x) for x in data)
    if isinstance(data, dict):
        count = 0
        status = str(data.get("status", "")).lower()
        if status in {"downloaded", "success", "saved"}:
            count += 1
        if data.get("fileName") or data.get("downloadUrl") or data.get("localZipPath"):
            count = max(count, 1)
        for value in data.values():
            if isinstance(value, (list, dict)):
                count += count_pdf_downloads(value)
        return count
    return 0

def safe_path_from_relative(relative_path: str) -> Path:
    file_path = (BASE_DIR / relative_path).resolve()

    if BASE_DIR.resolve() not in file_path.parents and file_path != BASE_DIR.resolve():
        raise ValueError("File path is outside the allowed project directory.")

    if not file_path.exists():
        raise FileNotFoundError("File does not exist.")

    return file_path


def pdf_package_links_for_run(run_dir: Path, pdf_response: Any) -> List[Dict[str, Any]]:
    """Return every locally saved PDF package ZIP for large-scale PDF runs."""
    links: List[Dict[str, Any]] = []
    seen = set()

    def add_path(path_value: Any, label: str = "") -> None:
        if not path_value:
            return
        path = Path(str(path_value))
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        if not path.exists() or path.suffix.lower() != ".zip":
            return
        try:
            rel = str(path.resolve().relative_to(BASE_DIR))
        except Exception:
            return
        if rel in seen:
            return
        seen.add(rel)
        links.append({
            "name": path.name,
            "label": label or path.name,
            "size": f"{path.stat().st_size:,} bytes",
            "url": "/download?" + urlencode({"path": rel}),
        })

    if isinstance(pdf_response, dict):
        for item in pdf_response.get("packageZips") or []:
            if isinstance(item, dict):
                add_path(item.get("localZipPath") or item.get("relativePath"), item.get("batchLabel", ""))
        for batch in pdf_response.get("batch_results") or []:
            if isinstance(batch, dict):
                add_path(batch.get("localZipPath"), batch.get("batch_label", ""))
        add_path(pdf_response.get("localZipPath"), "Run-level PDF package")

    # Fallback discovery for older runs or partially checkpointed runs.
    pdfs_dir = run_dir / "outputs" / "pdfs"
    if pdfs_dir.exists():
        for zip_path in sorted(pdfs_dir.glob("*.zip")):
            add_path(zip_path, zip_path.name)

    return links

def run_summary(subproject: str, run_dir: Path) -> Dict[str, Any]:
    status_data = parse_status(read_text(run_dir / "status.md"))
    metadata = read_json(run_dir / "metadata.json", default={}) or {}
    pdf_data = read_json(run_dir / "outputs" / "05_pdf_download.json", default={})
    return {
        "subproject": subproject,
        "run_id": run_dir.name,
        "state": status_data.get("State", metadata.get("state", "unknown")),
        "current_step": status_data.get("Current Step", "unknown"),
        "created_at": format_display_time(metadata.get("created_at", "")),
        "search_records": count_json_file(run_dir, "01_literature_search.json"),
        "dedup_records": count_json_file(run_dir, "02_deduplication.json"),
        "screened_records": count_json_file(run_dir, "03_ai_screening.json"),
        "retrieved_records": count_json_file(run_dir, "04_retrieved_records.json"),
        "pdf_downloads": count_pdf_downloads(pdf_data),
    }


def extract_records_from_response(response):
    if isinstance(response, list):
        return [x for x in response if isinstance(x, dict)]

    if not isinstance(response, dict):
        return []

    for key in ["records", "results", "data", "items", "references"]:
        value = response.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    payload = response.get("payload")

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ["records", "results", "data", "items", "references"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

    return []

def get_record_value(record: dict, keys: list[str], default: str = "") -> str:
    for key in keys:
        value = record.get(key)
        if value:
            if isinstance(value, list):
                return "; ".join(str(v) for v in value)
            return str(value)

    lower_map = {str(k).lower(): v for k, v in record.items()}

    for key in keys:
        value = lower_map.get(key.lower())
        if value:
            if isinstance(value, list):
                return "; ".join(str(v) for v in value)
            return str(value)

    return default

def allow_generated_pdf_previews() -> bool:
    """Only show generated/demo PDFs when explicitly requested.

    The default UI policy is to show original API PDFs only. This prevents
    generated summary PDFs from being confused with actual papers.
    """
    return os.environ.get("SMI_SHOW_GENERATED_PDFS") == "1" or os.environ.get("SMI_ALLOW_GENERATED_PDF_FALLBACK") == "1"


def is_original_pdf_entry(entry: dict) -> bool:
    source = str(entry.get("source", "")).lower()
    return bool(entry.get("is_original_pdf")) or source.startswith("original_api_pdf") or source in {"api_original_pdf_download", "original_pdf", "original_api_pdf_direct"}


def clean_title_text(title: str) -> str:
    """Clean small citation-title artifacts without changing scientific meaning.

    PubMed/API text sometimes separates PM2.5 into "PM" plus a stray
    trailing "2.5" or "2 .5". This normalizes the common patterns seen in
    citation tables while avoiding broad title rewrites.
    """
    title = str(title or "").strip()
    title = re.sub(r"\s+", " ", title)

    # Normalize direct forms first.
    title = re.sub(r"\bPM\s*2\s*\.\s*5\b", "PM2.5", title, flags=re.I)

    # Normalize a stray final "2.5" that clearly belongs to the nearest PM mention.
    trailing_pm25 = re.search(r"(?:\s+2\s*\.\s*5)\.?$", title)
    if trailing_pm25 and re.search(r"\bPM\b", title, flags=re.I):
        title = re.sub(r"(?:\s+2\s*\.\s*5)\.?$", "", title).strip()
        title = re.sub(r"\bPM\b", "PM2.5", title, count=1, flags=re.I)

    # Common phrase-level cleanup.
    title = re.sub(r"\bPM exposure\b", "PM2.5 exposure", title, flags=re.I)
    title = re.sub(r"\bPM concentration\b", "PM2.5 concentration", title, flags=re.I)
    title = re.sub(r"\bPM components\b", "PM2.5 components", title, flags=re.I)
    title = re.sub(r"\bPM is associated\b", "PM2.5 is associated", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def app_abstract_text(record: Dict[str, Any]) -> str:
    """Display the longest abstract-like field; do not use PDF/body text."""
    if not record:
        return ""
    def flat(v):
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return " ".join(flat(x) for x in v if x is not None)
        if isinstance(v, dict):
            parts = []
            for key, val in v.items():
                kl = str(key).lower()
                if any(skip in kl for skip in ["pdf", "fulltext", "full_text", "body", "html", "xml"]):
                    continue
                part = flat(val)
                if part:
                    parts.append(part)
            return " ".join(parts)
        return str(v)
    candidates = []
    for key, value in record.items():
        kl = str(key).lower()
        if any(skip in kl for skip in ["pdf", "fulltext", "full_text", "body", "html", "xml"]):
            continue
        if kl in {"abstract", "abstract_text", "abstracttext", "summary", "snippet", "description", "article_abstract", "pubmed_abstract"} or "abstract" in kl:
            txt = re.sub(r"\s+", " ", flat(value)).strip()
            if txt:
                candidates.append(txt)
    for key in ["abstract", "Abstract", "summary", "snippet", "description", "article_abstract", "pubmed_abstract"]:
        txt = re.sub(r"\s+", " ", get_record_value(record, [key])).strip()
        if txt:
            candidates.append(txt)
    seen=[]
    for c in candidates:
        if c not in seen:
            seen.append(c)
    return max(seen, key=len) if seen else ""


def _app_normalize_article_doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi\s*:\s*", "", text)
    return text.rstrip(".;,) ")


def _app_normalize_article_title(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _app_record_match_keys(record: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    pmid = get_record_value(record, ["pmid", "PMID"])
    if pmid:
        digits = re.sub(r"\D", "", str(pmid))
        if digits:
            keys.append(f"pmid:{digits}")
    pmcid = get_record_value(record, ["pmcid", "PMCID"])
    if pmcid:
        normalized = str(pmcid).upper().replace("PMCID:", "").strip()
        if normalized:
            keys.append(f"pmcid:{normalized}")
    doi = _app_normalize_article_doi(get_record_value(record, ["doi", "DOI", "article_doi"]))
    if doi:
        keys.append(f"doi:{doi}")
    title = _app_normalize_article_title(get_record_value(record, ["title", "article_title", "Title", "name"]))
    if len(title) >= 24:
        keys.append(f"title:{title}")
    return keys


def load_records_with_best_abstracts_for_display(run_dir: Path) -> List[Dict[str, Any]]:
    """Preserve retrieved records/PDF identifiers, enriching only displayed abstract text."""
    outputs = run_dir / "outputs"
    record_sets: Dict[str, List[Dict[str, Any]]] = {}
    for filename in [
        "04_retrieved_records.json",
        "02a_chunked_screening_input_records.json",
        "02_deduplication.json",
        "01_literature_search.json",
    ]:
        record_sets[filename] = extract_records_from_response(read_json(outputs / filename, default={}))

    primary = (
        record_sets["04_retrieved_records.json"]
        or record_sets["02_deduplication.json"]
        or record_sets["02a_chunked_screening_input_records.json"]
        or record_sets["01_literature_search.json"]
    )
    if not primary:
        return []

    index: Dict[str, List[Dict[str, Any]]] = {}
    for records in record_sets.values():
        for record in records:
            for key in _app_record_match_keys(record):
                index.setdefault(key, []).append(record)

    enriched: List[Dict[str, Any]] = []
    for record in primary:
        candidates: List[Dict[str, Any]] = [record]
        seen_ids = {id(record)}
        for key in _app_record_match_keys(record):
            for candidate in index.get(key, []):
                if id(candidate) not in seen_ids:
                    seen_ids.add(id(candidate))
                    candidates.append(candidate)
        best_abstract = ""
        for candidate in candidates:
            candidate_abstract = app_abstract_text(candidate)
            if len(candidate_abstract) > len(best_abstract):
                best_abstract = candidate_abstract
        item = dict(record)
        if best_abstract:
            item["_smi_full_abstract"] = best_abstract
        enriched.append(item)
    return enriched


def scope_warning_for_record(record: dict, title: str) -> str:
    """Flag obvious search-noise records for reviewer screening.

    This is intentionally conservative. It does not remove rows; it only adds a
    warning when the title suggests that the article may not be an environmental
    exposure-health article for the current review purpose.
    """
    hay = " ".join([
        str(title or ""),
        app_abstract_text(record),
        get_record_value(record, ["journal", "journal_title", "source", "publication"]),
    ]).lower()

    warning_rules = [
        ("tuberculosis" in hay and "hiv" in hay and not any(x in hay for x in ["air pollution", "environmental exposure", "exposome", "climate", "heat", "smoke", "pollution"]),
         "Potentially out of scope: infectious-disease burden/HIV record, not clearly environmental exposure-focused."),
        ("heat-processed rodent chow" in hay or "rodent chow" in hay,
         "Potentially out of scope: food-processing/animal-feed heat term, not environmental heat exposure."),
        ("embryo transfer in dairy" in hay or "dairy herds" in hay,
         "Potentially out of scope: veterinary reproduction record, not clearly environmental health exposure-focused."),
        ("passive heating interventions" in hay and not any(x in hay for x in ["climate", "environmental heat", "heat wave", "ambient temperature"]),
         "Potentially out of scope: passive-heating intervention, not environmental exposure."),
        ("poultry" in hay and "blood acid/base" in hay,
         "Potentially out of scope: poultry physiology/nutrition record."),
    ]
    for matched, message in warning_rules:
        if matched:
            return message
    return ""

def build_citation_table(run_dir: Path) -> list[dict]:
    retrieved_path = run_dir / "outputs" / "04_retrieved_records.json"
    pdf_path = run_dir / "outputs" / "05_pdf_download.json"

    retrieved_response = read_json(retrieved_path, default={})
    pdf_response = read_json(pdf_path, default={})
    records = extract_records_from_response(retrieved_response)

    allow_generated = allow_generated_pdf_previews()
    pdf_response_source = str(pdf_response.get("source", "")).lower() if isinstance(pdf_response, dict) else ""
    package_url = ""
    package_name = ""
    pdf_entries: list[dict] = []

    if isinstance(pdf_response, dict):
        package_name = pdf_response.get("fileName", "")
        local_zip_path = pdf_response.get("localZipPath", "")
        if local_zip_path:
            local_path = Path(local_zip_path)
            if not local_path.is_absolute():
                local_path = (BASE_DIR / local_path).resolve()
            if local_path.exists():
                try:
                    package_url = "/download?" + urlencode({"path": str(local_path.resolve().relative_to(BASE_DIR))})
                except ValueError:
                    package_url = ""
        if not package_url and package_name:
            possible_zip = run_dir / "outputs" / "pdfs" / package_name
            if possible_zip.exists():
                package_url = "/download?" + urlencode({"path": str(possible_zip.resolve().relative_to(BASE_DIR))})
        if not package_url:
            package_url = pdf_response.get("downloadUrl", "")
        if pdf_response_source in {"local_fallback", "generated_fallback"} and not allow_generated:
            package_url = ""

        raw_entries = pdf_response.get("pdfs") or pdf_response.get("files") or []
        if isinstance(raw_entries, list):
            for item in raw_entries:
                if isinstance(item, dict):
                    if is_original_pdf_entry(item) or allow_generated:
                        pdf_entries.append(item)

    # Also discover original individual PDFs saved under outputs/pdfs/api_original_pdfs.
    # Do not auto-discover generated demo preview PDFs unless explicitly allowed.
    pdf_dir = run_dir / "outputs" / "pdfs"
    if pdf_dir.exists():
        known = {str(x.get("localPath") or x.get("path") or x.get("file_name") or x.get("fileName")) for x in pdf_entries}
        search_roots = [pdf_dir / "api_original_pdfs"]
        if allow_generated:
            search_roots.append(pdf_dir)
        for root in search_roots:
            if not root.exists():
                continue
            for file in sorted(root.glob("*.pdf")):
                if str(file) not in known and file.name not in known:
                    pdf_entries.append({"fileName": file.name, "localPath": str(file), "source": "original_api_pdf_discovered" if "api_original_pdfs" in str(file) else "generated_preview", "is_original_pdf": "api_original_pdfs" in str(file)})

    def entry_url(entry: dict, mode: str) -> str:
        raw = entry.get("localPath") or entry.get("path") or entry.get("filePath") or ""
        if not raw:
            return ""
        path = Path(raw)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        if not path.exists():
            return ""
        try:
            rel = path.resolve().relative_to(BASE_DIR)
        except ValueError:
            return ""
        endpoint = "/pdf-viewer" if mode == "preview" and path.suffix.lower() == ".pdf" else ("/preview" if mode == "preview" else "/download")
        return endpoint + "?" + urlencode({"path": str(rel)})

    def norm(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return " ".join(norm(x) for x in value)
        return re.sub(r"\s+", " ", str(value).lower()).strip()

    def entry_value(entry: dict, keys: list[str]) -> str:
        lower = {str(k).lower(): v for k, v in entry.items()}
        for key in keys:
            value = entry.get(key)
            if value is None:
                value = lower.get(key.lower())
            if value:
                return str(value)
        return ""

    used_pdf_entries: set[int] = set()

    def best_pdf_entry_for_record(record: dict, fallback_index: int) -> dict:
        """Return the original PDF that confidently matches this citation.

        Important: do not assign PDFs by row order when the API returns a mixed ZIP/package.
        Row-order fallback caused incorrect links such as PMID 42442744 displaying
        PMID-42436292.pdf. A PDF is now attached to a citation only when PMID, PMCID,
        DOI, or a strong title/file-name match is present. If no confident match exists,
        the row shows only the package link, not a misleading individual PDF.
        """
        if not pdf_entries:
            return {}
        record_doi = norm(get_record_value(record, ["doi", "DOI", "article_doi"]))
        record_pmid = norm(get_record_value(record, ["pmid", "PMID"]))
        record_pmcid = norm(get_record_value(record, ["pmcid", "PMCID"]))
        record_title = norm(get_record_value(record, ["title", "article_title", "Title"]))
        best_idx = None
        best_score = 0
        for idx, entry in enumerate(pdf_entries):
            if idx in used_pdf_entries:
                continue
            haystack = norm(" ".join(str(v) for v in entry.values() if not isinstance(v, (dict, list))))
            score = 0

            # High-confidence identifier matches.
            if record_doi and record_doi in haystack:
                score += 120
            if record_pmid and record_pmid in haystack:
                score += 110
            if record_pmcid and record_pmcid in haystack:
                score += 110

            entry_title = norm(" ".join([
                entry_value(entry, ["title", "name", "fileName", "file_name", "filename", "sourceRecordTitle"]),
                entry_value(entry, ["pdfTextProbe"])[:3000],
            ]))
            if record_title and entry_title:
                record_terms = {t for t in re.split(r"[^a-z0-9]+", record_title) if len(t) > 4}
                entry_terms = {t for t in re.split(r"[^a-z0-9]+", entry_title) if len(t) > 4}
                overlap = len(record_terms & entry_terms)
                if record_terms:
                    overlap_ratio = overlap / max(len(record_terms), 1)
                    # Title-only matching is allowed only when it is strong.
                    if overlap >= 4 or overlap_ratio >= 0.35:
                        score += min(70, overlap * 10)

            if score > best_score:
                best_score = score
                best_idx = idx

        # If there is exactly one record and one PDF, accepting the only PDF is safe.
        # For multi-record runs, require an actual identifier/strong-title match.
        if (best_idx is None or best_score < 60) and len(records) == 1 and len(pdf_entries) == 1 and 0 not in used_pdf_entries:
            best_idx = 0
            best_score = 60

        if best_idx is None or best_score < 60:
            return {}
        used_pdf_entries.add(best_idx)
        return pdf_entries[best_idx]

    rows = []
    for index, record in enumerate(records, start=1):
        citation_id = get_record_value(record, ["citation_id", "record_id", "id", "uid", "pmid", "PMID"], default=str(index))
        title = clean_title_text(get_record_value(record, ["title", "article_title", "Title"]))
        authors = get_record_value(record, ["authors", "author", "Author", "author_string", "creator"])
        journal = get_record_value(record, ["journal", "journal_title", "source", "publication", "publisher", "Publisher"])
        publication_date = get_record_value(record, ["publication_date", "pub_date", "date", "year", "PublicationDate"])
        doi = get_record_value(record, ["doi", "DOI", "article_doi"])
        pmid = get_record_value(record, ["pmid", "PMID"])
        pmcid = get_record_value(record, ["pmcid", "PMCID"])

        pdf_entry = best_pdf_entry_for_record(record, index)
        matched_pdf_file_name = pdf_entry.get("fileName") or pdf_entry.get("file_name") or pdf_entry.get("name") or ""
        individual_pdf_download_url = entry_url(pdf_entry, "download")
        pdf_preview_url = entry_url(pdf_entry, "preview")
        pdf_is_individual = bool(individual_pdf_download_url)
        pdf_is_package_only = bool(package_url and not individual_pdf_download_url)
        pdf_download_url = individual_pdf_download_url
        pdf_file_name = matched_pdf_file_name
        if pdf_preview_url and pdf_entry.get("is_original_pdf"):
            pdf_status = "Original article PDF available"
            pdf_match_type = "individual_original_pdf"
        elif pdf_preview_url:
            pdf_status = "Generated/demo PDF available"
            pdf_match_type = "generated_preview_pdf"
        elif package_url:
            # Important: the ZIP is a run-level/batch package, not proof that this row
            # has a mapped article-specific PDF. Do not repeat the ZIP as the row file.
            pdf_status = "No article-specific PDF mapped; run-level PDF package available"
            pdf_match_type = "package_only_unmapped"
            pdf_file_name = ""
            pdf_download_url = ""
        elif pdf_response_source in {"local_fallback", "generated_fallback"} and not allow_generated:
            pdf_status = "Original PDF unavailable in demo/fallback mode"
            pdf_match_type = "unavailable_demo_hidden"
        else:
            pdf_status = "Original PDF not available"
            pdf_match_type = "unavailable"

        scope_warning = scope_warning_for_record(record, title)

        rows.append({
            "index": index,
            "citation_id": citation_id,
            "title": title,
            "authors": authors,
            "journal": journal,
            "publication_date": publication_date,
            "doi": doi,
            "pmid": pmid,
            "pmcid": pmcid,
            "pdf_status": pdf_status,
            "pdf_file_name": pdf_file_name,
            "pdf_download_url": pdf_download_url,
            "pdf_preview_url": pdf_preview_url,
            "pdf_package_url": package_url,
            "pdf_is_individual": pdf_is_individual,
            "pdf_is_package_only": pdf_is_package_only,
            "pdf_match_type": pdf_match_type,
            "scope_warning": scope_warning,
        })
    return rows

def list_output_files(run_dir: Path) -> List[Dict[str, Any]]:
    outputs = run_dir / "outputs"
    files: List[Dict[str, Any]] = []

    if not outputs.exists():
        return files

    pdf_response = read_json(outputs / "05_pdf_download.json", default={}) or {}
    hide_generated_pdfs = (str(pdf_response.get("source", "")).lower() in {"local_fallback", "generated_fallback"}) and not allow_generated_pdf_previews()

    for file in sorted(outputs.rglob("*")):
        if file.is_file():
            if hide_generated_pdfs and file.relative_to(outputs).parts[:1] == ("pdfs",):
                continue
            rel_to_base = file.resolve().relative_to(BASE_DIR)

            suffix = file.suffix.lower()
            kind = suffix.replace(".", "") or "file"

            previewable = suffix in [
                ".pdf",
                ".json",
                ".md",
                ".txt",
                ".csv",
                ".yaml",
                ".yml",
                ".log",
            ]

            files.append(
                {
                    "name": file.name,
                    "relative": str(file.relative_to(outputs)),
                    "size": f"{file.stat().st_size:,} bytes",
                    "kind": kind,
                    "previewable": previewable,
                    "url": "/download?" + urlencode({"path": str(rel_to_base)}),
                    "preview_url": "/preview?" + urlencode({"path": str(rel_to_base)}),
                }
            )

    return files

def form_limit(value: Optional[str], use_all: Optional[str] = None) -> Optional[int]:
    """Convert a limit form value into an integer or None for no limit/all records."""
    if use_all:
        return None
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def build_dynamic_exclusion_criteria(query: str) -> Dict[str, str]:
    """Build conservative AI-screening exclusions from the current search query.

    The upstream AI-screening endpoint requires a non-empty exclusion_criteria
    object.  These criteria intentionally avoid guessing the scientific domain:
    the user's own search query defines scope, and uncertain/borderline records
    are retained rather than excluded.

    The legacy study_type/exposure/outcome keys are retained for compatibility
    with the existing AI-screening API contract.
    """
    query_text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not query_text:
        query_text = "the current literature-search topic"

    scope = f"Current literature-search query: {query_text}"

    return {
        "study_type": (
            f"{scope}. Exclude based on study type only when the record is clearly "
            "outside the scope of this search. Do not impose a study-design restriction "
            "unless the search query explicitly requires one. Retain uncertain or "
            "borderline records."
        ),
        "exposure": (
            f"{scope}. Exclude only when the record's main topic, exposure, intervention, "
            "predictor, condition, technology, population, or subject is clearly unrelated "
            "to the search query. Do not add domain-specific restrictions that are not "
            "present in the query. Retain uncertain or borderline records."
        ),
        "outcome": (
            f"{scope}. Exclude only when the record's outcomes, endpoints, objectives, or "
            "overall purpose are clearly unrelated to the search query. Do not require a "
            "particular health outcome or endpoint unless the query explicitly does so. "
            "Retain uncertain or borderline records."
        ),
    }


def resolve_exclusion_criteria(query: str, criteria: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Preserve usable explicit criteria; otherwise generate them from the query."""
    cleaned: Dict[str, str] = {}
    if isinstance(criteria, dict):
        for key, value in criteria.items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if key_text and value_text:
                cleaned[key_text] = value_text
    return cleaned or build_dynamic_exclusion_criteria(query)


def write_config(
    subproject: str,
    query: str,
    start_date: str,
    end_date: str,
    databases: List[str],
    exclusion_criteria: Dict[str, str],
    search_limit: Optional[int],
    screening_limit: Optional[int],
    retrieval_limit: Optional[int],
    intelligence_modules: Optional[List[str]] = None,
    pdf_enabled: bool = True,
    ai_screening_enabled: bool = True,
) -> Path:
    CONFIG_DIR.mkdir(exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    config_path = CONFIG_DIR / f"{slugify(subproject)}_{timestamp}.yaml"
    resolved_exclusion_criteria = resolve_exclusion_criteria(query, exclusion_criteria)

    config = {
        "literature_search": {
            "query": query,
            "start_date": start_date,
            "end_date": end_date,
            "database": databases,
            # v28 strict all-steps mode: do not create an unbounded server-side
            # reference set before AI screening. The AI screening / working-set
            # limit is applied at search, deduplication, AI screening, retrieval,
            # and PDF stages so every requested record goes through every stage.
            # There is no separate literature-search limit in the UI.
            "limit": screening_limit,
        },
        "deduplication": {"limit": screening_limit},
        "ai_screening": {
            "enabled": bool(ai_screening_enabled),
            "exclusion_criteria": resolved_exclusion_criteria,
            "limit": screening_limit,
            # v30: large AI screening runs are chunked so 1000+ records are not
            # sent to the AI-screening API as one fragile long request.
            "chunk_size": int(os.environ.get("SMI_AI_SCREEN_CHUNK_SIZE", "100")),
            "min_chunk_size": int(os.environ.get("SMI_AI_SCREEN_MIN_CHUNK_SIZE", "25")),
            "retries": int(os.environ.get("SMI_AI_SCREEN_RETRIES", "1")),
        },
        "record_retrieval": {"type": "screened", "start_index": 0, "number_of_records": retrieval_limit},
        # v65: no separate PDF limit. The PDF stage attempts PDFs for all
        # records present in 04_retrieved_records.json. Workload is controlled
        # by record retrieval only, not by a PDF-specific UI field or env var.
        "pdf_download": {"enabled": bool(pdf_enabled), "unpaywall_email": "", "row_level_pdfs": True, "batch_size": 1, "min_batch_size": 1},
        "intelligence": {"enabled": True, "modules": intelligence_modules or DEFAULT_MODULES},
        # v28: strict all-steps mode. PDF is part of the normal pipeline unless
        # a developer intentionally changes the config outside the UI.
        "approval_gates": {"post-pdf-download": bool(pdf_enabled), "final-approval": True,},
    }
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False)
    return config_path




def write_run_status_file(run_dir: Path, subproject: str, run_id: str, state: str, current_step: str, note: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.md").write_text(
        f"""# Workflow Status

| Field | Value |
|---|---|
| Subproject | {subproject} |
| Run ID | {run_id} |
| State | {state} |
| Current Step | {current_step} |
| Updated | {dt.datetime.now().isoformat(timespec='seconds')} |

## Note

{note}
""",
        encoding="utf-8",
    )


def cleanup_finished_jobs() -> None:
    """Drop completed background jobs from the active list after preserving their metadata."""
    stale = []
    for job_id, job in list(ACTIVE_RUN_JOBS.items()):
        process = job.get("process")
        if process is None:
            stale.append(job_id)
            continue
        if process.poll() is not None:
            job["returncode"] = process.returncode
            job["state"] = "completed" if process.returncode == 0 else "failed_or_stopped"
            job["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
            # Keep finished jobs visible for a short time on the result page only.
    # Do not delete immediately; users may still be looking at the page.


def active_job_for(subproject: Optional[str], run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    cleanup_finished_jobs()
    for job_id, job in reversed(list(ACTIVE_RUN_JOBS.items())):
        if subproject and job.get("subproject") != subproject:
            continue
        if run_id and job.get("run_id") and job.get("run_id") != run_id:
            continue
        process = job.get("process")
        if process is not None and process.poll() is None:
            out = dict(job)
            out["job_id"] = job_id
            return out
    return None


def write_job_file(job_id: str, job: Dict[str, Any]) -> None:
    jobs_dir = ROOT_DIR / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    serializable = {k: v for k, v in job.items() if k != "process"}
    serializable["job_id"] = job_id
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(serializable, indent=2, default=str), encoding="utf-8")


def read_job_file(job_id: str) -> Dict[str, Any]:
    if not job_id:
        return {}
    path = ROOT_DIR / "jobs" / f"{job_id}.json"
    if not path.exists():
        return {}
    return read_json(path, default={}) or {}


def pid_is_running(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except Exception:
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def mark_run_stopped(subproject: str, run_id: Optional[str], note: str) -> None:
    if not run_id:
        runs = get_runs(subproject)
        run_id = runs[0].name if runs else None
    if not run_id:
        return
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id
    if not run_dir.exists():
        return
    (run_dir / "STOP_REQUESTED").write_text(note + "\n", encoding="utf-8")
    metadata_path = run_dir / "metadata.json"
    metadata = read_json(metadata_path, default={}) or {}
    metadata["state"] = "stopped"
    metadata["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
    metadata["stop_note"] = note
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    write_run_status_file(run_dir, subproject, run_id, "stopped", "stopped", note)


def clear_generated_results(run_dir: Path, subproject: str, run_id: str) -> None:
    """Clear generated artifacts for a run while preserving the run shell and review history."""
    outputs_dir = run_dir / "outputs"

    if outputs_dir.exists():
        shutil.rmtree(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = run_dir / "metadata.json"
    metadata = read_json(metadata_path, default={}) or {}
    metadata["artifacts"] = {}
    metadata["state"] = "results_cleared"
    metadata["cleared_at"] = dt.datetime.now().isoformat(timespec="seconds")
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    write_run_status_file(
        run_dir=run_dir,
        subproject=subproject,
        run_id=run_id,
        state="results_cleared",
        current_step="cleared",
        note="Generated PDFs, workflow outputs, statistics, and intelligence results were cleared. Metadata, approvals, and logs were preserved.",
    )


def common_context(request: Request, active_tab: str) -> Dict[str, Any]:
    summaries = []
    for project in get_projects():
        runs = get_runs(project)
        if runs:
            summaries.append(run_summary(project, runs[0]))
    return {
        "request": request,
        "app_name": APP_NAME,
        "app_full_name": APP_FULL_NAME,
        "active_tab": active_tab,
        "projects": get_projects(),
        "project_summaries": summaries,
        "approval_checkpoints": ["post-pdf-download", "final-approval"],
        "api_connected": bool(os.environ.get("CONNECT_API_KEY")),
        "demo_mode_env": os.environ.get("SMI_DEMO_MODE") == "1",
        "demo_runs_allowed": os.environ.get("SMI_ALLOW_DEMO_RUNS") == "1",
        "today": dt.date.today().isoformat(),
        "module_specs": MODULE_SPECS,
        "active_jobs": [dict(job, job_id=job_id, started_at=format_display_time(job.get("started_at", ""))) for job_id, job in ACTIVE_RUN_JOBS.items() if job.get("process") is not None and job["process"].poll() is None],
    }


def append_approval(run_dir: Path, checkpoint: str, reviewer: str, comments: str) -> None:
    approvals_file = run_dir / "approvals.md"
    approval_text = f"""

APPROVED: {checkpoint}
Reviewer: {reviewer}
Date: {dt.date.today().isoformat()}
Comments: {comments}
"""
    approvals_file.parent.mkdir(parents=True, exist_ok=True)
    with approvals_file.open("a", encoding="utf-8") as handle:
        handle.write(approval_text)


def safe_local_path(relative_path: str) -> Path:
    candidate = (BASE_DIR / relative_path).resolve()
    if BASE_DIR not in candidate.parents and candidate != BASE_DIR:
        raise ValueError("Invalid download path")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(relative_path)
    return candidate


def generate_statistics_files(run_dir: Path) -> Dict[str, str]:
    reports_dir = run_dir / "outputs" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    counts = [
        ("Literature search", count_json_file(run_dir, "01_literature_search.json")),
        ("Deduplication", count_json_file(run_dir, "02_deduplication.json")),
        ("AI screening", count_json_file(run_dir, "03_ai_screening.json")),
        ("Record retrieval", count_json_file(run_dir, "04_retrieved_records.json")),
        ("PDF downloads", count_pdf_downloads(read_json(run_dir / "outputs" / "05_pdf_download.json", default={}))),
    ]
    csv_path = reports_dir / "statistical_table.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["stage", "count"])
        for stage, count in counts:
            writer.writerow([stage, count])

    max_count = max([count for _, count in counts] + [1])
    width, left_pad, row_h = 760, 170, 54
    height = 80 + row_h * len(counts)
    bars = []
    for i, (stage, count) in enumerate(counts):
        y = 55 + i * row_h
        bar_w = int((width - left_pad - 80) * (count / max_count)) if max_count else 0
        bars.append(
            f'<text x="24" y="{y + 20}" font-size="14" fill="#334155" font-weight="700">{html.escape(stage)}</text>'
            f'<rect x="{left_pad}" y="{y}" width="{bar_w}" height="28" rx="7" fill="#0ea5a5" />'
            f'<text x="{left_pad + bar_w + 10}" y="{y + 20}" font-size="13" fill="#0f172a" font-weight="800">{count:,}</text>'
        )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#ffffff" />
<text x="24" y="30" font-size="20" fill="#0f172a" font-weight="900">SMI Workflow Counts</text>
{''.join(bars)}
</svg>'''
    svg_path = reports_dir / "workflow_counts.svg"
    svg_path.write_text(svg, encoding="utf-8")
    rows = "".join(f"<tr><td>{html.escape(stage)}</td><td>{count:,}</td></tr>" for stage, count in counts)
    html_path = reports_dir / "statistics_report.html"
    html_path.write_text(f"""<!doctype html><html><head><title>SMI Statistics Report</title>
<style>body{{font-family:Arial,sans-serif;margin:30px;color:#0f172a}}table{{border-collapse:collapse;width:560px}}td,th{{border:1px solid #cbd5e1;padding:10px}}th{{background:#f1f5f9}}</style></head>
<body><h1>SMI Statistics Report</h1><table><tr><th>Stage</th><th>Count</th></tr>{rows}</table><h2>Graph</h2>{svg}</body></html>""", encoding="utf-8")
    return {"csv": str(csv_path.resolve().relative_to(BASE_DIR)), "svg": str(svg_path.resolve().relative_to(BASE_DIR)), "html": str(html_path.resolve().relative_to(BASE_DIR))}


def risk_csv_path(run_dir: Path) -> Path:
    path = run_dir / "outputs" / "risk_assessment.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_risk_assessment(run_dir: Path, row: Dict[str, str]) -> None:
    path = risk_csv_path(run_dir)
    fieldnames = ["timestamp", "reviewer", "record_id", "title", "selection_bias", "exposure_assessment", "outcome_assessment", "confounding", "reporting_bias", "overall_risk", "notes"]
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fieldnames})


def read_risk_rows(run_dir: Path) -> List[Dict[str, str]]:
    path = risk_csv_path(run_dir)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request,"dashboard.html", common_context(request, "dashboard"))


@app.get("/new", response_class=HTMLResponse)
def new_run(request: Request):
    return templates.TemplateResponse(request,"new_run.html", common_context(request, "new"))


@app.post("/runs/start", response_class=HTMLResponse)
def start_run(
    request: Request,
    subproject: str = Form(...),
    query: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    databases: List[str] = Form(...),
    # v64: the old environmental-health out-of-scope text boxes were removed
    # from the UI. Keep optional form fields only for backward compatibility
    # with an already-open browser page, but do not send them to AI screening.
    study_type_criterion: str = Form(""),
    exposure_criterion: str = Form(""),
    outcome_criterion: str = Form(""),
    search_limit: str = Form(""),
    search_all: Optional[str] = Form("1"),
    screening_limit: str = Form("100"),
    screening_all: Optional[str] = Form(None),
    retrieval_limit: str = Form("100"),
    retrieval_all: Optional[str] = Form(None),
    skip_pdf_download: Optional[str] = Form(None),
    defer_bulk_pdf_download: Optional[str] = Form(None),
    skip_ai_screening: Optional[str] = Form(None),
    intelligence_modules: List[str] = Form(default=DEFAULT_MODULES),
):
    subproject_slug = slugify(subproject)

    config_path = write_config(
        subproject=subproject,
        query=query,
        start_date=start_date,
        end_date=end_date,
        databases=databases,
        # The AI-screening API requires non-empty exclusion criteria.
        # Build them from the user's current query instead of imposing a
        # hard-coded scientific domain or scope.
        exclusion_criteria=build_dynamic_exclusion_criteria(query),
        search_limit=form_limit(search_limit, search_all),
        screening_limit=form_limit(screening_limit, screening_all),
        retrieval_limit=form_limit(retrieval_limit, retrieval_all),
        intelligence_modules=intelligence_modules or DEFAULT_MODULES,
        # Targeted PDF harvest UI removed; keep original v37 main workflow PDF behavior.
        pdf_enabled=True,
        ai_screening_enabled=True,
    )

    commands = [
        [sys.executable, str(WORKFLOW_SCRIPT), "--root", str(ROOT_DIR), "init"],
        [sys.executable, str(WORKFLOW_SCRIPT), "--root", str(ROOT_DIR), "create-subproject", subproject_slug],
        [
            sys.executable,
            str(WORKFLOW_SCRIPT),
            "--root",
            str(ROOT_DIR),
            "run",
            "--subproject",
            subproject_slug,
            "--config",
            str(config_path),
        ],
    ]

    command_outputs = []
    failed = False
    job_id = ""
    job_pid = ""
    job_log_url = ""

    # Keep repository setup synchronous, then launch the potentially long live workflow
    # in the background so the Stop Run button can interrupt it.
    for command in commands[:2]:
        code, stdout, stderr = run_command(command)
        command_outputs.append({"command": " ".join(command), "code": code, "stdout": stdout, "stderr": stderr})
        if code != 0:
            failed = True
            break

    if not failed:
        workflow_command = commands[2]
        jobs_dir = ROOT_DIR / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_id = f"job-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        log_path = jobs_dir / f"{job_id}.log"
        log_handle = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            workflow_command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
            cwd=str(BASE_DIR),
            start_new_session=True,
        )
        time.sleep(0.75)
        runs_after_launch = get_runs(subproject_slug)
        launched_run = runs_after_launch[0].name if runs_after_launch else ""
        job_pid = str(process.pid)
        rel_log = log_path.resolve().relative_to(BASE_DIR)
        job_log_url = "/preview?" + urlencode({"path": str(rel_log)})
        ACTIVE_RUN_JOBS[job_id] = {
            "process": process,
            "pid": process.pid,
            "subproject": subproject_slug,
            "run_id": launched_run,
            "config_path": str(config_path),
            "log_path": str(log_path),
            "log_url": job_log_url,
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "state": "running",
            "command": " ".join(workflow_command),
        }
        write_job_file(job_id, ACTIVE_RUN_JOBS[job_id])
        command_outputs.append({
            "command": " ".join(workflow_command),
            "code": "running",
            "stdout": f"Started in background. PID: {process.pid}. Use Stop Run if you need to interrupt it.",
            "stderr": "",
        })

    combined_output = "\n".join(
        [
            item.get("command", "") + "\n" + str(item.get("stdout", "")) + "\n" + str(item.get("stderr", ""))
            for item in command_outputs
        ]
    )

    pause_info = detect_workflow_pause(combined_output)
    runs_after_start = get_runs(subproject_slug)
    selected_run = runs_after_start[0] if runs_after_start else None
    selected_run_id = selected_run.name if selected_run else ""
    if job_id and selected_run_id and job_id in ACTIVE_RUN_JOBS:
        ACTIVE_RUN_JOBS[job_id]["run_id"] = selected_run_id
        write_job_file(job_id, ACTIVE_RUN_JOBS[job_id])
    selected_summary = run_summary(subproject_slug, selected_run) if selected_run else None
    log_is_available = bool(command_outputs)

    ctx = common_context(request, "new")

    ctx.update(
        {
            "subproject_slug": subproject_slug,
            "run_id": selected_run_id,
            "selected_run": selected_run,
            "run_summary": selected_summary,
            "config_path": config_path,
            "command_outputs": command_outputs,
            "combined_output": combined_output,
            "pause_info": pause_info,
            "needs_approval": pause_info["paused"],
            "failed": failed,
            "log_is_available": log_is_available,
            "job_id": job_id,
            "job_pid": job_pid,
            "job_log_url": job_log_url,
            "running_in_background": bool(job_id and not failed),
        }
    )

    return templates.TemplateResponse(
        request,
        "run_result.html",
        ctx,
    )
@app.get("/runs/status", response_class=HTMLResponse)
def run_status(request: Request, subproject: str = Query(""), run_id: str = Query(""), job_id: str = Query("")):
    selected_project, selected_run = get_selected_run(subproject or None, run_id or None)
    selected_run_id = selected_run.name if selected_run else run_id
    selected_summary = run_summary(selected_project, selected_run) if selected_project and selected_run else None

    job = ACTIVE_RUN_JOBS.get(job_id) if job_id else None
    job_file = read_job_file(job_id)
    pid = (job or {}).get("pid") or job_file.get("pid")
    running = False
    if job and job.get("process") is not None:
        running = job["process"].poll() is None
    elif pid:
        running = pid_is_running(pid)

    state = selected_summary.get("state") if selected_summary else ("running" if running else "unknown")
    failed = state == "failed" or state == "failed_or_stopped"
    needs_approval = state == "waiting_for_review"
    log_url = (job or {}).get("log_url") or job_file.get("log_url")

    ctx = common_context(request, "new")
    ctx.update({
        "subproject_slug": selected_project or subproject,
        "run_id": selected_run_id,
        "selected_run": selected_run,
        "run_summary": selected_summary,
        "config_path": job_file.get("config_path", ""),
        "command_outputs": [{
            "command": "workflow status",
            "code": "running" if running else (1 if failed else 0),
            "stdout": "Workflow is still running." if running else f"Workflow state: {state}",
            "stderr": "",
        }],
        "combined_output": "",
        "pause_info": {"paused": needs_approval, "message": "Workflow is waiting for human approval.", "approval_checkpoint": "post-pdf-download or final-approval"},
        "needs_approval": needs_approval,
        "failed": failed,
        "log_is_available": bool(log_url),
        "job_id": job_id,
        "job_pid": str(pid or ""),
        "job_log_url": log_url,
        "running_in_background": running,
        "ai_progress": ai_screening_progress(selected_run),
        "pdf_progress": pdf_download_progress(selected_run),
    })
    return templates.TemplateResponse(request, "run_result.html", ctx)


@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    selected_project, selected_run = get_selected_run(subproject, run_id)
    ctx = common_context(request, "approvals")
    ctx.update({"selected_project": selected_project, "selected_run": selected_run, "runs": get_runs(selected_project) if selected_project else [], "status_text": format_status_markdown_times(read_text(selected_run / "status.md")) if selected_run else "", "approvals_text": read_text(selected_run / "approvals.md") if selected_run else ""})
    return templates.TemplateResponse(request,"approvals.html", ctx)


@app.post("/approvals/add")
def approvals_add(subproject: str = Form(...), run_id: str = Form(...), checkpoint: str = Form(...), reviewer: str = Form(...), comments: str = Form(...)):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id
    append_approval(run_dir, checkpoint, reviewer, comments)
    return RedirectResponse(f"/approvals?{urlencode({'subproject': subproject, 'run_id': run_id})}", status_code=303)


@app.post("/runs/resume", response_class=HTMLResponse)
def resume_run(request: Request, subproject: str = Form(...), run_id: str = Form(...)):
    command = [
        sys.executable,
        str(WORKFLOW_SCRIPT),
        "--root",
        str(ROOT_DIR),
        "resume",
        "--subproject",
        subproject,
        "--run-id",
        run_id,
    ]

    code, stdout, stderr = run_command(command)

    combined_output = "\n".join([" ".join(command), stdout, stderr])
    pause_info = detect_workflow_pause(combined_output)

    ctx = common_context(request, "approvals")
    ctx.update(
        {
            "subproject_slug": subproject,
            "run_id": run_id,
            "command_outputs": [
                {
                    "command": " ".join(command),
                    "code": code,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            ],
            "combined_output": combined_output,
            "pause_info": pause_info,
            "needs_approval": pause_info["paused"],
            "failed": code != 0,
        }
    )

    return templates.TemplateResponse(request, "run_result.html", ctx)




@app.post("/runs/stop")
def stop_run(
    job_id: str = Form(""),
    subproject: str = Form(""),
    run_id: str = Form(""),
    return_to: str = Form("outputs"),
):
    job = ACTIVE_RUN_JOBS.get(job_id) if job_id else None
    note = "Workflow stop requested by user from the SMI Workbench UI."

    if job:
        process = job.get("process")
        subproject = subproject or job.get("subproject", "")
        run_id = run_id or job.get("run_id", "")
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except Exception:
                try:
                    process.terminate()
                except Exception:
                    pass
            try:
                process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
        job["state"] = "stopped"
        job["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
        write_job_file(job_id, job)

    if subproject:
        mark_run_stopped(subproject, run_id or None, note)

    targets = {
        "dashboard": "/",
        "outputs": "/outputs",
        "run": "/outputs",
        "intelligence": "/intelligence",
    }
    base = targets.get(return_to, "/outputs")
    params = {"stopped": "1"}
    if subproject:
        params["subproject"] = subproject
    if run_id:
        params["run_id"] = run_id
    return RedirectResponse(base + "?" + urlencode(params), status_code=303)


@app.post("/runs/clear-results")
def clear_results(
    subproject: str = Form(...),
    run_id: str = Form(...),
    return_to: str = Form("outputs"),
):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id

    if not run_dir.exists() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="Run not found.")

    clear_generated_results(run_dir, subproject, run_id)

    targets = {
        "outputs": "/outputs",
        "statistics": "/statistics",
        "risk": "/risk",
        "intelligence": "/intelligence",
        "reports": "/intelligence/reports",
    }
    base = targets.get(return_to, "/outputs")
    return RedirectResponse(
        base + "?" + urlencode({"subproject": subproject, "run_id": run_id, "cleared": "1"}),
        status_code=303,
    )


@app.get("/outputs", response_class=HTMLResponse)
def outputs(
    request: Request,
    subproject: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
):
    selected_project, selected_run = get_selected_run(subproject, run_id)

    ctx = common_context(request, "outputs")

    files = []
    citation_rows = []
    selected_run_id = ""
    pdf_package_url = ""
    pdf_package_name = ""
    pdf_package_links = []

    if selected_run:
        selected_run_dir = selected_run
        selected_run_id = selected_run_dir.name

        files = list_output_files(selected_run_dir)
        citation_rows = build_citation_table(selected_run_dir)
        for row in citation_rows:
            row["evidence_pack_url"] = evidence_pack_url(
                selected_project or "",
                selected_run_id,
                record={"title": row.get("title", ""), "pmid": row.get("pmid", ""), "doi": row.get("doi", ""), "pmcid": row.get("pmcid", "")},
            )
            if row.get("pdf_package_url") and not pdf_package_url:
                pdf_package_url = row.get("pdf_package_url", "")
        pdf_response = read_json(selected_run_dir / "outputs" / "05_pdf_download.json", default={})
        if isinstance(pdf_response, dict):
            pdf_package_name = pdf_response.get("fileName", "") or pdf_response.get("file_name", "")
            pdf_package_links = pdf_package_links_for_run(selected_run_dir, pdf_response)

    ctx.update(
        {
            "selected_project": selected_project,
            "selected_run": selected_run,
            "runs": get_runs(selected_project) if selected_project else [],
            "files": files,

            # Citation/PDF table values
            "citation_rows": citation_rows,
            "selected_subproject": selected_project or "",
            "selected_run_id": selected_run_id,
            "pdf_package_url": pdf_package_url,
            "pdf_package_name": pdf_package_name,
            "pdf_package_links": pdf_package_links,
            "active_job": active_job_for(selected_project, selected_run_id) if selected_project and selected_run_id else None,
        }
    )

    return templates.TemplateResponse(
        request,
        "outputs.html",
        ctx,
    )


@app.get("/pdf-viewer", response_class=HTMLResponse)
def pdf_viewer(request: Request, path: str):
    try:
        file_path = safe_path_from_relative(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except ValueError:
        raise HTTPException(status_code=403, detail="File access denied.")

    if file_path.suffix.lower() != ".pdf":
        return RedirectResponse("/preview?" + urlencode({"path": path}), status_code=303)

    ctx = common_context(request, "outputs")
    ctx.update({
        "pdf_name": file_path.name,
        "preview_url": "/preview?" + urlencode({"path": path}),
        "download_url": "/download?" + urlencode({"path": path}),
    })
    return templates.TemplateResponse(request, "pdf_viewer.html", ctx)


@app.get("/preview")
def preview_file(path: str):
    try:
        file_path = safe_path_from_relative(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except ValueError:
        raise HTTPException(status_code=403, detail="File access denied.")

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return FileResponse(
            path=file_path,
            media_type="application/pdf",
            filename=file_path.name,
            headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
        )

    if suffix in [".json", ".md", ".txt", ".csv", ".yaml", ".yml", ".log"]:
        return PlainTextResponse(
            file_path.read_text(encoding="utf-8", errors="replace")
        )

    raise HTTPException(status_code=415, detail="Preview not available for this file type.")

@app.get("/citation-table.csv")
def citation_table_csv(subproject: str, run_id: str):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id

    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found.")

    rows = build_citation_table(run_dir)

    output = io.StringIO()

    fieldnames = [
        "index",
        "citation_id",
        "title",
        "authors",
        "journal",
        "publication_date",
        "doi",
        "pmid",
        "pmcid",
        "pdf_status",
        "pdf_file_name",
        "pdf_match_type",
        "scope_warning",
    ]

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{subproject}_{run_id}_citation_table.csv"'
        },
    )

@app.get("/statistics", response_class=HTMLResponse)
def statistics(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    selected_project, selected_run = get_selected_run(subproject, run_id)
    svg_inline = read_text(selected_run / "outputs" / "reports" / "workflow_counts.svg") if selected_run else ""
    ctx = common_context(request, "statistics")
    ctx.update({"selected_project": selected_project, "selected_run": selected_run, "runs": get_runs(selected_project) if selected_project else [], "summary": run_summary(selected_project, selected_run) if selected_project and selected_run else None, "generated": None, "svg_inline": svg_inline, "files": list_output_files(selected_run) if selected_run else []})
    return templates.TemplateResponse(request,"statistics.html", ctx)


@app.post("/statistics/generate", response_class=HTMLResponse)
def statistics_generate(request: Request, subproject: str = Form(...), run_id: str = Form(...)):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id
    generated = generate_statistics_files(run_dir)
    ctx = common_context(request, "statistics")
    ctx.update({"selected_project": subproject, "selected_run": run_dir, "runs": get_runs(subproject), "summary": run_summary(subproject, run_dir), "generated": {key: "/download?" + urlencode({"path": value}) for key, value in generated.items()}, "svg_inline": read_text(BASE_DIR / generated["svg"]), "files": list_output_files(run_dir)})
    return templates.TemplateResponse(request,"statistics.html", ctx)


@app.get("/risk", response_class=HTMLResponse)
def risk(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    selected_project, selected_run = get_selected_run(subproject, run_id)
    ctx = common_context(request, "risk")
    ctx.update({"selected_project": selected_project, "selected_run": selected_run, "runs": get_runs(selected_project) if selected_project else [], "risk_rows": read_risk_rows(selected_run) if selected_run else []})
    return templates.TemplateResponse(request,"risk.html", ctx)


@app.post("/risk/add")
def risk_add(subproject: str = Form(...), run_id: str = Form(...), reviewer: str = Form(...), record_id: str = Form(...), title: str = Form(""), selection_bias: str = Form(...), exposure_assessment: str = Form(...), outcome_assessment: str = Form(...), confounding: str = Form(...), reporting_bias: str = Form(...), overall_risk: str = Form(...), notes: str = Form("")):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id
    append_risk_assessment(run_dir, {"timestamp": dt.datetime.now().isoformat(timespec="seconds"), "reviewer": reviewer, "record_id": record_id, "title": title, "selection_bias": selection_bias, "exposure_assessment": exposure_assessment, "outcome_assessment": outcome_assessment, "confounding": confounding, "reporting_bias": reporting_bias, "overall_risk": overall_risk, "notes": notes})
    return RedirectResponse(f"/risk?{urlencode({'subproject': subproject, 'run_id': run_id})}", status_code=303)


@app.get("/download")
def download_file(path: str):
    try:
        file_path = safe_path_from_relative(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except ValueError:
        raise HTTPException(status_code=403, detail="File access denied.")

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
    )

# -----------------------------------------------------------------------------
# Intelligence add-on routes. These preserve the original app routes above and
# only add structured extraction/review pages on top of existing SMI runs.
# -----------------------------------------------------------------------------

def intelligence_counts(run_dir: Optional[Path]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    if not run_dir:
        return {name: 0 for name in MODULE_SPECS}
    for name in MODULE_SPECS:
        counts[name] = len(read_module_rows(run_dir, name))
    return counts


def selected_run_context(request: Request, active_tab: str, subproject: Optional[str], run_id: Optional[str]) -> Dict[str, Any]:
    selected_project, selected_run = get_selected_run(subproject, run_id)
    ctx = common_context(request, active_tab)
    ctx.update({
        "selected_project": selected_project,
        "selected_run": selected_run,
        "runs": get_runs(selected_project) if selected_project else [],
        "counts": intelligence_counts(selected_run),
        "summary_text": read_text(selected_run / "outputs" / SUMMARY_FILE) if selected_run else "",
    })
    return ctx


# -----------------------------------------------------------------------------
# Evidence Pack / traceability helpers. These make every extracted row traceable
# back to the original citation, abstract, mapped PDF, and sibling extractions.
# -----------------------------------------------------------------------------

def _norm_match(value: Any) -> str:
    text = "" if value is None else str(value)
    text = clean_title_text(text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _extract_id_from_text(text: Any, kind: str) -> str:
    value = "" if text is None else str(text)
    if kind == "pmid":
        m = re.search(r"\bPMID\s*:?\s*(\d+)\b", value, flags=re.I) or re.search(r"\bpmid[-_ ]?(\d+)\b", value, flags=re.I)
        return m.group(1) if m else ""
    if kind == "pmcid":
        m = re.search(r"\bPMC(?:ID)?\s*:?\s*(PMC?\d+)\b", value, flags=re.I)
        if m:
            out = m.group(1).upper()
            return out if out.startswith("PMC") else "PMC" + out
        return ""
    if kind == "doi":
        m = re.search(r"\bDOI\s*:?\s*(10\.\S+)\b", value, flags=re.I) or re.search(r"(10\.[^\s;,)]+)", value, flags=re.I)
        return m.group(1).rstrip(".;,)").lower() if m else ""
    return ""


def _record_source_fields(record: Dict[str, Any]) -> Dict[str, str]:
    title = clean_title_text(get_record_value(record, ["title", "article_title", "Title", "name"]))
    pmid = get_record_value(record, ["pmid", "PMID"])
    doi = get_record_value(record, ["doi", "DOI", "article_doi"])
    pmcid = get_record_value(record, ["pmcid", "PMCID"])
    citation = []
    if pmid:
        citation.append(f"PMID: {pmid}")
    if doi:
        citation.append(f"DOI: {doi}")
    if pmcid:
        citation.append(f"PMCID: {pmcid}")
    return {
        "title": title,
        "pmid": str(pmid or ""),
        "doi": str(doi or ""),
        "pmcid": str(pmcid or ""),
        "citation": " | ".join(citation) or get_record_value(record, ["citation", "id", "uid", "record_id"]),
    }


def _row_source_title(row: Dict[str, Any]) -> str:
    return clean_title_text(
        row.get("source_title")
        or row.get("source_paper")
        or row.get("title")
        or row.get("paper_title")
        or row.get("article_title")
        or ""
    )


def _row_source_citation(row: Dict[str, Any]) -> str:
    return str(row.get("source_citation") or row.get("citation") or row.get("linked_publications") or row.get("source_url") or "")


def _row_identifier(row: Dict[str, Any], kind: str) -> str:
    direct = row.get(f"source_{kind}") or row.get(kind) or row.get(kind.upper())
    if direct:
        return str(direct).replace("PMID:", "").replace("DOI:", "").strip()
    hay = " | ".join(str(row.get(k, "")) for k in ["source_citation", "citation", "linked_publications", "source_title", "source_paper", "title"])
    return _extract_id_from_text(hay, kind)


def load_best_records(run_dir: Path) -> List[Dict[str, Any]]:
    return cached_best_records(run_dir)


def _normalize_source_identifier(value: Any, kind: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if kind == "doi":
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
        text = re.sub(r"^doi\s*:\s*", "", text, flags=re.I)
        return text.strip().rstrip(".").lower()
    if kind == "pmcid":
        text = re.sub(r"^pmcid\s*:\s*", "", text, flags=re.I)
        return text.strip().upper()
    if kind == "pmid":
        text = re.sub(r"^pmid\s*:\s*", "", text, flags=re.I)
        m = re.search(r"\d+", text)
        return m.group(0) if m else text.strip()
    return text


def record_matches_source(record: Dict[str, Any], row: Dict[str, Any]) -> bool:
    """Match an extracted row to its source paper without crossing strong-ID conflicts.

    Strong identifiers are authoritative. If both sides provide the same identifier
    type and the values conflict, the records are *not* the same paper even when
    their titles are highly similar (for example, papers from the same GBD series).
    Fuzzy title matching is used only when no comparable strong identifier conflicts.
    """
    fields = _record_source_fields(record)
    strong_match = False

    for kind in ("pmid", "pmcid", "doi"):
        row_value = _normalize_source_identifier(_row_identifier(row, kind), kind)
        record_value = _normalize_source_identifier(fields.get(kind), kind)
        if not row_value or not record_value:
            continue
        if row_value != record_value:
            return False
        strong_match = True

    if strong_match:
        return True

    row_title = _norm_match(_row_source_title(row))
    rec_title = _norm_match(fields.get("title"))
    if row_title and rec_title and (row_title == rec_title or row_title in rec_title or rec_title in row_title):
        return True
    if row_title and rec_title:
        row_terms = {x for x in row_title.split() if len(x) > 4}
        rec_terms = {x for x in rec_title.split() if len(x) > 4}
        if row_terms and len(row_terms & rec_terms) / max(len(row_terms), 1) >= 0.75:
            return True
    return False


def find_record_for_source(run_dir: Path, row: Dict[str, Any]) -> Dict[str, Any]:
    for record in load_best_records(run_dir):
        if record_matches_source(record, row):
            return record
    return {}


def find_record_by_query(run_dir: Path, pmid: str = "", doi: str = "", pmcid: str = "", title: str = "") -> Dict[str, Any]:
    row = {"source_pmid": pmid, "source_doi": doi, "source_pmcid": pmcid, "source_title": title}
    return find_record_for_source(run_dir, row)


def find_intelligence_item(run_dir: Path, module_name: str, item_id: str) -> Dict[str, Any]:
    if module_name not in MODULE_SPECS:
        return {}
    for row in cached_module_rows(run_dir, module_name):
        if str(row.get("id", "")) == str(item_id):
            return row
    return {}


def evidence_pack_url(subproject: str, run_id: str, module: str = "", item_id: str = "", record: Optional[Dict[str, Any]] = None, row: Optional[Dict[str, Any]] = None) -> str:
    params: Dict[str, str] = {"subproject": subproject or "", "run_id": run_id or ""}
    if module and item_id:
        params.update({"module": module, "item_id": item_id})
    elif record:
        fields = _record_source_fields(record)
        if fields.get("pmid"):
            params["pmid"] = fields["pmid"]
        elif fields.get("doi"):
            params["doi"] = fields["doi"]
        elif fields.get("pmcid"):
            params["pmcid"] = fields["pmcid"]
        elif fields.get("title"):
            params["title"] = fields["title"]
    elif row:
        if _row_identifier(row, "pmid"):
            params["pmid"] = _row_identifier(row, "pmid")
        elif _row_identifier(row, "doi"):
            params["doi"] = _row_identifier(row, "doi")
        elif _row_identifier(row, "pmcid"):
            params["pmcid"] = _row_identifier(row, "pmcid")
        elif _row_source_title(row):
            params["title"] = _row_source_title(row)
    return "/evidence-pack?" + urlencode(params)


def enrich_intelligence_row(row: Dict[str, Any], module_name: str, subproject: str, run_id: str) -> Dict[str, Any]:
    item = dict(row)
    source_title = _row_source_title(item)
    source_citation = _row_source_citation(item)
    # Some module rows, especially older cohort rows, may have only DOI/PMID citation fields.
    # Look up the source record so the UI does not show "Source title not available" when the
    # retrieved article metadata already contains the title.
    if not source_title:
        run_dir = ROOT_DIR / "subprojects" / str(subproject or "") / "runs" / str(run_id or "")
        if run_dir.exists():
            record = find_record_for_source(run_dir, item)
            fields = _record_source_fields(record) if record else {}
            if fields.get("title"):
                source_title = clean_title_text(fields.get("title", ""))
                item.setdefault("source_title", source_title)
            if not source_citation and fields.get("citation"):
                source_citation = fields.get("citation", "")
    item["source_title_display"] = source_title or "Source title not available"
    item["source_citation_display"] = source_citation
    item["evidence_pack_url"] = evidence_pack_url(subproject, run_id, module_name, str(item.get("id", "")), row=item)
    return item


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(v) for v in value if str(v).strip())
    return str(value).strip()


def _human_field_name(name: str) -> str:
    special = {
        "id": "Id",
        "pmid": "PMID",
        "pmcid": "PMCID",
        "doi": "DOI",
        "ckd": "CKD",
        "ehr": "EHR",
    }
    parts = []
    for part in str(name or "").split("_"):
        low = part.lower()
        parts.append(special.get(low, part[:1].upper() + part[1:]))
    return " ".join(parts)


def _row_detail_items(row: Dict[str, Any], module_name: str) -> List[Dict[str, str]]:
    """Return ordered field/value details for Evidence Pack display.

    The compact module table used to show only an item label and evidence sentence,
    which hid structured clinical fields such as population, comparator, data source,
    recruitment site, enrollment site, and study site.  This helper keeps the concise
    row while exposing the complete structured extraction under it.
    """
    spec = MODULE_SPECS.get(module_name, {})
    columns = list(spec.get("columns", []))
    if not columns:
        columns = list(row.keys())
    skip = {"id", "citation", "source_title", "review_status", "reviewer_notes"}
    details: List[Dict[str, str]] = []
    for col in columns:
        if col in skip:
            continue
        value = _display_value(row.get(col, ""))
        if value == "":
            continue
        details.append({"field": _human_field_name(col), "value": value})
    return details


def _related_label_for_row(module_name: str, row: Dict[str, Any]) -> str:
    if module_name == "clinical_epidemiology":
        evidence_type = _display_value(row.get("evidence_type"))
        outcome = _display_value(row.get("health_outcome"))
        title = _display_value(row.get("source_title"))
        if evidence_type:
            # Keep Evidence Pack item labels readable; long outcome lists are shown
            # under "Show structured fields" instead of crowding the ITEM column.
            if outcome and outcome.lower() not in {"not extracted", "not specified"} and len(outcome) <= 90:
                return f"{evidence_type} — {outcome}"
            if title:
                return f"{evidence_type} — {title[:90]}"
            return evidence_type
    return (
        _display_value(row.get("exposure_name"))
        or _display_value(row.get("predictor_or_exposure"))
        or _display_value(row.get("dataset_name"))
        or _display_value(row.get("dataset_title"))
        or _display_value(row.get("cohort_name"))
        or _display_value(row.get("chemical_name"))
        or _display_value(row.get("id"))
    )


def _source_row_is_minimal(row: Dict[str, Any]) -> bool:
    if not row:
        return True
    visible = [k for k, v in row.items() if _display_value(v)]
    return set(visible).issubset({"source_title", "title", "citation", "source_citation", "pmid", "doi", "pmcid"})


def collect_related_extractions(run_dir: Path, record: Dict[str, Any], focus_module: str = "", focus_item_id: str = "") -> List[Dict[str, Any]]:
    related: List[Dict[str, Any]] = []
    for module_name, spec in MODULE_SPECS.items():
        for row in cached_module_rows(run_dir, module_name):
            is_focus = module_name == focus_module and str(row.get("id", "")) == str(focus_item_id)
            if is_focus or (record and record_matches_source(record, row)):
                related.append({
                    "module": module_name,
                    "module_label": spec["label"],
                    "id": row.get("id", ""),
                    "label": _related_label_for_row(module_name, row),
                    "evidence": row.get("evidence_sentence") or row.get("market_or_product_use") or row.get("testing_rationale") or row.get("source_title") or row.get("source_paper") or "",
                    "confidence": row.get("confidence", ""),
                    "review_status": row.get("review_status", "pending"),
                    "details": _row_detail_items(row, module_name),
                    "row": row,
                    "is_focus": is_focus,
                })
    return related


def citation_row_for_record(run_dir: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    if not record or not evidence_pack_load_pdf_status():
        return {}
    probe = {"source_title": _record_source_fields(record).get("title"), "source_pmid": _record_source_fields(record).get("pmid"), "source_doi": _record_source_fields(record).get("doi"), "source_pmcid": _record_source_fields(record).get("pmcid")}
    key = str(run_dir.resolve())
    outputs = run_dir / "outputs"
    sig = max(_mtime(outputs / "04_retrieved_records.json"), _mtime(outputs / "05_pdf_download.json"))
    cached = _CITATION_TABLE_CACHE.get(key)
    if cached and cached[0] == sig:
        rows = cached[1]
    else:
        rows = build_citation_table(run_dir)
        _CITATION_TABLE_CACHE[key] = (sig, rows)
    for row in rows:
        fake_record = {"title": row.get("title", ""), "pmid": row.get("pmid", ""), "doi": row.get("doi", ""), "pmcid": row.get("pmcid", "")}
        if record_matches_source(fake_record, probe):
            return row
    return {}


@app.get("/intelligence", response_class=HTMLResponse)
def intelligence_dashboard(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    return templates.TemplateResponse(request, "intelligence.html", selected_run_context(request, "intelligence", subproject, run_id))


@app.post("/intelligence/generate", response_class=HTMLResponse)
def intelligence_generate(request: Request, subproject: str = Form(...), run_id: str = Form(...)):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    counts = generate_intelligence_outputs(run_dir, DEFAULT_MODULES)
    knowledge_counts_result = generate_knowledge_space_outputs(run_dir)
    ctx = selected_run_context(request, "intelligence", subproject, run_id)
    ctx.update({"generated_counts": counts, "knowledge_counts_result": knowledge_counts_result})
    return templates.TemplateResponse(request, "intelligence.html", ctx)


@app.get("/intelligence/review", response_class=HTMLResponse)
def intelligence_review(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None), status: str = Query("all")):
    ctx = selected_run_context(request, "intelligence_review", subproject, run_id)
    selected_run = ctx.get("selected_run")
    rows = all_review_rows(selected_run) if selected_run else []
    if selected_run and ctx.get("selected_project"):
        rows = [enrich_intelligence_row(row, row.get("module", ""), ctx.get("selected_project", ""), selected_run.name) for row in rows]
    if status != "all":
        rows = [row for row in rows if row.get("review_status", "pending") == status]
    ctx.update({"review_rows": rows, "status_filter": status})
    return templates.TemplateResponse(request, "intelligence_review.html", ctx)


@app.post("/intelligence/review/update")
def intelligence_review_update(
    subproject: str = Form(...),
    run_id: str = Form(...),
    module: str = Form(...),
    item_id: str = Form(...),
    status: str = Form(...),
    notes: str = Form(""),
):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    ok = update_review_decision(run_dir, module, item_id, status, notes)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return RedirectResponse(f"/intelligence/review?{urlencode({'subproject': subproject, 'run_id': run_id})}", status_code=303)


@app.get("/evidence-pack", response_class=HTMLResponse)
def evidence_pack(
    request: Request,
    subproject: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    module: str = Query(""),
    item_id: str = Query(""),
    pmid: str = Query(""),
    doi: str = Query(""),
    pmcid: str = Query(""),
    title: str = Query(""),
):
    ctx = selected_run_context(request, "intelligence", subproject, run_id)
    selected_run = ctx.get("selected_run")
    selected_project = ctx.get("selected_project")
    if not selected_run:
        return templates.TemplateResponse(request, "evidence_pack.html", {**ctx, "record": {}, "record_fields": {}, "source_row": {}, "related_rows": [], "citation_row": {}, "source_found": False})

    source_row: Dict[str, Any] = {}
    record: Dict[str, Any] = {}
    if module and item_id:
        source_row = find_intelligence_item(selected_run, module, item_id)
        if source_row:
            record = find_record_for_source(selected_run, source_row)
    if not record:
        record = find_record_by_query(selected_run, pmid=pmid, doi=doi, pmcid=pmcid, title=title)
    if not source_row and record:
        source_row = {"source_title": _record_source_fields(record).get("title", ""), "citation": _record_source_fields(record).get("citation", "")}

    record_fields = _record_source_fields(record) if record else {
        "title": _row_source_title(source_row) or title,
        "citation": _row_source_citation(source_row),
        "pmid": pmid,
        "doi": doi,
        "pmcid": pmcid,
    }
    abstract = app_abstract_text(record) if record else ""
    authors = get_record_value(record, ["authors", "author", "Author", "author_string", "creator"]) if record else ""
    journal = get_record_value(record, ["journal", "journal_title", "source", "publication", "publisher", "Publisher"]) if record else ""
    publication_date = get_record_value(record, ["publication_date", "pub_date", "date", "year", "PublicationDate"]) if record else ""
    citation_row = citation_row_for_record(selected_run, record) if record else {}
    related_rows = collect_related_extractions(selected_run, record, module, item_id) if record else ([] if not source_row else collect_related_extractions(selected_run, {}, module, item_id))

    # When an Evidence Pack is opened from the citation/source table, the initial
    # source_row only has title/citation identifiers.  If structured extractions
    # from the same paper are available, promote the focused/first extraction row
    # so the "Selected extracted row" card shows the full field structure rather
    # than only Source Title and Citation.
    if _source_row_is_minimal(source_row) and related_rows:
        preferred = next((item for item in related_rows if item.get("is_focus")), related_rows[0])
        if preferred.get("row"):
            source_row = dict(preferred["row"])

    ctx.update({
        "record": record,
        "record_fields": record_fields,
        "source_row": source_row,
        "related_rows": related_rows,
        "citation_row": citation_row,
        "abstract": abstract,
        "authors": authors,
        "journal": journal,
        "publication_date": publication_date,
        "source_found": bool(record),
        "pdf_status_fast_mode": bool(record) and not evidence_pack_load_pdf_status(),
        "back_to_module_url": f"/intelligence/{MODULE_SPECS.get(module, {}).get('short', '')}?" + urlencode({"subproject": selected_project or "", "run_id": selected_run.name}) if module in MODULE_SPECS else "",
    })
    return templates.TemplateResponse(request, "evidence_pack.html", ctx)


@app.get("/intelligence/reports", response_class=HTMLResponse)
def intelligence_reports(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    ctx = selected_run_context(request, "intelligence_reports", subproject, run_id)
    selected_run = ctx.get("selected_run")
    files = []
    if selected_run:
        outputs = selected_run / "outputs"
        wanted = {spec["json"] for spec in MODULE_SPECS.values()} | {spec["csv"] for spec in MODULE_SPECS.values()} | {SUMMARY_FILE, ARTICLE_METHODS_JSON, ARTICLE_METHODS_CSV}
        for file in sorted(outputs.iterdir()):
            if file.name in wanted:
                rel = file.resolve().relative_to(BASE_DIR)
                files.append({
                    "name": file.name,
                    "size": f"{file.stat().st_size:,} bytes",
                    "url": "/download?" + urlencode({"path": str(rel)}),
                    "preview_url": "/preview?" + urlencode({"path": str(rel)}),
                })
    ctx.update({"intelligence_files": files})
    return templates.TemplateResponse(request, "intelligence_reports.html", ctx)


@app.get("/intelligence/{module_short}", response_class=HTMLResponse)
def intelligence_module(request: Request, module_short: str, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    module_name = module_from_short(module_short)
    if not module_name:
        raise HTTPException(status_code=404, detail="Unknown intelligence module")
    ctx = selected_run_context(request, "intelligence", subproject, run_id)
    selected_run = ctx.get("selected_run")
    rows = read_module_rows(selected_run, module_name) if selected_run else []
    if selected_run and ctx.get("selected_project"):
        rows = [enrich_intelligence_row(row, module_name, ctx.get("selected_project", ""), selected_run.name) for row in rows]
    spec = MODULE_SPECS[module_name]
    files = []
    if selected_run:
        for filename in [spec["json"], spec["csv"]]:
            file = selected_run / "outputs" / filename
            if file.exists():
                rel = file.resolve().relative_to(BASE_DIR)
                files.append({
                    "name": filename,
                    "url": "/download?" + urlencode({"path": str(rel)}),
                    "preview_url": "/preview?" + urlencode({"path": str(rel)}),
                })
    ctx.update({"module_name": module_name, "spec": spec, "rows": rows, "files": files})
    return templates.TemplateResponse(request, "intelligence_module.html", ctx)

# -----------------------------------------------------------------------------
# Automated validation against LaserAI controlled-vocabulary exports
# -----------------------------------------------------------------------------

@app.get("/validation", response_class=HTMLResponse)
def validation_dashboard(
    request: Request,
    validation_id: Optional[str] = Query(None),
    page: int = Query(1),
):
    ctx = common_context(request, "validation")
    recent = list_validations(limit=25)
    selected_id = validation_id or (recent[0].get("validation_id") if recent else None)
    view: Dict[str, Any] = {"status": {}, "summary": {}, "results": [], "result_count": 0, "page": 1, "total_pages": 1, "per_page": 50}
    if selected_id:
        view = validation_view(selected_id, page=page, per_page=50)
    ctx.update({
        "validation_id": selected_id,
        "validation_status": view.get("status", {}),
        "validation_summary": view.get("summary", {}),
        "validation_results": view.get("results", []),
        "validation_result_count": view.get("result_count", 0),
        "validation_page": view.get("page", 1),
        "validation_total_pages": view.get("total_pages", 1),
        "recent_validations": recent,
        "validation_default_workers": max(1, min(int(os.environ.get("SMI_VALIDATION_MAX_WORKERS", "3")), 8)),
    })
    return templates.TemplateResponse(request, "validation.html", ctx)


@app.post("/validation/start")
async def validation_start(
    request: Request,
    benchmark_file: UploadFile = File(...),
    sample_size: str = Form("50"),
    sampling_mode: str = Form("stratified"),
    seed: int = Form(2026),
    max_workers: int = Form(3),
    exclude_development: Optional[str] = Form(None),
):
    filename = str(benchmark_file.filename or "LaserAI Export.xlsx")
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload the LaserAI .xlsx export.")
    content = await benchmark_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded workbook is empty.")
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="The uploaded workbook is larger than 100 MB.")
    sampling_mode = sampling_mode if sampling_mode in {"stratified", "random"} else "stratified"
    try:
        validation_id = start_validation(
            content,
            filename=filename,
            sample_size=sample_size,
            sampling_mode=sampling_mode,
            seed=int(seed),
            max_workers=max(1, min(int(max_workers), 8)),
            exclude_development=bool(exclude_development),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not start validation: {type(exc).__name__}: {exc}") from exc
    return RedirectResponse(url="/validation?" + urlencode({"validation_id": validation_id}), status_code=303)


@app.get("/validation/status.json")
def validation_status_endpoint(validation_id: str = Query(...)):
    status = validation_status(validation_id)
    if not status:
        raise HTTPException(status_code=404, detail="Validation run not found.")
    return status


@app.post("/validation/stop")
def validation_stop(validation_id: str = Form(...)):
    request_validation_stop(validation_id)
    return RedirectResponse(url="/validation?" + urlencode({"validation_id": validation_id}), status_code=303)


@app.post("/validation/resume")
def validation_resume(validation_id: str = Form(...), max_workers: int = Form(3)):
    try:
        resume_validation(validation_id, max_workers=max(1, min(int(max_workers), 8)))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not resume validation: {type(exc).__name__}: {exc}") from exc
    return RedirectResponse(url="/validation?" + urlencode({"validation_id": validation_id}), status_code=303)


@app.get("/validation/export")
def validation_export(validation_id: str = Query(...), kind: str = Query("results")):
    path = validation_export_file(validation_id, kind)
    if not path:
        raise HTTPException(status_code=404, detail="Validation export is not available yet.")
    media_type = "text/csv" if path.suffix.lower() == ".csv" else "application/json"
    return FileResponse(path, filename=path.name, media_type=media_type)


# -----------------------------------------------------------------------------
# Knowledge Space routes. This layer is built on top of the intelligence
# modules and does not replace any original workflow pages.
# -----------------------------------------------------------------------------

def read_csv_rows(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(dict(row))
            if limit is not None and len(rows) >= limit:
                break
    return rows


def knowledge_counts(run_dir: Optional[Path]) -> Dict[str, int]:
    if not run_dir:
        return {"vectors": 0, "clusters": 0, "priority": 0, "matrix": 0}
    outputs = run_dir / "outputs"
    vectors = read_json(outputs / KNOWLEDGE_FILES["vectors_json"], default=[])
    clusters = read_json(outputs / KNOWLEDGE_FILES["clusters_json"], default=[])
    priority = read_csv_rows(outputs / KNOWLEDGE_FILES["priority_csv"])
    matrix = read_csv_rows(outputs / KNOWLEDGE_FILES["evidence_matrix_csv"])
    return {
        "vectors": len(vectors) if isinstance(vectors, list) else 0,
        "clusters": len(clusters) if isinstance(clusters, list) else 0,
        "priority": len(priority),
        "matrix": len(matrix),
    }


def knowledge_context(request: Request, active_tab: str, subproject: Optional[str], run_id: Optional[str]) -> Dict[str, Any]:
    selected_project, selected_run = get_selected_run(subproject, run_id)
    ctx = common_context(request, active_tab)
    ctx.update({
        "selected_project": selected_project,
        "selected_run": selected_run,
        "runs": get_runs(selected_project) if selected_project else [],
        "knowledge_counts": knowledge_counts(selected_run),
        "knowledge_summary": read_text(selected_run / "outputs" / KNOWLEDGE_FILES["summary_md"]) if selected_run else "",
    })
    return ctx


# -----------------------------------------------------------------------------
# Structured Evidence Map
# -----------------------------------------------------------------------------
# This map is deliberately additive. It reads the Structured Evidence output
# produced by the Intelligence layer and never changes, filters, or suppresses
# any existing Intelligence or Knowledge Space rows.

STRUCTURED_EVIDENCE_MAP_MODES: Dict[str, Dict[str, Any]] = {
    "exposure_health": {
        "label": "Exposure → Health Impact",
        "left_label": "Exposure",
        "right_label": "Health impact",
        "left_fields": ("exposure_l1",),
        "right_fields": ("health_impact_l1",),
    },
    "exposure_health_detail": {
        "label": "Detailed Exposure → Detailed Health Impact",
        "left_label": "Exposure",
        "right_label": "Health impact",
        "left_fields": ("exposure_l2", "exposure_l1"),
        "right_fields": ("health_impact_l3", "health_impact_l2", "health_impact_l1"),
    },
    "exposure_geography": {
        "label": "Exposure → Geography",
        "left_label": "Exposure",
        "right_label": "Geography",
        "left_fields": ("exposure_l2", "exposure_l1"),
        "right_fields": ("geography_l2", "geography_l1"),
    },
    "health_geography": {
        "label": "Health Impact → Geography",
        "left_label": "Health impact",
        "right_label": "Geography",
        "left_fields": ("health_impact_l3", "health_impact_l2", "health_impact_l1"),
        "right_fields": ("geography_l2", "geography_l1"),
    },
    "exposure_special": {
        "label": "Exposure → Special Topic",
        "left_label": "Exposure",
        "right_label": "Special topic",
        "left_fields": ("exposure_l2", "exposure_l1"),
        "right_fields": ("special_topic_l2", "special_topic_l1"),
    },
    "health_model": {
        "label": "Health Impact → Model Type",
        "left_label": "Health impact",
        "right_label": "Model type",
        "left_fields": ("health_impact_l3", "health_impact_l2", "health_impact_l1"),
        "right_fields": ("model_type",),
    },
}


def structured_evidence_module_name() -> str:
    """Resolve the generic Structured Evidence module without depending on its legacy internal key."""
    for name, spec in MODULE_SPECS.items():
        if str(spec.get("short", "")).strip() == "structured_evidence":
            return name
    return ""


def split_structured_evidence_values(value: Any) -> List[str]:
    """Split a multi-value Structured Evidence cell while dropping placeholder values."""
    values: List[str] = []
    seen: set[str] = set()
    for piece in re.split(r"\s*;\s*|\s*\|\s*", str(value or "")):
        piece = re.sub(r"\s+", " ", piece).strip()
        low = piece.lower()
        if not piece or low in {"not reported", "not extracted", "not specified", "none", "n/a", "na"}:
            continue
        if low not in seen:
            seen.add(low)
            values.append(piece)
    return values


def structured_evidence_values(row: Dict[str, Any], fields: Tuple[str, ...]) -> List[str]:
    """Use the most specific populated field in a hierarchy, falling back to its parent."""
    for field in fields:
        values = split_structured_evidence_values(row.get(field, ""))
        if values:
            return values
    return []


def build_structured_evidence_map(
    rows: List[Dict[str, Any]],
    mode: str,
    query: str = "",
    max_nodes: int = 12,
    focus_left: str = "",
    focus_right: str = "",
) -> Dict[str, Any]:
    """Build a compact bipartite evidence map from one-row-per-publication classifications."""
    mode = mode if mode in STRUCTURED_EVIDENCE_MAP_MODES else "exposure_health"
    spec = STRUCTURED_EVIDENCE_MAP_MODES[mode]
    query_terms = [part.lower() for part in re.findall(r"[^\s,;]+", query or "") if part.strip()]

    filtered_rows: List[Dict[str, Any]] = []
    edge_papers: Dict[Tuple[str, str], set[str]] = {}
    left_papers: Dict[str, set[str]] = {}
    right_papers: Dict[str, set[str]] = {}

    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        haystack = " ".join(str(v or "") for v in row.values()).lower()
        if query_terms and not all(term in haystack for term in query_terms):
            continue
        left_values = structured_evidence_values(row, spec["left_fields"])
        right_values = structured_evidence_values(row, spec["right_fields"])
        if not left_values or not right_values:
            continue
        filtered_rows.append(row)
        paper_key = str(row.get("id") or row.get("citation") or row.get("source_title") or idx)
        for left in left_values:
            left_papers.setdefault(left, set()).add(paper_key)
            for right in right_values:
                right_papers.setdefault(right, set()).add(paper_key)
                edge_papers.setdefault((left, right), set()).add(paper_key)

    left_ranked_all = sorted(left_papers.items(), key=lambda item: (-len(item[1]), item[0].lower()))
    right_ranked_all = sorted(right_papers.items(), key=lambda item: (-len(item[1]), item[0].lower()))
    # max_nodes <= 0 means show every concept. This changes only visualization;
    # all Structured Evidence rows were already retained regardless of this setting.
    left_ranked = left_ranked_all[:max_nodes] if max_nodes > 0 else left_ranked_all
    right_ranked = right_ranked_all[:max_nodes] if max_nodes > 0 else right_ranked_all
    left_keep = {name for name, _ in left_ranked}
    right_keep = {name for name, _ in right_ranked}

    visible_edges = [
        (left, right, len(papers))
        for (left, right), papers in edge_papers.items()
        if left in left_keep and right in right_keep
    ]
    visible_edges.sort(key=lambda item: (-item[2], item[0].lower(), item[1].lower()))

    left_order = [name for name, _ in left_ranked]
    right_order = [name for name, _ in right_ranked]
    row_step = 54
    top_pad = 42
    canvas_height = max(360, top_pad * 2 + row_step * max(len(left_order), len(right_order), 1))
    left_y = {name: top_pad + idx * row_step for idx, name in enumerate(left_order)}
    right_y = {name: top_pad + idx * row_step for idx, name in enumerate(right_order)}
    max_edge_count = max((count for _, _, count in visible_edges), default=1)

    left_nodes = [
        {"label": name, "count": len(papers), "y": left_y[name]}
        for name, papers in left_ranked
    ]
    right_nodes = [
        {"label": name, "count": len(papers), "y": right_y[name]}
        for name, papers in right_ranked
    ]

    edges: List[Dict[str, Any]] = []
    for left, right, count in visible_edges:
        selected = bool(
            (not focus_left or left == focus_left)
            and (not focus_right or right == focus_right)
            and (focus_left or focus_right)
        )
        edges.append({
            "left": left,
            "right": right,
            "count": count,
            "y1": left_y[left],
            "y2": right_y[right],
            "stroke_width": round(1.4 + 11.0 * count / max_edge_count, 2),
            "selected": selected,
        })

    focus_rows: List[Dict[str, Any]] = []
    for row in filtered_rows:
        left_values = structured_evidence_values(row, spec["left_fields"])
        right_values = structured_evidence_values(row, spec["right_fields"])
        if focus_left and focus_left not in left_values:
            continue
        if focus_right and focus_right not in right_values:
            continue
        focus_rows.append(row)

    top_relationships = [
        {"left": left, "right": right, "count": count}
        for left, right, count in visible_edges[:30]
    ]

    return {
        "mode": mode,
        "mode_spec": spec,
        "left_nodes": left_nodes,
        "right_nodes": right_nodes,
        "edges": edges,
        "top_relationships": top_relationships,
        "filtered_rows": filtered_rows,
        "focus_rows": focus_rows,
        "canvas_height": canvas_height,
        "publication_count": len(filtered_rows),
        "relationship_count": len(edge_papers),
        "left_concept_count": len(left_papers),
        "right_concept_count": len(right_papers),
    }


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_dashboard(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    ctx = knowledge_context(request, "knowledge", subproject, run_id)
    selected_run = ctx.get("selected_run")
    clusters = read_json(selected_run / "outputs" / KNOWLEDGE_FILES["clusters_json"], default=[]) if selected_run else []
    priority = read_csv_rows(selected_run / "outputs" / KNOWLEDGE_FILES["priority_csv"], limit=8) if selected_run else []
    ctx.update({
        "clusters": clusters if isinstance(clusters, list) else [],
        "priority_rows": priority,
    })
    return templates.TemplateResponse(request, "knowledge_dashboard.html", ctx)


@app.post("/knowledge/generate", response_class=HTMLResponse)
def knowledge_generate(request: Request, subproject: str = Form(...), run_id: str = Form(...)):
    run_dir = ROOT_DIR / "subprojects" / subproject / "runs" / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    counts = generate_knowledge_space_outputs(run_dir)
    ctx = knowledge_context(request, "knowledge", subproject, run_id)
    clusters = read_json(run_dir / "outputs" / KNOWLEDGE_FILES["clusters_json"], default=[])
    priority = read_csv_rows(run_dir / "outputs" / KNOWLEDGE_FILES["priority_csv"], limit=8)
    ctx.update({"generated_counts": counts, "clusters": clusters if isinstance(clusters, list) else [], "priority_rows": priority})
    return templates.TemplateResponse(request, "knowledge_dashboard.html", ctx)



TOPIC_LENS_STOPWORDS = {
    "", "not specified", "not extracted", "mixed environmental health topic", "environmental health",
    "health", "exposure", "exposures", "outcome", "outcomes", "association", "associations",
    "study", "analysis", "model", "models", "population", "cohort", "participants", "records",
}


def normalize_topic_lens_label(term: str) -> str:
    term = clean_title_text(str(term or "")).strip()
    term = re.sub(r"\s+", " ", term)
    # Keep common scientific capitalization readable.
    replacements = {
        "pm2.5": "PM2.5",
        "pm10": "PM10",
        "pfas": "PFAS",
        "pfoa": "PFOA",
        "pfos": "PFOS",
        "no2": "NO2",
        "o3": "O3",
        "nitrogen dioxide (no2)": "nitrogen dioxide (NO2)",
        "ozone (o3)": "ozone (O3)",
    }
    return replacements.get(term.lower(), term)


def topic_lens_query(label: str) -> str:
    label = normalize_topic_lens_label(label)
    if "(" in label and ")" in label:
        # Query with both plain name and abbreviation when present.
        return re.sub(r"[()]", " ", label).strip()
    return label


def split_topic_terms(value: Any) -> List[str]:
    terms: List[str] = []
    for piece in re.split(r";|,|\|", str(value or "")):
        piece = normalize_topic_lens_label(piece.strip())
        if not piece:
            continue
        if piece.lower() in TOPIC_LENS_STOPWORDS:
            continue
        terms.append(piece)
    return terms



def dynamic_topic_lenses(points: List[Dict[str, Any]], clusters: List[Dict[str, Any]], max_lenses: int = 5) -> List[Dict[str, str]]:
    """Build up to 5 Knowledge Map topic buttons from this run, not hard-coded topics."""
    counts: Counter[str] = Counter()
    examples: Dict[str, str] = {}

    def add(term: str, weight: int, source: str = "") -> None:
        label = normalize_topic_lens_label(term)
        low = label.lower()
        if not label or low in TOPIC_LENS_STOPWORDS or len(label) < 3:
            return
        # Avoid very long labels as buttons.
        if len(label) > 42:
            return
        counts[label] += weight
        examples.setdefault(label, source)

    if isinstance(points, list):
        for point in points:
            title = str(point.get("title", ""))
            for term in split_topic_terms(point.get("exposures", "")):
                add(term, 4, title)
            for term in split_topic_terms(point.get("outcomes", "")):
                add(term, 3, title)
            for term in split_topic_terms(point.get("cohorts", "")):
                add(term, 2, title)
            hay = " ".join([title, str(point.get("filter_text", ""))]).lower()
            # Dynamic support for current-search topics that are not in the older dictionaries.
            phrase_patterns = [
                ("microplastics", r"\bmicroplastics?\b"),
                ("nanoplastics", r"\bnanoplastics?\b"),
                ("plastic additives", r"\bplastic additives?\b"),
                ("electric vehicle battery", r"\b(?:electric vehicle|ev)\s+batter(?:y|ies)\b"),
                ("lithium-ion battery", r"\b(?:lithium[- ]ion|li[- ]ion)\s+batter(?:y|ies)\b"),
                ("battery recycling", r"\bbatter(?:y|ies)\s+recycling\b|\brecycling\s+(?:of\s+)?batter(?:y|ies)\b"),
                ("battery waste", r"\bbatter(?:y|ies)\s+waste\b|\bbattery\s+black\s+mass\b|\bblack\s+mass\b"),
                ("cobalt", r"\bcobalt\b"),
                ("nickel", r"\bnickel\b"),
                ("lithium", r"\blithium\b"),
                ("manganese", r"\bmanganese\b"),
                ("battery fire emissions", r"\bbatter(?:y|ies)\s+fire|\bthermal\s+runaway\b"),
                ("endocrine disruption", r"\bendocrine disruption\b"),
                ("gut microbiome", r"\bgut microbiome\b"),
                ("reproductive toxicity", r"\breproductive toxicity\b"),
                ("oxidative stress", r"\boxidative stress\b"),
            ]
            for label, pattern in phrase_patterns:
                if re.search(pattern, hay, flags=re.I):
                    add(label, 4, title)

    if isinstance(clusters, list):
        for cluster in clusters:
            theme = str(cluster.get("theme", ""))
            for term in re.split(r"\s+\+\s+|;|,", theme):
                add(term, 2, theme)

    lenses: List[Dict[str, str]] = []
    seen_queries = set()
    for label, _count in counts.most_common(max_lenses * 3):
        query = topic_lens_query(label)
        key = query.lower()
        if key in seen_queries:
            continue
        seen_queries.add(key)
        lenses.append({"label": label, "query": query, "example": examples.get(label, "")})
        if len(lenses) >= max_lenses:
            break
    return lenses


def _lens_label_for_query(query_terms: List[str]) -> str:
    """Readable lens label for the active filter."""
    if not query_terms:
        return ""
    joined = " ".join(query_terms).strip()
    # Prefer the first term for broad lenses such as pregnancy, heat, PFAS, microplastics.
    label = normalize_topic_lens_label(joined if len(query_terms) <= 2 else query_terms[0])
    return label


def _term_matches_lens(term: str, query_terms: List[str]) -> bool:
    low = str(term or "").lower()
    return bool(low and query_terms and any(q in low or low in q for q in query_terms))


def _choose_theme_parts(members: List[Dict[str, Any]], query_terms: List[str]) -> Tuple[str, List[str]]:
    exp = Counter()
    out = Counter()
    for p in members:
        for term in split_topic_terms(p.get("exposures", "")):
            exp[term] += 1
        for term in split_topic_terms(p.get("outcomes", "")):
            out[term] += 1

    lens_label = _lens_label_for_query(query_terms)
    parts: List[str] = []
    if lens_label:
        parts.append(lens_label)

    # If the lens is already one of the detected exposures/outcomes, do not repeat it.
    candidates: List[str] = []
    for term, _ in exp.most_common(4):
        if not _term_matches_lens(term, query_terms):
            candidates.append(term)
    for term, _ in out.most_common(4):
        if not _term_matches_lens(term, query_terms):
            candidates.append(term)

    if not lens_label:
        # No active lens: summarize as best exposure + best outcome.
        candidates = []
        if exp:
            candidates.append(exp.most_common(1)[0][0])
        if out:
            candidates.append(out.most_common(1)[0][0])

    for term in candidates:
        if term and term not in parts:
            parts.append(term)
        if len(parts) >= 2:
            break

    if parts:
        return " + ".join(parts[:2]), parts
    return "Mixed environmental health topic", []


def _theme_key(theme: str) -> str:
    return re.sub(r"\s+", " ", str(theme or "").strip().lower())


def filtered_cluster_summary(filtered_points: List[Dict[str, Any]], clusters: List[Dict[str, Any]], query_terms: List[str]) -> List[Dict[str, Any]]:
    """Summarize visible points and aggregate duplicate lens labels.

    For a topic lens such as Pregnancy, this returns rows such as
    pregnancy + particulate matter, pregnancy + heat, pregnancy + PFAS, etc.
    Duplicate labels from separate clustering IDs are merged so the table is a
    clean lens summary instead of a list of fragmented one-paper clusters.
    """
    if not filtered_points:
        return []

    method_by_cluster = {str(c.get("cluster_id")): c.get("cluster_method", "") for c in clusters if isinstance(c, dict)} if isinstance(clusters, list) else {}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for p in filtered_points:
        grouped.setdefault(str(p.get("cluster_id")), []).append(p)

    # Build per-cluster themes first, then merge duplicate themes across clusters.
    merged: Dict[str, Dict[str, Any]] = {}
    for cid, members in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        theme, _parts = _choose_theme_parts(members, query_terms)
        key = _theme_key(theme)
        item = merged.setdefault(key, {
            "cluster_id": [],
            "theme": theme,
            "article_count": 0,
            "cluster_method": "filtered_aggregate" if query_terms else method_by_cluster.get(cid, "filtered_view"),
            "articles": [],
        })
        item["cluster_id"].append(cid)
        item["article_count"] += len(members)
        item["articles"].extend(members)

    rows: List[Dict[str, Any]] = []
    small_articles: List[Dict[str, Any]] = []
    small_cluster_ids: List[str] = []
    for item in sorted(merged.values(), key=lambda x: (-int(x.get("article_count", 0)), str(x.get("theme", "")))):
        count = int(item.get("article_count", 0) or 0)
        if query_terms and count == 1 and len(merged) > 6:
            small_articles.extend(item.get("articles", []))
            small_cluster_ids.extend(item.get("cluster_id", []))
            continue
        rows.append({
            "cluster_id": ", ".join(str(x) for x in item.get("cluster_id", [])),
            "theme": item.get("theme", "Mixed environmental health topic"),
            "article_count": count,
            "cluster_method": item.get("cluster_method", "filtered_aggregate"),
        })

    if small_articles:
        lens = _lens_label_for_query(query_terms) or "selected topic"
        rows.append({
            "cluster_id": ", ".join(str(x) for x in small_cluster_ids),
            "theme": f"other {lens}-related topics",
            "article_count": len(small_articles),
            "cluster_method": "filtered_aggregate",
        })

    return rows


def knowledge_point_evidence_url(subproject: str, run_id: str, point: Dict[str, Any]) -> str:
    """Build an Evidence Pack URL for a Knowledge Map point."""
    citation = str(point.get("citation") or point.get("article_id") or "")
    row: Dict[str, Any] = {"title": point.get("title", ""), "source_citation": citation}
    pmid = _extract_id_from_text(citation, "pmid")
    doi = _extract_id_from_text(citation, "doi")
    pmcid = _extract_id_from_text(citation, "pmcid")
    if pmid:
        row["pmid"] = pmid
    if doi:
        row["doi"] = doi
    if pmcid:
        row["pmcid"] = pmcid
    return evidence_pack_url(subproject, run_id, row=row)

@app.get("/knowledge/evidence-map", response_class=HTMLResponse)
def knowledge_evidence_map(
    request: Request,
    subproject: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    mode: str = Query("exposure_health"),
    q: str = Query(""),
    focus_left: str = Query(""),
    focus_right: str = Query(""),
    max_nodes: str = Query("20"),
    page: int = Query(1),
    per_page: int = Query(50),
):
    ctx = knowledge_context(request, "evidence_map", subproject, run_id)
    selected_project = ctx.get("selected_project") or subproject or ""
    selected_run = ctx.get("selected_run")
    module_name = structured_evidence_module_name()
    rows: List[Dict[str, Any]] = []
    if selected_run and module_name:
        rows = cached_module_rows(selected_run, module_name)

    max_nodes_raw = str(max_nodes or "20").strip().lower()
    if max_nodes_raw == "all":
        max_nodes_limit = 0
        max_nodes_choice: Any = "all"
    else:
        try:
            parsed_max_nodes = int(max_nodes_raw)
        except Exception:
            parsed_max_nodes = 20
        # Keep the selector predictable while allowing a manually supplied value
        # up to 200. The UI offers 10, 20, 30, 50, and All.
        max_nodes_limit = max(1, min(parsed_max_nodes, 200))
        max_nodes_choice = max_nodes_limit
    try:
        page = max(1, int(page or 1))
    except Exception:
        page = 1
    try:
        per_page = max(25, min(int(per_page or 50), 100))
    except Exception:
        per_page = 50

    map_data = build_structured_evidence_map(
        rows,
        mode=mode,
        query=q,
        max_nodes=max_nodes_limit,
        focus_left=focus_left,
        focus_right=focus_right,
    )
    mode = map_data["mode"]

    def _map_url(**updates: Any) -> str:
        params: Dict[str, str] = {
            "subproject": selected_project or "",
            "run_id": selected_run.name if selected_run else "",
            "mode": mode,
            "q": q or "",
            "max_nodes": str(max_nodes_choice),
            "per_page": str(per_page),
            "page": "1",
        }
        if focus_left:
            params["focus_left"] = focus_left
        if focus_right:
            params["focus_right"] = focus_right
        for key, value in updates.items():
            if value in (None, ""):
                params.pop(key, None)
            else:
                params[key] = str(value)
        return "/knowledge/evidence-map?" + urlencode(params)

    for edge in map_data["edges"]:
        edge["url"] = _map_url(focus_left=edge["left"], focus_right=edge["right"], page=1)
    for relation in map_data["top_relationships"]:
        relation["url"] = _map_url(focus_left=relation["left"], focus_right=relation["right"], page=1)
    for node in map_data["left_nodes"]:
        node["url"] = _map_url(focus_left=node["label"], focus_right="", page=1)
    for node in map_data["right_nodes"]:
        node["url"] = _map_url(focus_left="", focus_right=node["label"], page=1)

    publication_rows = list(map_data["focus_rows"])
    total_publications = len(publication_rows)
    total_pages = max(1, (total_publications + per_page - 1) // per_page) if total_publications else 1
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    publication_rows = publication_rows[start_idx:end_idx]

    if selected_run and module_name:
        publication_rows = [
            enrich_intelligence_row(row, module_name, selected_project, selected_run.name)
            for row in publication_rows
        ]

    def _page_url(new_page: int) -> str:
        return _map_url(page=max(1, new_page))

    ctx.update({
        "structured_evidence_available": bool(rows),
        "structured_evidence_module": module_name,
        "map_modes": STRUCTURED_EVIDENCE_MAP_MODES,
        "evidence_mode": mode,
        "evidence_query": q,
        "focus_left": focus_left,
        "focus_right": focus_right,
        "max_nodes": max_nodes_choice,
        "left_nodes": map_data["left_nodes"],
        "right_nodes": map_data["right_nodes"],
        "evidence_edges": map_data["edges"],
        "top_relationships": map_data["top_relationships"],
        "evidence_canvas_height": map_data["canvas_height"],
        "evidence_publication_count": map_data["publication_count"],
        "evidence_relationship_count": map_data["relationship_count"],
        "left_concept_count": map_data["left_concept_count"],
        "right_concept_count": map_data["right_concept_count"],
        "evidence_publications": publication_rows,
        "evidence_publication_total": total_publications,
        "evidence_publication_start": start_idx + 1 if total_publications else 0,
        "evidence_publication_end": min(end_idx, total_publications),
        "evidence_page": page,
        "evidence_total_pages": total_pages,
        "evidence_prev_url": _page_url(page - 1) if page > 1 else "",
        "evidence_next_url": _page_url(page + 1) if page < total_pages else "",
        "clear_focus_url": _map_url(focus_left="", focus_right="", page=1),
        "clear_all_evidence_url": "/knowledge/evidence-map?" + urlencode({
            "subproject": selected_project or "",
            "run_id": selected_run.name if selected_run else "",
            "mode": mode,
            "max_nodes": str(max_nodes_choice),
        }),
    })
    return templates.TemplateResponse(request, "evidence_map.html", ctx)


@app.get("/knowledge/map", response_class=HTMLResponse)
def knowledge_map(
    request: Request,
    subproject: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    q: str = Query(""),
    projection: str = Query("umap"),
    max_points: int = Query(3000),
    cluster: str = Query(""),
    page: int = Query(1),
    per_page: int = Query(100),
):
    ctx = knowledge_context(request, "knowledge_map", subproject, run_id)
    selected_project = ctx.get("selected_project") or subproject or ""
    selected_run = ctx.get("selected_run")
    points = read_json(selected_run / "outputs" / KNOWLEDGE_FILES["scatter_json"], default=[]) if selected_run else []
    clusters = read_json(selected_run / "outputs" / KNOWLEDGE_FILES["clusters_json"], default=[]) if selected_run else []
    projection = projection if projection in {"umap", "pca"} else "umap"
    query_terms = [term.lower().strip() for term in re.split(r"[,;]+|\s+", q or "") if term.strip()]
    cluster_filter = str(cluster or "").strip()
    selected_cluster_ids = {part.strip() for part in re.split(r"[,;]+", cluster_filter) if part.strip()}
    try:
        page = max(1, int(page or 1))
    except Exception:
        page = 1
    try:
        per_page = int(per_page or 100)
    except Exception:
        per_page = 100
    per_page = max(25, min(per_page, 250))

    filtered_points = []
    if isinstance(points, list):
        for point in points:
            haystack = " ".join([
                str(point.get("title", "")),
                str(point.get("article_id", "")),
                str(point.get("exposures", "")),
                str(point.get("outcomes", "")),
                str(point.get("statistical_methods", "")),
                str(point.get("filter_text", "")),
            ]).lower()
            if query_terms and not all(term in haystack for term in query_terms):
                continue
            x_key = f"{projection}_x"
            y_key = f"{projection}_y"
            # Backward compatibility with older files that only had x/y.
            raw_x = point.get(x_key, "")
            raw_y = point.get(y_key, "")
            # If UMAP was skipped or unavailable, fall back to PCA so the map is never blank.
            if (raw_x in (None, "") or raw_y in (None, "")) and projection != "pca":
                raw_x = point.get("pca_x", point.get("x", ""))
                raw_y = point.get("pca_y", point.get("y", ""))
            if raw_x in (None, "") or raw_y in (None, ""):
                continue
            try:
                x_val = float(raw_x)
                y_val = float(raw_y)
                x = 300 + 260 * x_val
                y = 260 - 210 * y_val
                score = float(point.get("priority_score", 0) or 0)
                r = 5 + min(10, score * 12)
                enriched_point = {**point, "map_x": x_val, "map_y": y_val, "svg_x": round(x, 2), "svg_y": round(y, 2), "radius": round(r, 2)}
                if selected_project and selected_run:
                    enriched_point["evidence_pack_url"] = knowledge_point_evidence_url(selected_project, selected_run.name, enriched_point)
                filtered_points.append(enriched_point)
            except Exception:
                continue

    # Keep the complete matching set for cluster summaries and paper drill-down.
    # The map itself can still be capped with max_points so the SVG stays responsive.
    all_matching_points = filtered_points

    if isinstance(clusters, list):
        visible_clusters = filtered_cluster_summary(all_matching_points, clusters, query_terms)
    else:
        visible_clusters = []

    if visible_clusters and selected_project and selected_run:
        for cluster_row in visible_clusters:
            cid = str(cluster_row.get("cluster_id", "")).strip()
            cluster_row["show_papers_url"] = "/knowledge/map?" + urlencode({
                "subproject": selected_project or "",
                "run_id": selected_run.name if selected_run else "",
                "q": q or "",
                "projection": projection,
                "max_points": str(max_points),
                "cluster": cid,
                "page": "1",
                "per_page": str(per_page),
            })

    if selected_cluster_ids:
        paper_points = [p for p in all_matching_points if str(p.get("cluster_id", "")).strip() in selected_cluster_ids]
    else:
        paper_points = all_matching_points

    total_filtered = len(paper_points)
    map_points = paper_points
    if max_points and max_points > 0 and len(map_points) > max_points:
        map_points = map_points[:max_points]

    # Keep the page responsive: never render every related paper at once.
    # The default view and selected-cluster view both use pagination.
    related_papers_all = sorted(paper_points, key=lambda p: (str(p.get("cluster_id", "")), str(p.get("title", ""))))
    total_related_papers = len(related_papers_all)
    total_pages = max(1, (total_related_papers + per_page - 1) // per_page) if total_related_papers else 1
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    related_papers = related_papers_all[start_idx:end_idx]

    def _knowledge_page_url(new_page: int) -> str:
        params = {
            "subproject": selected_project or "",
            "run_id": selected_run.name if selected_run else "",
            "q": q or "",
            "projection": projection,
            "max_points": str(max_points),
            "page": str(max(1, new_page)),
            "per_page": str(per_page),
        }
        if cluster_filter:
            params["cluster"] = cluster_filter
        return "/knowledge/map?" + urlencode(params)

    clear_cluster_url = "/knowledge/map?" + urlencode({
        "subproject": selected_project or "",
        "run_id": selected_run.name if selected_run else "",
        "q": q or "",
        "projection": projection,
        "max_points": str(max_points),
        "page": "1",
        "per_page": str(per_page),
    })
    clear_all_url = "/knowledge/map?" + urlencode({
        "subproject": selected_project or "",
        "run_id": selected_run.name if selected_run else "",
        "projection": projection,
        "max_points": str(max_points),
        "page": "1",
        "per_page": str(per_page),
    })

    topic_lenses = dynamic_topic_lenses(points if isinstance(points, list) else [], clusters if isinstance(clusters, list) else [], max_lenses=5)
    for lens in topic_lenses:
        lens["url"] = "/knowledge/map?" + urlencode({
            "subproject": selected_project or "",
            "run_id": selected_run.name if selected_run else "",
            "q": lens.get("query", ""),
            "projection": projection,
            "max_points": str(max_points),
        })

    ctx.update({
        "points": map_points,
        "clusters": visible_clusters if isinstance(visible_clusters, list) else [],
        "related_papers": related_papers,
        "topic_lenses": topic_lenses,
        "map_query": q,
        "projection": projection,
        "max_points": max_points,
        "cluster_filter": cluster_filter,
        "selected_cluster_ids": selected_cluster_ids,
        "total_related_papers": total_related_papers,
        "related_paper_start": start_idx + 1 if total_related_papers else 0,
        "related_paper_end": min(end_idx, total_related_papers),
        "paper_page": page,
        "paper_per_page": per_page,
        "paper_total_pages": total_pages,
        "paper_prev_url": _knowledge_page_url(page - 1) if page > 1 else "",
        "paper_next_url": _knowledge_page_url(page + 1) if page < total_pages else "",
        "clear_cluster_url": clear_cluster_url,
        "clear_all_url": clear_all_url,
        "total_filtered_points": total_filtered,
        "shown_points": len(map_points),
    })
    return templates.TemplateResponse(request, "knowledge_map.html", ctx)


@app.get("/knowledge/variables", response_class=HTMLResponse)
def knowledge_variables(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None), status: str = Query("all")):
    # Auto Variables were removed from Knowledge Space in this build.
    params = {k: v for k, v in {"subproject": subproject, "run_id": run_id}.items() if v}
    suffix = "?" + urlencode(params) if params else ""
    return RedirectResponse(f"/knowledge{suffix}", status_code=303)


@app.post("/knowledge/variables/update")
def knowledge_variables_update(
    subproject: str = Form(...),
    run_id: str = Form(...),
    variable_slug: str = Form(...),
    status: str = Form(...),
    notes: str = Form(""),
):
    # Auto Variables were removed from Knowledge Space in this build.
    return RedirectResponse(f"/knowledge?{urlencode({'subproject': subproject, 'run_id': run_id})}", status_code=303)


@app.get("/knowledge/priority", response_class=HTMLResponse)
def knowledge_priority(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    ctx = knowledge_context(request, "knowledge_priority", subproject, run_id)
    selected_run = ctx.get("selected_run")
    priority = read_csv_rows(selected_run / "outputs" / KNOWLEDGE_FILES["priority_csv"]) if selected_run else []
    novelty = read_csv_rows(selected_run / "outputs" / KNOWLEDGE_FILES["novelty_csv"]) if selected_run else []
    dispute = read_csv_rows(selected_run / "outputs" / KNOWLEDGE_FILES["support_dispute_csv"]) if selected_run else []
    matrix = read_csv_rows(selected_run / "outputs" / KNOWLEDGE_FILES["evidence_matrix_csv"], limit=40) if selected_run else []
    ctx.update({"priority_rows": priority, "novelty_rows": novelty, "dispute_rows": dispute, "matrix_rows": matrix})
    return templates.TemplateResponse(request, "knowledge_priority.html", ctx)


@app.get("/knowledge/reports", response_class=HTMLResponse)
def knowledge_reports(request: Request, subproject: Optional[str] = Query(None), run_id: Optional[str] = Query(None)):
    ctx = knowledge_context(request, "knowledge_reports", subproject, run_id)
    selected_run = ctx.get("selected_run")
    files = []
    if selected_run:
        outputs = selected_run / "outputs"
        wanted = {name for key, name in KNOWLEDGE_FILES.items() if key not in {"auto_variables_json", "auto_variables_csv"}}
        if outputs.exists():
            for file in sorted(outputs.iterdir()):
                if file.name in wanted:
                    rel = file.resolve().relative_to(BASE_DIR)
                    files.append({
                        "name": file.name,
                        "size": f"{file.stat().st_size:,} bytes",
                        "url": "/download?" + urlencode({"path": str(rel)}),
                        "preview_url": "/preview?" + urlencode({"path": str(rel)}),
                    })
    ctx.update({"knowledge_files": files})
    return templates.TemplateResponse(request, "knowledge_reports.html", ctx)
