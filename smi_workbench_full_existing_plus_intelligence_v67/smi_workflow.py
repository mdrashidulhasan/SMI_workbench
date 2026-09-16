#!/usr/bin/env python3
"""Markdown-first SMI workflow runner."""

from __future__ import annotations

import os
import argparse
import dataclasses
import datetime as dt
import json
import re
import shutil
import time
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError as exc:
    raise SystemExit("Missing dependency: requests. Install with: pip install requests") from exc

try:
    import yaml
except ImportError as exc:
    raise SystemExit("Missing dependency: PyYAML. Install with: pip install pyyaml") from exc

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

try:
    from smi_intelligence import DEFAULT_MODULES, generate_intelligence_outputs
except ImportError:
    DEFAULT_MODULES = []
    generate_intelligence_outputs = None

try:
    from smi_knowledge_space import generate_knowledge_space_outputs
except ImportError:
    generate_knowledge_space_outputs = None

DEFAULT_API_BASE_URL = "https://dev.posit.niehs.nih.gov/et/api"


@dataclasses.dataclass
class WorkflowContext:
    root_dir: Path
    subproject: str
    run_id: str
    api_base_url: str
    config: Dict[str, Any]

    @property
    def subproject_dir(self) -> Path:
        return self.root_dir / "subprojects" / self.subproject

    @property
    def run_dir(self) -> Path:
        return self.subproject_dir / "runs" / self.run_id

    @property
    def outputs_dir(self) -> Path:
        return self.run_dir / "outputs"

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def status_file(self) -> Path:
        return self.run_dir / "status.md"

    @property
    def approvals_file(self) -> Path:
        return self.run_dir / "approvals.md"

    @property
    def metadata_file(self) -> Path:
        return self.run_dir / "metadata.json"


class SMIAPIClient:
    def __init__(self, base_url: str = DEFAULT_API_BASE_URL, timeout: int = 900):
        self.base_url = base_url.rstrip("/")
        self.timeout = int(os.environ.get("SMI_API_TIMEOUT", timeout))

    def literature_search(self, query: str, database: List[str], start_date: Optional[str] = None, end_date: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        payload = {"query": query, "database": database}
        if start_date:
            payload["start_date"] = start_date
        if end_date:
            payload["end_date"] = end_date
        if limit is not None:
            # Different API versions have used different names for this control.
            # Send the common aliases so the server can honor whichever one it
            # supports, then the workflow also enforces the limit locally below.
            payload["limit"] = limit
            payload["max_results"] = limit
            payload["max_records"] = limit
            payload["number_of_records"] = limit
        return self._post("/search", payload)

    def deduplicate(self, references_id: str, limit: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"references_id": references_id}
        if limit is not None:
            # Some deployments honor different aliases for limiting the reference set.
            # Sending all common names helps make the search limit a hard downstream cap.
            payload["limit"] = limit
            payload["max_results"] = limit
            payload["max_records"] = limit
            payload["number_of_records"] = limit
        return self._post("/deduplication", payload)

    def ai_screen(
        self,
        references_id: Optional[str] = None,
        exclusion_criteria: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        references: Optional[List[Dict[str, Any]]] = None,
        start_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "exclusion_criteria": exclusion_criteria or {},
        }

        if references_id:
            payload["references_id"] = references_id
        if references:
            # v30: chunked AI screening can send a concrete list of citation
            # records rather than asking the API to process one large server-side
            # reference set. Keep payloads compact but include abstract/title
            # because exclusion criteria may need them.
            payload["references"] = slim_screening_references(references)
        if start_index is not None:
            # Different API deployments have used different paging names.
            payload["start_index"] = start_index
            payload["offset"] = start_index
            payload["start"] = start_index

        if limit is not None:
            # Mirror all common limit aliases.
            payload["limit"] = limit
            payload["max_results"] = limit
            payload["max_records"] = limit
            payload["number_of_records"] = limit
            payload["page_size"] = limit

        return self._post("/ai-screening", payload)

    def retrieve_records(
        self,
        result_id: str,
        result_type: str = "screened",
        start_index: int = 0,
        number_of_records: Optional[int] = 100,
    ) -> Dict[str, Any]:
        params = {
            "id": result_id,
            "type": result_type,
            "start_index": start_index,
        }
        if number_of_records is not None:
            params["number_of_records"] = number_of_records

        return self._get("/results", params=params)


    def download_pdf(
        self,
        references_id: Optional[str] = None,
        references: Optional[List[Dict[str, Any]]] = None,
        unpaywall_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}

        if references_id:
            payload["references_id"] = references_id

        if references:
            # v25: keep PDF-download payloads small. Large retrieved records can
            # include long abstracts and nested metadata; sending 100 full records
            # can make /pdf-download fail before it even starts. The PDF endpoint
            # only needs stable identifiers for acquisition, so send a compact
            # PMID/PMCID/DOI/title payload by default. Set SMI_PDF_SEND_FULL_REFERENCES=1
            # only if a deployment explicitly requires the original full record body.
            if os.environ.get("SMI_PDF_SEND_FULL_REFERENCES") == "1":
                payload["references"] = references
            else:
                payload["references"] = slim_pdf_references(references)

        if unpaywall_email:
            payload["unpaywall_email"] = unpaywall_email

        return self._post("/pdf-download", payload)
    

                
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }

        api_key = os.environ.get("CONNECT_API_KEY")
        if api_key:
            headers["Authorization"] = f"Key {api_key}"

        return headers
    def download_file(self, download_url: str, output_path: Path) -> str:
        """
        Download a file returned by the API, such as the PDF ZIP file or an
        individual paper PDF. Returns the resolved URL for logging/provenance.
        """

        if download_url.startswith("http"):
            url = download_url
        else:
            # base_url is usually:
            # https://dev.posit.niehs.nih.gov/et/api
            #
            # download_url may be:
            # /api/pdf-downloads/file.zip or /api/pdf-downloads/paper.pdf
            #
            # final URL should be rooted at the app base:
            # https://dev.posit.niehs.nih.gov/et/api/pdf-downloads/file.zip
            app_base_url = self.base_url.rsplit("/api", 1)[0]
            url = f"{app_base_url}{download_url}"

        response = requests.get(
            url,
            headers=self._headers(),
            timeout=self.timeout,
            verify="./macos-ca-bundle.pem",
            stream=True,
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            print("HTTP error:", response.status_code)
            print("URL:", response.url)
            print("Response text:")
            print(response.text[:2000])
            raise exc

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    handle.write(chunk)
        return url
    
    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(
            f"{self.base_url}{endpoint}",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
            verify="./macos-ca-bundle.pem",
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            print("HTTP error:", response.status_code)
            print("URL:", response.url)
            print("Response text:")
            print(response.text[:2000])
            raise exc

        return response.json()

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.get(
            f"{self.base_url}{endpoint}",
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
            verify="./macos-ca-bundle.pem",
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            print("HTTP error:", response.status_code)
            print("URL:", response.url)
            print("Response text:")
            print(response.text[:2000])
            raise exc

        return response.json()


def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "subproject"


def load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_log(ctx: WorkflowContext, message: str) -> None:
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    with (ctx.logs_dir / "workflow.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{utc_now()}] {message}\n")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def bounded_worker_count(name: str, default: int, max_allowed: int = 4) -> int:
    """Read a conservative worker count from the environment.

    Public/API deployments should not hammer the upstream service. This keeps
    accidental values like 50 from overwhelming the API.
    """
    value = coerce_positive_int(os.environ.get(name), default)
    return max(1, min(value, max_allowed))


def write_status(ctx: WorkflowContext, state: str, current_step: str, note: str = "") -> None:
    ctx.status_file.write_text(f"""# Workflow Status

| Field | Value |
|---|---|
| Subproject | {ctx.subproject} |
| Run ID | {ctx.run_id} |
| State | {state} |
| Current Step | {current_step} |
| Updated | {utc_now()} |

## Note

{note or "No note."}
""", encoding="utf-8")


def initialize_repo(root_dir: Path) -> None:
    for rel in ["configs", "templates", "subprojects", "archive", "logs", "scripts"]:
        (root_dir / rel).mkdir(parents=True, exist_ok=True)
    readme = root_dir / "README.md"
    if not readme.exists():
        readme.write_text("# SMI Workflow Repository\n\nMarkdown-first repository for SMI projects.\n", encoding="utf-8")


def create_subproject(root_dir: Path, name: str) -> Path:
    slug = slugify(name)
    subproject_dir = root_dir / "subprojects" / slug
    subproject_dir.mkdir(parents=True, exist_ok=True)
    (subproject_dir / "runs").mkdir(exist_ok=True)
    project_md = subproject_dir / "project.md"
    if not project_md.exists():
        project_md.write_text(f"""# {name}

## Purpose
Describe the scientific objective of this SMI subproject.

## Research Question
TBD.

## Data Sources
- PubMed

## Notes
Created: {utc_now()}
""", encoding="utf-8")
    workflow_md = subproject_dir / "workflow.md"
    if not workflow_md.exists():
        workflow_md.write_text(DEFAULT_WORKFLOW_MD, encoding="utf-8")
    return subproject_dir


def create_run(root_dir: Path, subproject: str, config_path: Path, api_base_url: str) -> WorkflowContext:
    run_id = f"run-{dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    config = load_yaml(config_path)
    ctx = WorkflowContext(root_dir=root_dir, subproject=subproject, run_id=run_id, api_base_url=api_base_url, config=config)
    ctx.outputs_dir.mkdir(parents=True, exist_ok=True)
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    save_json(ctx.metadata_file, {"subproject": subproject, "run_id": run_id, "created_at": utc_now(), "config_path": str(config_path), "api_base_url": api_base_url, "state": "created", "artifacts": {}})
    ctx.approvals_file.write_text(f"""# Human Approvals

Run ID: `{run_id}`

## Approval Format

```text
APPROVED: <checkpoint-name>
Reviewer: <name>
Date: YYYY-MM-DD
Comments: <comments>
```

## Approvals

""", encoding="utf-8")
    write_status(ctx, "created", "initialize", "Run directory created.")
    append_log(ctx, "Created workflow run.")
    return ctx


def update_metadata(ctx: WorkflowContext, key: str, value: Any) -> None:
    metadata = load_json(ctx.metadata_file, default={})
    artifacts = metadata.setdefault("artifacts", {})
    artifacts[key] = value
    metadata["updated_at"] = utc_now()
    save_json(ctx.metadata_file, metadata)


def require_approval(ctx: WorkflowContext, checkpoint: str) -> bool:
    text = ctx.approvals_file.read_text(encoding="utf-8") if ctx.approvals_file.exists() else ""
    return f"APPROVED: {checkpoint}" in text


def find_response_id(result: Any, possible_keys: List[str]) -> Optional[str]:
    """
    Find an ID in an API response.

    Handles:
    - dict responses
    - list responses
    - nested payload/data/results/records structures
    """

    if result is None:
        return None

    if isinstance(result, dict):
        for key in possible_keys:
            value = result.get(key)
            if value:
                return str(value)

        for nested_key in ["payload", "data", "result", "results", "records", "items"]:
            nested_value = result.get(nested_key)
            found = find_response_id(nested_value, possible_keys)
            if found:
                return found

    if isinstance(result, list):
        for item in result:
            found = find_response_id(item, possible_keys)
            if found:
                return found

    return None

def extract_records_from_response(response: Any) -> List[Dict[str, Any]]:
    """
    Extract a list of records from different possible API response shapes.
    Handles:
    - direct list
    - {"records": [...]}
    - {"results": [...]}
    - {"data": [...]}
    - {"items": [...]}
    - {"payload": [...]}
    - {"payload": {"records": [...]}}
    """

    if isinstance(response, list):
        return [x for x in response if isinstance(x, dict)]

    if not isinstance(response, dict):
        return []

    for key in ["records", "results", "data", "items"]:
        value = response.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    payload = response.get("payload")

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ["records", "results", "data", "items"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

    return []


def get_first_record_value(record: Dict[str, Any], possible_keys: List[str]) -> Optional[str]:
    """
    Find the first non-empty value from a record using possible field names.
    """

    for key in possible_keys:
        value = record.get(key)
        if value:
            return str(value).strip()

    lower_map = {str(k).lower(): v for k, v in record.items()}

    for key in possible_keys:
        value = lower_map.get(key.lower())
        if value:
            return str(value).strip()

    return None


def should_attempt_pdf_download(record: Dict[str, Any]) -> bool:
    """
    Return True if a record has at least a DOI or PMCID.
    """

    doi = get_first_record_value(
        record,
        ["doi", "DOI", "article_doi", "digital_object_identifier"],
    )

    pmcid = get_first_record_value(
        record,
        ["pmcid", "PMCID", "pmc_id", "pmc"],
    )

    return bool(doi or pmcid)

def use_local_fallback(ctx: WorkflowContext) -> bool:
    """Return True only for explicit local/demo runs.

    Earlier packages silently fell back to bundled Demo records whenever the
    external API timed out or CONNECT_API_KEY was missing. That made live runs
    look successful while showing fake/demo citations and no original PDFs.

    v10 changes the default: normal runs are live API runs. Demo/fallback data is
    used only when the user explicitly opts in with BOTH variables below or when
    the config explicitly sets local_demo_mode: true.

        SMI_DEMO_MODE=1
        SMI_ALLOW_DEMO_RUNS=1
    """
    if ctx.config.get("local_demo_mode") is True:
        return True
    return os.environ.get("SMI_DEMO_MODE") == "1" and os.environ.get("SMI_ALLOW_DEMO_RUNS") == "1"


def require_live_api_key(ctx: WorkflowContext, step: str) -> None:
    """Fail clearly instead of falling back to demo records."""
    if use_local_fallback(ctx):
        return
    if not os.environ.get("CONNECT_API_KEY"):
        message = (
            "CONNECT_API_KEY is not set. This run is in live API mode and will not "
            "use demo records automatically. Set CONNECT_API_KEY for original API "
            "records/PDFs, or explicitly enable demo mode with SMI_DEMO_MODE=1 and "
            "SMI_ALLOW_DEMO_RUNS=1."
        )
        write_status(ctx, "failed", step, message)
        append_log(ctx, message)
        raise RuntimeError(message)


def fail_stage(ctx: WorkflowContext, step: str, exc: Exception) -> None:
    message = f"{step} failed: {type(exc).__name__}: {exc}"
    write_status(ctx, "failed", step, message)
    append_log(ctx, message)

def config_limit(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Normalize config limits. None/blank/0 mean all records/no limit."""
    if value is None:
        return None
    if value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else None


# Search text sometimes arrives already mojibaked (for example, UTF-8 bytes
# decoded as Windows-1252/Latin-1: ``EspÃ­rito`` instead of ``Espírito``).
# Repair only when a round-trip clearly reduces mojibake markers; valid Unicode
# is otherwise left untouched. This is intentionally limited to the search-query
# handoff and does not rewrite publication metadata or downstream evidence text.
_MOJIBAKE_HINTS = (
    "Ã", "Â", "â€", "â€™", "â€œ", "â€", "â€“", "â€”", "ðŸ", "ï¿½", "�",
)


def _mojibake_score(value: str) -> int:
    text = str(value or "")
    score = sum(text.count(marker) for marker in _MOJIBAKE_HINTS)
    score += sum(1 for ch in text if 0x80 <= ord(ch) <= 0x9F)
    return score


def repair_search_query_text(value: Any) -> str:
    """Conservatively repair common UTF-8 mojibake in a literature query.

    Examples repaired:
      EspÃ­rito -> Espírito
      SÃ£o -> São
      MÃ¼ller -> Müller

    Proper Unicode and ordinary ASCII/Boolean search syntax are preserved.
    Up to three passes handle the occasional double-encoded string.
    """
    current = unicodedata.normalize("NFC", str(value or ""))

    for _ in range(3):
        current_score = _mojibake_score(current)
        if current_score == 0:
            break

        candidates = [current]
        for encoding in ("cp1252", "latin-1"):
            try:
                candidate = current.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            candidates.append(unicodedata.normalize("NFC", candidate))

        best = min(candidates, key=lambda s: (_mojibake_score(s), len(s)))
        if _mojibake_score(best) >= current_score or best == current:
            break
        current = best

    return current


def is_title_like_search_query(value: Any) -> bool:
    """Return True only for plain/title-like searches safe for recall fallbacks.

    Structured Boolean/PubMed-style queries are intentionally excluded so a
    zero-result fallback cannot silently broaden or alter a scientific search.
    """
    text = str(value or "").strip()
    if not text:
        return False

    # PubMed field tags, explicit Boolean logic, grouping, wildcards, and other
    # structured syntax indicate a deliberate search expression rather than a
    # pasted publication title.
    if re.search(r"\[[^\]]+\]", text):
        return False
    # Treat explicit uppercase operators as structured Boolean syntax.  Natural
    # publication titles frequently contain lowercase words such as "and" or
    # "or"; treating those as Boolean operators prevented the safe title-recall
    # fallbacks from running for valid titles.
    if re.search(r"(^|\s)(AND|OR|NOT)(\s|$)", text):
        return False
    if any(token in text for token in ("(", ")", "*")):
        return False

    # A whole-query pair of quotation marks is common when pasting an exact
    # title and remains safe; internal/unbalanced quotes are treated as syntax.
    if '"' in text:
        if not (text.startswith('"') and text.endswith('"') and text.count('"') == 2):
            return False
        text = text[1:-1].strip()

    # Avoid applying title fallbacks to very short keyword searches.
    words = re.findall(r"\b\w+\b", text, flags=re.UNICODE)
    return len(words) >= 5


def ascii_fold_search_query(value: Any) -> str:
    """Return an ASCII-equivalent query for APIs that mishandle diacritics."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).encode(
        "ascii", "ignore"
    ).decode("ascii")


def simplify_title_search_query(value: Any) -> str:
    """Normalize punctuation/spacing without dropping title words."""
    text = str(value or "").strip()
    if text.startswith('"') and text.endswith('"') and text.count('"') == 2:
        text = text[1:-1].strip()

    # Replace punctuation with spaces while retaining letters/numbers. This is
    # intentionally conservative: no title words are removed or substituted.
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


_TITLE_RECALL_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with",
    "its", "their", "this", "that", "our", "using", "use", "based", "study", "studies", "article", "analysis",
    "approach", "association", "associations", "relationship", "relationships", "population", "cohort",
}


def distinctive_title_search_query(value: Any, max_terms: int = 9) -> str:
    """Build a short, high-specificity keyword query from a pasted title.

    This is used only after spelling/punctuation-equivalent title searches have
    returned zero.  It drops common prose/study-design words but keeps scientific
    concepts and place names, e.g. the known title
    "Maternal exposure to heat ... KwaZulu-Natal, South Africa ..." becomes
    "Maternal exposure heat miscarriage rural KwaZulu Natal South Africa".
    """
    text = simplify_title_search_query(value)
    tokens = re.findall(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", ascii_fold_search_query(text))
    kept: List[str] = []
    for token in tokens:
        low = token.lower().strip(".-")
        if not low or low in _TITLE_RECALL_STOPWORDS:
            continue
        # Avoid weak one-character tokens while preserving biomedical acronyms
        # and numeric identifiers when present.
        if len(low) == 1 and not low.isdigit():
            continue
        if token not in kept:
            kept.append(token)
        if len(kept) >= max_terms:
            break
    return " ".join(kept)


def title_prefix_search_query(value: Any) -> str:
    """Return the substantive title text before a generic colon subtitle."""
    text = str(value or "").strip().strip('"')
    if ":" not in text:
        return ""
    prefix, suffix = text.split(":", 1)
    # Only drop a suffix that looks like a generic design/method descriptor.
    if not re.search(r"(?i)\b(study|analysis|approach|review|model|methods?)\b", suffix):
        return ""
    return re.sub(r"\s+", " ", prefix).strip()


def zero_result_title_fallback_queries(value: Any) -> List[str]:
    """Build ordered conservative fallbacks for a zero-result title search.

    Structured Boolean/PubMed queries are never modified.  The final two
    variants are recall-oriented but still retain multiple distinctive title
    concepts, so they address upstream exact-title misses without turning a
    pasted title into a broad one- or two-word topic search.
    """
    query = str(value or "").strip()
    if not is_title_like_search_query(query):
        return []

    prefix = title_prefix_search_query(query)
    keyword = distinctive_title_search_query(query, max_terms=9)
    keyword_short = distinctive_title_search_query(query, max_terms=6)

    variants: List[str] = []
    for candidate in (
        ascii_fold_search_query(query),
        simplify_title_search_query(query),
        simplify_title_search_query(ascii_fold_search_query(query)),
        prefix,
        ascii_fold_search_query(prefix) if prefix else "",
        keyword,
        keyword_short,
    ):
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate and candidate != query and candidate not in variants:
            variants.append(candidate)
    return variants


def response_record_count(response: Any) -> Optional[int]:
    """Return an explicit/local record count when a response exposes one.

    ``None`` means the response shape does not tell us the count. Zero is kept
    distinct from ``None`` so the workflow can stop cleanly on a genuine
    zero-result search instead of attempting deduplication/AI screening.
    """
    if isinstance(response, list):
        return len(response)
    if not isinstance(response, dict):
        return None

    for key in (
        "recordCount", "record_count", "sourceRecordCount", "source_record_count",
        "count", "total", "total_records", "n",
    ):
        value = response.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())

    for key in ("records", "references", "items"):
        value = response.get(key)
        if isinstance(value, list):
            return len(value)

    payload = response.get("payload")
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        nested = response_record_count(payload)
        if nested is not None:
            return nested

    # ``data`` / ``results`` are sometimes lists of actual records and sometimes
    # metadata dictionaries. Only infer a count when their shape is unambiguous.
    for key in ("data", "results"):
        value = response.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            nested = response_record_count(value)
            if nested is not None:
                return nested

    return None


def mark_no_records_completion(ctx: WorkflowContext, stage: str, note: str) -> None:
    """End a zero-result run cleanly without turning it into an AI-screening error."""
    metadata = load_json(ctx.metadata_file, default={})
    metadata["state"] = "completed"
    metadata["completion_reason"] = "no_records"
    metadata["completed_at"] = utc_now()
    metadata["workflow_control"] = {
        "stop": True,
        "reason": "no_records",
        "stage": stage,
        "note": note,
    }
    save_json(ctx.metadata_file, metadata)
    write_status(ctx, "completed", stage, note)
    append_log(ctx, note)


def apply_record_limit(records: List[Dict[str, Any]], limit: Any) -> List[Dict[str, Any]]:
    parsed = config_limit(limit)
    return records if parsed is None else records[:parsed]


def effective_limit(*values: Any) -> Optional[int]:
    """Return the smallest positive limit from multiple configured limits.

    This makes the Search result limit act as a hard cap for downstream stages
    even when the external search API returns an unbounded reference set.
    """
    parsed_values = [config_limit(v) for v in values]
    parsed_values = [v for v in parsed_values if v is not None]
    return min(parsed_values) if parsed_values else None


def limit_response_records(response: Any, limit: Any) -> Any:
    """Trim records embedded in common API response shapes.

    Some SMI API deployments ignore the search limit but still return records in
    the response. This helper keeps the saved output and displayed counts aligned
    with the requested limit. When the API returns only a references_id/file_id,
    there is no local list to trim, so downstream stages are capped separately.
    """
    parsed = config_limit(limit)
    if parsed is None:
        return response

    def trim_list(items):
        if isinstance(items, list):
            return items[:parsed]
        return items

    if isinstance(response, list):
        return trim_list(response)

    if not isinstance(response, dict):
        return response

    changed = False
    result = dict(response)

    for key in ["records", "results", "data", "items", "references"]:
        if isinstance(result.get(key), list):
            original_count = len(result[key])
            result[key] = result[key][:parsed]
            changed = True
            result.setdefault("unlimited_record_count", original_count)

    payload = result.get("payload")
    if isinstance(payload, list):
        original_count = len(payload)
        result["payload"] = payload[:parsed]
        changed = True
        result.setdefault("unlimited_record_count", original_count)
    elif isinstance(payload, dict):
        payload_copy = dict(payload)
        for key in ["records", "results", "data", "items", "references"]:
            if isinstance(payload_copy.get(key), list):
                original_count = len(payload_copy[key])
                payload_copy[key] = payload_copy[key][:parsed]
                changed = True
                result.setdefault("unlimited_record_count", original_count)
        if changed:
            result["payload"] = payload_copy

    if changed:
        for key in [
            "recordCount", "record_count", "sourceRecordCount", "source_record_count",
            "count", "n", "total", "total_records", "kept", "keptCount", "kept_count",
        ]:
            if isinstance(result.get(key), int):
                result[key] = min(result[key], parsed)
        result["applied_limit"] = parsed

    return result


def stop_file(ctx: WorkflowContext) -> Path:
    return ctx.run_dir / "STOP_REQUESTED"


def check_stop_requested(ctx: WorkflowContext, step: str) -> None:
    if stop_file(ctx).exists():
        write_status(ctx, "stopped", step, "Workflow stopped by user request from the web UI.")
        append_log(ctx, f"Stopped before or during stage: {step}.")
        raise SystemExit("Workflow stopped by user request.")


def demo_records(query: str = "environmental health intelligence") -> List[Dict[str, Any]]:
    q = query or "environmental health intelligence"
    return [
        {
            "id": "demo-rec-1",
            "pmid": "90000001",
            "doi": "10.0000/demo.pfas.birth",
            "title": "PFAS exposure, pregnancy outcomes, and environmental cohort evidence",
            "authors": ["Demo A", "Demo B"],
            "journal": "Demo Environmental Health",
            "publication_date": "2026",
            "abstract": f"This demo record was generated for query {q}. A cohort study using ECHO and HHEAR data reported PFAS exposure associated with lower birth weight and pregnancy outcomes. NHANES biomonitoring and CompTox identifiers were used to support exposure interpretation.",
        },
        {
            "id": "demo-rec-2",
            "pmid": "90000002",
            "doi": "10.0000/demo.pm25.asthma",
            "title": "Geospatial PM2.5 exposure and childhood asthma",
            "authors": ["Demo C"],
            "journal": "Demo Journal of Exposure Science",
            "publication_date": "2025",
            "abstract": "Children in a cohort study had higher asthma risk associated with PM2.5 and air pollution exposure. The study linked EPA AQS, Air Quality System, AirNow, CDC Tracking, and county-level geospatial measures.",
        },
        {
            "id": "demo-rec-3",
            "pmid": "90000003",
            "doi": "10.0000/demo.heat.mortality",
            "title": "Heat, green space, and cardiovascular mortality using geospatial data",
            "authors": ["Demo D"],
            "journal": "Demo Climate and Health",
            "publication_date": "2024",
            "abstract": "A United States study used NASA Earthdata, MODIS, Landsat, NLCD, PRISM, Daymet, NOAA, USGS, EnviroAtlas, and EPA Environmental Dataset Gateway records to evaluate heat, NDVI green space, cardiovascular outcomes, and mortality.",
        },
        {
            "id": "demo-rec-4",
            "pmid": "90000004",
            "doi": "10.0000/demo.cohorts",
            "title": "Cohorts with environmental exposure data for respiratory and cardiometabolic research",
            "authors": ["Demo E"],
            "journal": "Demo Cohort Methods",
            "publication_date": "2023",
            "abstract": "NHANES, ECHO, HHEAR, the Sister Study, Nurses' Health Study, Agricultural Health Study, MESA, Framingham, Children's Health Study, and ABCD include environmental exposures, blood, urine, serum, biospecimen, biomarker, respiratory, diabetes, obesity, and cardiovascular outcomes.",
        },
        {
            "id": "demo-rec-5",
            "pmid": "90000005",
            "doi": "10.0000/demo.new.chemicals",
            "title": "New chemical marketplace signals and toxicity testing priorities",
            "authors": ["Demo F"],
            "journal": "Demo Chemical Watch",
            "publication_date": "2026",
            "abstract": "A new chemical replacement for PFAS, CAS 12345-67-8 and DTXSID7020182, was reported in consumer product use and industrial use. TSCA PMN and SNUN records, CompTox, ToxCast, TRI, and product use evidence suggest exposure potential, limited toxicity testing, and data gaps.",
        },
    ]


def make_demo_literature_result(ctx: WorkflowContext, note: str = "local demo mode") -> Dict[str, Any]:
    cfg = ctx.config.get("literature_search", {})
    records = apply_record_limit(demo_records(cfg.get("query", "")), cfg.get("limit"))
    return {
        "references_id": f"local-demo-{ctx.run_id}",
        "recordCount": len(records),
        "records": records,
        "source": "local_fallback",
        "note": note,
    }


def previous_records(ctx: WorkflowContext) -> List[Dict[str, Any]]:
    for name in ["04_retrieved_records.json", "03_ai_screening.json", "02_deduplication.json", "01_literature_search.json"]:
        data = load_json(ctx.outputs_dir / name, default={})
        records = extract_records_from_response(data)
        if records:
            return records
    return demo_records(ctx.config.get("literature_search", {}).get("query", ""))


def make_stage_result(ctx: WorkflowContext, stage: str, id_key: str, note: str = "local fallback") -> Dict[str, Any]:
    records = previous_records(ctx)
    return {
        id_key: f"local-{stage}-{ctx.run_id}",
        "references_id": f"local-{stage}-{ctx.run_id}",
        "recordCount": len(records),
        "records": records,
        "source": "local_fallback",
        "note": note,
    }


def minimal_pdf_bytes(title: str, body: str) -> bytes:
    """Small fallback PDF generator used only if ReportLab is unavailable."""
    safe_title = re.sub(r"[^A-Za-z0-9 .,:;()/_-]", " ", title)[:80]
    safe_body = re.sub(r"[^A-Za-z0-9 .,:;()/_-]", " ", body)[:1200]
    lines = [safe_title, "", safe_body]
    text = "\n".join(lines)
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\n", ") Tj T* (")
    content = f"BT /F1 12 Tf 72 740 Td 14 TL ({escaped}) Tj ET".encode("latin-1", errors="replace")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
        b"5 0 obj << /Length " + str(len(content)).encode() + b" >> stream\n" + content + b"\nendstream endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode())
    return bytes(out)


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        return "; ".join(str(x) for x in value if x is not None) or default
    return str(value)


def safe_pdf_filename(index: int, title: str) -> str:
    slug = slugify(title)[:55] or f"record-{index}"
    return f"record-{index:02d}-{slug}.pdf"


def portable_local_path(path: Path) -> str:
    """Store a path that keeps working after the project folder is moved."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except Exception:
        return str(path)


def article_pdf_bytes(record: Dict[str, Any], index: int) -> bytes:
    """Create a readable multi-page article-style PDF from a retrieved record.

    Demo/fallback mode cannot legally or technically invent publisher PDFs. Instead,
    it creates a multi-page, paper-like PDF using the retrieved citation metadata,
    abstract, and extraction signals. In live API mode, downloaded/open-access PDFs
    from the API ZIP are extracted and served directly, preserving all pages.
    """
    title = get_first_record_value(record, ["title", "article_title", "Title"]) or f"Retrieved Record {index}"
    authors = as_text(record.get("authors") or record.get("author") or record.get("Author"), "Authors not available")
    journal = get_first_record_value(record, ["journal", "journal_title", "source", "publication", "publisher", "Publisher"]) or "Journal not available"
    date = get_first_record_value(record, ["publication_date", "pub_date", "date", "year", "PublicationDate"]) or "Date not available"
    doi = get_first_record_value(record, ["doi", "DOI", "article_doi"]) or "not available"
    pmid = get_first_record_value(record, ["pmid", "PMID"]) or "not available"
    abstract = get_first_record_value(record, ["abstract", "summary", "description"]) or "No abstract was available in the retrieved record."

    if not REPORTLAB_AVAILABLE:
        body = f"Authors: {authors}\nJournal: {journal}. Date: {date}. DOI: {doi}. PMID: {pmid}\n\nAbstract\n{abstract}\n\nThis fallback PDF is limited because ReportLab is not installed."
        return minimal_pdf_bytes(title, body)

    from io import BytesIO
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.72 * inch,
        bottomMargin=0.72 * inch,
        title=title,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ArticleTitle", parent=styles["Title"], fontSize=16, leading=20, spaceAfter=10)
    h_style = ParagraphStyle("ArticleHeading", parent=styles["Heading2"], fontSize=12, leading=14, spaceBefore=10, spaceAfter=6, textColor=colors.HexColor("#0f172a"))
    body_style = ParagraphStyle("ArticleBody", parent=styles["BodyText"], fontSize=10.5, leading=14, spaceAfter=7)
    small_style = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8.5, leading=11, textColor=colors.HexColor("#475569"))
    note_style = ParagraphStyle("Note", parent=styles["BodyText"], fontSize=9, leading=12, textColor=colors.HexColor("#475569"), backColor=colors.HexColor("#f8fafc"), borderPadding=6)

    def esc(x: Any) -> str:
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    full_text = f"{title} {abstract} {authors} {journal}"
    exposure = get_first_record_value(record, ["exposure", "exposure_name", "chemical", "chemical_name"]) or infer_from_text(full_text, ["PFAS", "PM2.5", "particulate matter", "heat", "glyphosate", "lead", "bisphenol", "phthalate", "flame retardant"])
    outcome = get_first_record_value(record, ["outcome", "health_outcome", "disease"]) or infer_from_text(full_text, ["birth weight", "asthma", "mortality", "pregnancy outcomes", "cardiovascular", "neurodevelopment", "reproductive", "respiratory"])
    dataset = infer_from_text(full_text, ["NHANES", "EPA AQS", "CDC Tracking", "ECHO", "HHEAR", "CompTox", "NASA", "NOAA", "USGS", "PRISM"])
    geography = get_first_record_value(record, ["geography", "location", "region"]) or "not specified in metadata"
    design = get_first_record_value(record, ["study_design", "design", "publication_type"]) or "not specified in metadata"

    table_data = [
        ["Field", "Value"],
        ["Exposure signal", exposure or "not extracted"],
        ["Outcome signal", outcome or "not extracted"],
        ["Dataset/cohort signal", dataset or "not extracted"],
        ["Study design", design],
        ["Geography", geography],
        ["Evidence source", f"PMID {pmid}; DOI {doi}"],
    ]
    tbl = Table(table_data, colWidths=[1.65 * inch, 4.65 * inch], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#f8fafc")),
    ]))

    # Expand text so the demo PDF is truly multi-page. This helps test browser
    # preview and download behavior without depending on external publisher PDFs.
    abstract_paragraphs = [abstract]
    if len(abstract) < 900:
        abstract_paragraphs.append(
            "This generated preview repeats and expands the retrieved abstract metadata so the file behaves like a multi-page scientific article. It is intended for local testing of the SMI Workbench PDF viewer, citation table, and download links."
        )
    methods_text = (
        "The SMI workflow performed literature search, deduplication, AI-assisted screening, record retrieval, PDF acquisition, and structured intelligence extraction. "
        "The extraction layer identifies candidate exposure-health associations, research datasets, geospatial environmental datasets, environmental cohorts, and chemicals that may require toxicity testing. "
        "Each extracted item should remain reviewable, with a source record, supporting sentence or metadata field, confidence indicator, and reviewer decision. "
    )
    results_text = (
        f"For this record, the extracted exposure signal was '{exposure or 'not extracted'}' and the health outcome signal was '{outcome or 'not extracted'}'. "
        f"Dataset or cohort signals included '{dataset or 'not extracted'}'. These are machine-generated candidates and should not be treated as final scientific conclusions until reviewed. "
        "The evidence table, review queue, and intelligence reports in the app provide the human-in-the-loop validation step. "
    )
    discussion_text = (
        "For production use, replace this locally generated preview with actual open-access PDFs returned by the live PDF-download API. "
        "When actual PDFs are available in the API ZIP package, SMI Workbench extracts the individual PDF files and serves those original multi-page files for preview and download. "
        "The local demo file is intentionally multi-page so you can verify that the browser viewer, raw preview endpoint, download endpoint, and ZIP package preserve all pages. "
    )

    story = []
    story.append(Paragraph(esc(title), title_style))
    story.append(Paragraph(f"<b>Authors:</b> {esc(authors)}", body_style))
    story.append(Paragraph(f"<b>Source:</b> {esc(journal)} | <b>Date:</b> {esc(date)} | <b>PMID:</b> {esc(pmid)} | <b>DOI:</b> {esc(doi)}", body_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Local preview note", h_style))
    story.append(Paragraph("This is a multi-page local article-style preview generated from the retrieved record. It is not a publisher copy. In live API mode, actual downloaded PDFs are extracted from the ZIP and previewed/downloaded directly.", note_style))
    story.append(Paragraph("Abstract", h_style))
    for paragraph in abstract_paragraphs:
        story.append(Paragraph(esc(paragraph), body_style))
    story.append(Paragraph("Structured extraction preview", h_style))
    story.append(tbl)

    story.append(PageBreak())
    story.append(Paragraph("Methods", h_style))
    for _ in range(5):
        story.append(Paragraph(esc(methods_text), body_style))
    story.append(Paragraph("Evidence extraction workflow", h_style))
    for _ in range(4):
        story.append(Paragraph(esc(results_text), body_style))

    story.append(PageBreak())
    story.append(Paragraph("Discussion and reviewer guidance", h_style))
    for _ in range(5):
        story.append(Paragraph(esc(discussion_text), body_style))
    story.append(Paragraph("Reviewer checklist", h_style))
    checklist = [
        "Confirm whether the exposure term is correct.",
        "Confirm whether the health outcome term is correct.",
        "Check whether the dataset or cohort signal is truly used in the study.",
        "Accept, edit, or reject each extracted intelligence row.",
        "Generate summaries only from reviewed or accepted rows.",
    ]
    for item in checklist:
        story.append(Paragraph(f"- {esc(item)}", body_style))
    story.append(Spacer(1, 12))
    story.append(Paragraph("References", h_style))
    story.append(Paragraph(esc(f"Retrieved record citation: {authors}. {title}. {journal}. {date}. PMID: {pmid}. DOI: {doi}."), small_style))
    story.append(Paragraph("Generated by SMI Workbench local/demo PDF generator. This file is intended to test full multi-page PDF preview and download behavior.", small_style))

    doc.build(story)
    return buffer.getvalue()

def infer_from_text(text: str, terms: List[str]) -> str:
    lower = text.lower()
    found = [term for term in terms if term.lower() in lower]
    return "; ".join(found)


def clean_file_name(name: str, fallback: str = "paper.pdf") -> str:
    """Create a safe local filename while preserving the original title when possible."""
    name = (name or fallback).replace("\\", "/").split("/")[-1].strip()
    if not name:
        name = fallback
    name = re.sub(r"[^A-Za-z0-9._ -]+", "-", name)
    name = re.sub(r"\s+", " ", name).strip(" ._- ") or fallback
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:180]


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def is_probably_pdf(path: Path) -> bool:
    """Lightweight check to avoid serving an HTML error page as a PDF."""
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except Exception:
        return False


def pdf_text_probe(path: Path, max_pages: int = 2, max_chars: int = 12000) -> str:
    """Return a small text/metadata probe for package-first PDF matching.

    The workflow keeps the run-level ZIP package, extracts the PDFs, and uses
    only high-confidence evidence (PMID, PMCID, DOI, or strong title overlap)
    before assigning an individual PDF to a citation row. This helper is
    intentionally optional: if a PDF parser is unavailable, it falls back to a
    lightweight byte scan and never blocks the workflow.
    """
    pieces: List[str] = []
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        meta = getattr(reader, "metadata", None)
        if meta:
            for key in ["/Title", "/Subject", "/Author", "/Keywords"]:
                value = meta.get(key) if hasattr(meta, "get") else None
                if value:
                    pieces.append(str(value))
        for page in list(reader.pages[:max_pages]):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text:
                pieces.append(text)
            if sum(len(x) for x in pieces) >= max_chars:
                break
    except Exception:
        # Many PDFs contain identifiers/title fragments in lightly encoded bytes.
        # This is not full OCR and is used only as an additional conservative
        # matching signal, never as a forced row-order assignment.
        try:
            raw = path.read_bytes()[:800000]
            text = raw.decode("latin-1", errors="ignore")
            text = re.sub(r"[^A-Za-z0-9:/._()\- +,;]+", " ", text)
            pieces.append(text)
        except Exception:
            return ""
    text = re.sub(r"\s+", " ", " ".join(pieces)).strip()
    return text[:max_chars]


def extract_pdf_entries_from_zip(
    zip_path: Path,
    pdf_dir: Path,
    reference_records: Optional[List[Dict[str, Any]]] = None,
    allow_order_mapping: bool = False,
) -> List[Dict[str, Any]]:
    """Extract original individual PDFs from an API ZIP.

    v30 mapping policy:
    - If the ZIP came from a single requested reference, attach that reference
      PMID/PMCID/DOI/title to extracted PDFs so the citation table can map them.
    - If filenames contain identifiers, the citation-table matcher uses them.
    - Order-based mapping for multi-record ZIPs is disabled by default because it
      previously caused wrong row/PDF assignments. It can be enabled explicitly
      with SMI_PDF_ALLOW_BATCH_ORDER_MAPPING=1.
    """
    entries: List[Dict[str, Any]] = []
    if not zip_path.exists() or zip_path.suffix.lower() != ".zip":
        return entries
    refs = reference_records or []
    extract_dir = pdf_dir / "api_original_pdfs"
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            pdf_members = [m for m in zf.namelist() if m.lower().endswith(".pdf")]
            for idx, member in enumerate(pdf_members):
                original_name = clean_file_name(Path(member).name, f"paper-{len(entries) + 1}.pdf")
                target = unique_target(extract_dir / original_name)
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                if not is_probably_pdf(target):
                    target.unlink(missing_ok=True)
                    continue
                entry = {
                    "title": Path(original_name).stem,
                    "fileName": target.name,
                    "localPath": portable_local_path(target),
                    "status": "downloaded",
                    "source": "original_api_pdf_zip",
                    "is_original_pdf": True,
                    "zipSource": zip_path.name,
                    "zipMemberName": member,
                }
                probe = pdf_text_probe(target) if os.environ.get("SMI_PDF_TEXT_PROBE", "1") != "0" else ""
                if probe:
                    entry["pdfTextProbe"] = probe
                if len(refs) == 1:
                    entry = attach_pdf_reference_mapping(entry, refs[0], "single_reference_zip")
                elif allow_order_mapping and len(refs) == len(pdf_members) and idx < len(refs):
                    entry = attach_pdf_reference_mapping(entry, refs[idx], "batch_order_zip")
                entries.append(entry)
    except Exception:
        return entries
    return entries

def iter_nested_dicts(value: Any):
    """Yield dictionaries from common nested API response shapes."""
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                yield from iter_nested_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from iter_nested_dicts(item)


def first_value(data: Dict[str, Any], keys: List[str]) -> str:
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = data.get(key)
        if not value:
            value = lower.get(key.lower())
        if value:
            if isinstance(value, list):
                return "; ".join(str(x) for x in value if x)
            return str(value)
    return ""


def possible_pdf_url(entry: Dict[str, Any]) -> str:
    return first_value(entry, [
        "downloadUrl", "download_url", "pdfDownloadUrl", "pdf_download_url",
        "pdfUrl", "pdf_url", "fileUrl", "file_url", "url", "href",
    ])


def looks_like_pdf_entry(entry: Dict[str, Any]) -> bool:
    name = first_value(entry, ["fileName", "file_name", "filename", "name", "title"])
    url = possible_pdf_url(entry)
    status = first_value(entry, ["status", "downloadStatus", "download_status"]).lower()
    mime = first_value(entry, ["mimeType", "mime_type", "contentType", "content_type"]).lower()
    return (
        name.lower().endswith(".pdf")
        or ".pdf" in url.lower()
        or "pdf" in mime
        or status in {"downloaded", "success", "saved", "available"}
    )


def download_individual_api_pdfs(client: SMIAPIClient, api_result: Dict[str, Any], pdf_dir: Path) -> List[Dict[str, Any]]:
    """Download original individual paper PDFs when the API response lists per-paper files/URLs.

    This prevents the UI from falling back to generated summary PDFs when the API already returned
    original paper PDF links.
    """
    entries: List[Dict[str, Any]] = []
    originals_dir = pdf_dir / "api_original_pdfs"
    originals_dir.mkdir(parents=True, exist_ok=True)

    seen_urls = set()
    for item in iter_nested_dicts(api_result):
        if not looks_like_pdf_entry(item):
            continue
        url = possible_pdf_url(item)
        local_path = first_value(item, ["localPath", "path", "filePath"])
        if local_path:
            path = Path(local_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            if path.exists() and path.suffix.lower() == ".pdf" and is_probably_pdf(path):
                entries.append({
                    **item,
                    "fileName": path.name,
                    "localPath": portable_local_path(path),
                    "source": item.get("source") or "original_api_pdf",
                    "is_original_pdf": True,
                })
            continue
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        name = first_value(item, ["fileName", "file_name", "filename", "name", "title"])
        target = unique_target(originals_dir / clean_file_name(name, f"paper-{len(entries) + 1}.pdf"))
        try:
            resolved_url = client.download_file(url, target)
        except Exception:
            continue
        if not is_probably_pdf(target):
            target.unlink(missing_ok=True)
            continue
        entries.append({
            **item,
            "fileName": target.name,
            "localPath": portable_local_path(target),
            "downloadUrl": url,
            "resolvedDownloadUrl": resolved_url,
            "status": item.get("status") or "downloaded",
            "source": item.get("source") or "original_api_pdf_url",
            "is_original_pdf": True,
        })
    return entries



def coerce_positive_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        out = int(value)
        return out if out > 0 else default
    except Exception:
        return default


def coerce_nonnegative_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        return out if out >= 0 else default
    except Exception:
        return default


def reference_identifier(ref: Dict[str, Any]) -> str:
    for key in [
        "pmid", "PMID", "pubmedId", "pubmed_id", "doi", "DOI",
        "pmcid", "PMCID", "id", "record_id", "title", "Title"
    ]:
        value = ref.get(key) if isinstance(ref, dict) else None
        if value:
            return str(value)[:200]
    return ""


def deep_first_value(value: Any, keys: List[str]) -> str:
    """Find a value for any key name inside nested API record dictionaries."""
    wanted = {k.lower() for k in keys}
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in wanted and v not in (None, "", []):
                if isinstance(v, list):
                    return "; ".join(str(x) for x in v if x)[:1000]
                if isinstance(v, dict):
                    return json.dumps(v, ensure_ascii=False)[:1000]
                return str(v)[:1000]
        for v in value.values():
            if isinstance(v, (dict, list)):
                found = deep_first_value(v, keys)
                if found:
                    return found
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                found = deep_first_value(item, keys)
                if found:
                    return found
    return ""


def slim_pdf_reference(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Return the smallest useful PDF-acquisition record.

    This reduces failures when requesting 100+ PDFs because the API no longer
    receives full retrieved-record payloads for every paper.
    """
    if not isinstance(ref, dict):
        return {}
    pmid = deep_first_value(ref, ["pmid", "PMID", "pubmedId", "pubmed_id", "pubmed_id_value"])
    pmcid = deep_first_value(ref, ["pmcid", "PMCID", "pmcId", "pmc_id"])
    doi = deep_first_value(ref, ["doi", "DOI", "articleDoi", "article_doi"])
    title = deep_first_value(ref, ["title", "Title", "articleTitle", "article_title", "name"])
    journal = deep_first_value(ref, ["journal", "journalTitle", "journal_title", "source", "venue"])
    year = deep_first_value(ref, ["year", "publicationYear", "publication_year", "pub_year"])

    out: Dict[str, Any] = {}
    if pmid:
        out["pmid"] = pmid
        out["PMID"] = pmid
    if pmcid:
        out["pmcid"] = pmcid
        out["PMCID"] = pmcid
    if doi:
        out["doi"] = doi
        out["DOI"] = doi
    if title:
        out["title"] = title
        out["Title"] = title
    if journal:
        out["journal"] = journal
    if year:
        out["year"] = year

    # Keep a local-only trace so manifests remain readable; the API can ignore it.
    rid = reference_identifier(ref)
    if rid:
        out["source_record_id"] = rid

    # If the source record is already tiny or has no recognizable identifier,
    # fall back to the original record to avoid dropping information.
    return out if any(out.get(k) for k in ["pmid", "pmcid", "doi", "title"]) else ref


def slim_pdf_references(references: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [slim_pdf_reference(ref) for ref in references if isinstance(ref, dict)]


def slim_screening_reference(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact record suitable for chunked AI screening.

    Unlike PDF acquisition, screening benefits from title/abstract/journal/year
    plus stable identifiers. This avoids sending very large nested API records
    while preserving the fields most likely needed by exclusion criteria.
    """
    if not isinstance(ref, dict):
        return {}
    keys = [
        "pmid", "PMID", "pmcid", "PMCID", "doi", "DOI",
        "title", "Title", "article_title", "abstract", "Abstract",
        "summary", "snippet", "journal", "journal_title", "source",
        "publication_date", "pub_date", "date", "year",
        "authors", "author", "Author", "publication_type", "study_design",
    ]
    out: Dict[str, Any] = {}
    for key in keys:
        value = deep_first_value(ref, [key])
        if value:
            out[key] = value
    rid = reference_identifier(ref)
    if rid:
        out["source_record_id"] = rid
    return out if out else ref


def slim_screening_references(references: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [slim_screening_reference(ref) for ref in references if isinstance(ref, dict)]


def read_records_for_chunked_screening(
    ctx: WorkflowContext,
    client: SMIAPIClient,
    references_id: str,
    screen_limit: Optional[int],
    chunk_size: int,
) -> List[Dict[str, Any]]:
    """Get concrete citation records so AI screening can be chunked locally.

    Important behavior:
    - If screen_limit is numeric, retrieve up to that many records.
    - If screen_limit is None / ALL, retrieve all available deduplicated records
      page by page, then chunk them locally.
    - Never force ALL into one giant AI-screening API request.
    """

    def record_key(record: Dict[str, Any]) -> str:
        for key in ["pmid", "PMID", "pmcid", "PMCID", "doi", "DOI", "title", "article_title"]:
            value = record.get(key) if isinstance(record, dict) else None
            if value:
                return f"{key}:{str(value).strip().lower()}"
        try:
            return repr(sorted(record.items()))[:500]
        except Exception:
            return repr(record)[:500]

    # First use any records already saved locally. This is usually only useful
    # when the API returned embedded records instead of a reference-set ID.
    for name in ["02_deduplication.json", "01_literature_search.json"]:
        records = extract_records_from_response(load_json(ctx.outputs_dir / name, default={}))
        if records:
            if screen_limit is not None:
                records = records[:screen_limit]
            append_log(
                ctx,
                f"Chunked AI screening will use {len(records)} locally saved records from {name}."
            )
            save_json(ctx.outputs_dir / "02a_chunked_screening_input_records.json", {
                "source_result_type": "local_saved_payload",
                "source_file": name,
                "references_id": references_id,
                "requested_limit": screen_limit if screen_limit is not None else "ALL",
                "record_count": len(records),
                "records": records,
            })
            return records

    result_types = []
    for item in [
        os.environ.get("SMI_SCREENING_INPUT_RETRIEVAL_TYPE", ""),
        "deduped",
        "deduplicated",
        "references",
        "records",
        "citation",
        "citations",
        "screened",
    ]:
        item = str(item).strip()
        if item and item not in result_types:
            result_types.append(item)

    page_size = coerce_positive_int(
        os.environ.get("SMI_AI_SCREEN_INPUT_PAGE_SIZE"),
        500,
    )
    target_label = screen_limit if screen_limit is not None else "ALL"

    for result_type in result_types:
        records: List[Dict[str, Any]] = []
        seen_keys = set()
        start = 0

        append_log(
            ctx,
            f"Trying to retrieve concrete records for chunked AI screening: "
            f"result_type={result_type}, limit={target_label}, page_size={page_size}."
        )

        try:
            while True:
                check_stop_requested(ctx, "ai_screening")

                if screen_limit is not None:
                    remaining = screen_limit - len(records)
                    if remaining <= 0:
                        break
                    number_of_records = min(page_size, remaining)
                else:
                    number_of_records = page_size

                page = client.retrieve_records(
                    result_id=references_id,
                    result_type=result_type,
                    start_index=start,
                    number_of_records=number_of_records,
                )

                page_records = extract_records_from_response(page)
                if not page_records:
                    break

                new_records = []
                for record in page_records:
                    if not isinstance(record, dict):
                        continue
                    key = record_key(record)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        new_records.append(record)

                if not new_records:
                    append_log(
                        ctx,
                        f"Stopping retrieval for result_type={result_type}; "
                        "next page produced no new unique records."
                    )
                    break

                records.extend(new_records)
                append_log(
                    ctx,
                    f"Retrieved {len(records)} concrete records so far for chunked AI screening "
                    f"using result_type={result_type}."
                )

                if len(page_records) < number_of_records:
                    break

                start += len(page_records)

            if records:
                if screen_limit is not None:
                    records = records[:screen_limit]

                save_json(ctx.outputs_dir / "02a_chunked_screening_input_records.json", {
                    "source_result_type": result_type,
                    "references_id": references_id,
                    "requested_limit": target_label,
                    "record_count": len(records),
                    "records": records,
                })

                append_log(
                    ctx,
                    f"Retrieved {len(records)} concrete records for chunked AI screening "
                    f"using result_type={result_type}."
                )
                return records

        except Exception as exc:
            append_log(
                ctx,
                f"Could not retrieve chunked AI screening input with "
                f"result_type={result_type}: {type(exc).__name__}: {exc}"
            )

    return []

def ai_screen_chunk_size(cfg: Dict[str, Any], screen_limit: Optional[int]) -> int:
    default_size = 100
    size = coerce_positive_int(os.environ.get("SMI_AI_SCREEN_CHUNK_SIZE") or cfg.get("chunk_size"), default_size)
    if screen_limit is not None:
        size = max(1, min(size, screen_limit))
    return size


def build_dynamic_exclusion_criteria(query: str) -> Dict[str, str]:
    """Build conservative, query-derived criteria for AI screening.

    The AI-screening endpoint rejects an empty exclusion_criteria object.  The
    current literature-search query is therefore used as the scope definition
    instead of hard-coding an environmental, clinical, oncology, or other domain.
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


def resolve_ai_screen_exclusion_criteria(ctx: WorkflowContext, cfg: Dict[str, Any]) -> Dict[str, str]:
    """Return explicit criteria when present, otherwise generate query-derived criteria."""
    raw = cfg.get("exclusion_criteria")
    cleaned: Dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if key_text and value_text:
                cleaned[key_text] = value_text

    if cleaned:
        append_log(
            ctx,
            "AI screening is using non-empty exclusion criteria from the run config "
            f"(keys={list(cleaned.keys())}).",
        )
        return cleaned

    literature_cfg = ctx.config.get("literature_search") or {}
    query = str(literature_cfg.get("query") or "").strip()
    generated = build_dynamic_exclusion_criteria(query)
    append_log(
        ctx,
        "AI screening config contained empty/missing exclusion_criteria; generated "
        "conservative criteria dynamically from the current literature-search query "
        f"(keys={list(generated.keys())}, query_length={len(query)}).",
    )
    return generated


def ai_screen_http_error_details(exc: Exception) -> Dict[str, Any]:
    """Return useful HTTP diagnostics without assuming every exception is HTTP."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    url = str(getattr(response, "url", "") or "")

    body = ""
    if response is not None:
        try:
            body = str(response.text or "")
        except Exception:
            body = ""

    body = re.sub(r"\s+", " ", body).strip()
    return {
        "status_code": status_code,
        "url": url,
        "response_body": body[:4000],
    }


def ai_screen_error_signature(exc: Exception) -> str:
    """Create a stable signature for comparing repeated HTTP validation errors."""
    details = ai_screen_http_error_details(exc)
    body = str(details.get("response_body") or "").lower()
    body = re.sub(r"\s+", " ", body).strip()
    return f"{details.get('status_code')}|{body[:2000]}"


def ai_screen_is_bad_request(exc: Exception) -> bool:
    return ai_screen_http_error_details(exc).get("status_code") == 400


def ai_screen_is_transient(exc: Exception) -> bool:
    """Return True only for failures that are reasonable to retry."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    return ai_screen_http_error_details(exc).get("status_code") in {429, 500, 502, 503, 504}


def screening_record_label(ref: Dict[str, Any]) -> str:
    """Return a useful identifier when a single citation is rejected."""
    if not isinstance(ref, dict):
        return "unknown record"

    pmid = deep_first_value(ref, ["pmid", "PMID", "pubmedId", "pubmed_id"])
    if pmid:
        return f"PMID {pmid}"

    doi = deep_first_value(ref, ["doi", "DOI", "articleDoi", "article_doi"])
    if doi:
        return f"DOI {doi}"

    title = deep_first_value(ref, ["title", "Title", "article_title", "articleTitle"])
    if title:
        return title[:250]

    return reference_identifier(ref) or "unknown record"


def normalize_ai_screen_decision(value: Any) -> str:
    """Normalize record-level AI-screening decisions returned by the API."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if not text:
        return ""
    if text in {"relevant", "include", "included", "keep", "yes"}:
        return "Relevant"
    if text in {"not relevant", "not_relevant", "irrelevant", "exclude", "excluded", "no"}:
        return "Not relevant"
    if text in {"unclear", "uncertain", "maybe", "borderline", "unknown"}:
        return "Unclear"
    return ""


def ai_screen_record_decision(result: Any) -> str:
    """Extract the decision for a one-record /ai-screening response.

    The current endpoint returns aggregate ``decisionCounts`` even when the
    request contains exactly one record. A one-record request makes those
    aggregate counts record-addressable without relying on the opaque
    ``references_...`` id, which the endpoint does not register with /results.
    """
    if not isinstance(result, dict):
        return ""

    # Prefer an explicit record-level field if a deployment provides one.
    for key in [
        "decision", "screening_decision", "screeningDecision",
        "relevance", "relevance_decision", "relevanceDecision",
        "classification", "label",
    ]:
        decision = normalize_ai_screen_decision(result.get(key))
        if decision:
            return decision

    for record in extract_records_from_response(result):
        if not isinstance(record, dict):
            continue
        for key in [
            "decision", "screening_decision", "screeningDecision",
            "relevance", "relevance_decision", "relevanceDecision",
            "classification", "label",
        ]:
            decision = normalize_ai_screen_decision(record.get(key))
            if decision:
                return decision

    counts = result.get("decisionCounts")
    if not isinstance(counts, dict):
        counts = result.get("decision_counts")
    if isinstance(counts, dict):
        normalized_counts: Dict[str, int] = {
            "Relevant": 0,
            "Not relevant": 0,
            "Unclear": 0,
        }
        for raw_key, raw_value in counts.items():
            decision = normalize_ai_screen_decision(raw_key)
            if not decision:
                continue
            try:
                count = int(raw_value or 0)
            except (TypeError, ValueError):
                count = 0
            normalized_counts[decision] += max(0, count)

        positive = [name for name, count in normalized_counts.items() if count > 0]
        if len(positive) == 1:
            return positive[0]

    return ""


def screen_single_record_with_retries(
    ctx: WorkflowContext,
    client: SMIAPIClient,
    record: Dict[str, Any],
    exclusion_criteria: Dict[str, str],
    record_index: int,
    total_records: int,
    chunk_index: int,
    max_retries: int,
    retry_sleep_seconds: float,
) -> Dict[str, Any]:
    """Screen one concrete citation and return an auditable local decision.

    Only transient failures are retried. HTTP 400 remains immediately
    actionable and includes the server response body in workflow.log.
    """
    total_attempts = max(1, max_retries + 1)
    last_exc: Optional[Exception] = None

    for attempt in range(1, total_attempts + 1):
        try:
            response = client.ai_screen(
                references=[record],
                exclusion_criteria=exclusion_criteria,
                limit=1,
            )
            decision = ai_screen_record_decision(response)
            if not decision:
                raise RuntimeError(
                    "AI screening returned no record-level decision for a one-record request. "
                    f"response_keys={list(response.keys()) if isinstance(response, dict) else type(response).__name__}"
                )

            retained = decision in {"Relevant", "Unclear"}
            local_record = dict(record)
            local_record["ai_screening_decision"] = decision
            local_record["screening_decision"] = decision

            return {
                "record_index": record_index,
                "chunk_index": chunk_index,
                "decision": decision,
                "retained": retained,
                "record": local_record,
                "record_label": screening_record_label(record),
                "api_decision_counts": (
                    response.get("decisionCounts")
                    if isinstance(response, dict)
                    else None
                ),
                # Preserve the opaque API id only for audit/debugging. It is
                # deliberately never used as a /results id.
                "api_response_id": (
                    find_response_id(response, ["screening_id", "screened_id", "results_id", "result_id", "id", "file_id"])
                    if isinstance(response, dict)
                    else None
                ),
            }
        except Exception as exc:
            last_exc = exc
            details = ai_screen_http_error_details(exc)
            append_log(
                ctx,
                (
                    f"AI screening record {record_index}/{total_records} "
                    f"({screening_record_label(record)}) attempt {attempt}/{total_attempts} failed: "
                    f"{type(exc).__name__}: {exc}; status={details.get('status_code')}; "
                    f"url={details.get('url')}; response={details.get('response_body')!r}"
                ),
            )

            if ai_screen_is_bad_request(exc) or not ai_screen_is_transient(exc):
                raise
            if attempt < total_attempts and retry_sleep_seconds > 0:
                time.sleep(retry_sleep_seconds)

    raise last_exc or RuntimeError("Unknown record-level AI screening failure")


def build_local_screening_chunk_result(
    chunk_index: int,
    total_chunks: int,
    source_records: List[Dict[str, Any]],
    record_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the familiar per-chunk artifact with screened records embedded."""
    counts = {"Relevant": 0, "Not relevant": 0, "Unclear": 0}
    retained_records: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []

    for item in record_results:
        decision = item.get("decision")
        if decision in counts:
            counts[decision] += 1
        if item.get("retained") and isinstance(item.get("record"), dict):
            retained_records.append(item["record"])
        decisions.append({
            "record_index": item.get("record_index"),
            "record_label": item.get("record_label"),
            "decision": decision,
            "retained": bool(item.get("retained")),
            "api_response_id": item.get("api_response_id"),
        })

    return {
        "recordCount": len(source_records),
        "sourceRecordCount": len(source_records),
        "decisionCounts": counts,
        "chunk_index": chunk_index,
        "chunk_label": f"AI screening chunk {chunk_index}/{total_chunks} ({len(source_records)} records)",
        "chunk_size": len(source_records),
        "requested_reference_count": len(source_records),
        "processed_record_count": len(source_records),
        "retained_record_count": len(retained_records),
        "excluded_record_count": counts["Not relevant"],
        "unclear_record_count": counts["Unclear"],
        "source": "local_record_level_ai_screening",
        "screening_mode": "record_level_local_decisions",
        "records": retained_records,
        "decisions": decisions,
    }


def call_ai_screen_chunk(
    ctx: WorkflowContext,
    client: SMIAPIClient,
    chunk_refs: List[Dict[str, Any]],
    exclusion_criteria: Dict[str, str],
    chunk_label: str,
    chunk_index: int,
    max_retries: int,
    retry_sleep_seconds: float,
) -> Dict[str, Any]:
    """Call AI screening and retry only transient failures.

    In particular, HTTP 400 is returned immediately to the caller so the
    resilient splitter can decide whether it is record-specific or shared by
    the request/payload. The API response body is copied into workflow.log.
    """
    total_attempts = max(1, max_retries + 1)
    last_exc: Optional[Exception] = None

    for attempt in range(1, total_attempts + 1):
        try:
            append_log(ctx, f"Starting {chunk_label}, attempt {attempt}/{total_attempts}.")
            result = client.ai_screen(
                references=chunk_refs,
                exclusion_criteria=exclusion_criteria,
                limit=len(chunk_refs),
            )
            result = limit_response_records(result, len(chunk_refs))
            result["chunk_index"] = chunk_index
            result["chunk_label"] = chunk_label
            result["chunk_size"] = len(chunk_refs)
            result["requested_reference_count"] = len(chunk_refs)
            result["source"] = result.get("source") or "chunked_ai_screening"
            return result
        except Exception as exc:
            last_exc = exc
            details = ai_screen_http_error_details(exc)
            append_log(
                ctx,
                (
                    f"{chunk_label} attempt {attempt} failed: {type(exc).__name__}: {exc}; "
                    f"status={details.get('status_code')}; url={details.get('url')}; "
                    f"response={details.get('response_body')!r}"
                ),
            )

            # Repeating an identical HTTP 400 cannot make the request valid.
            if ai_screen_is_bad_request(exc):
                raise

            # Unknown/permanent failures should also return immediately.
            if not ai_screen_is_transient(exc):
                raise

            if attempt < total_attempts and retry_sleep_seconds > 0:
                time.sleep(retry_sleep_seconds)

    raise last_exc or RuntimeError("Unknown AI screening failure")


def _save_ai_screen_chunk_result(
    ctx: WorkflowContext,
    chunk_index: int,
    result: Dict[str, Any],
) -> None:
    chunk_dir = ctx.outputs_dir / "ai_screen_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    save_json(
        chunk_dir / f"03_ai_screening_chunk_{chunk_index:04d}_{uuid.uuid4().hex[:6]}.json",
        result,
    )


def process_ai_screen_chunk_resilient(
    ctx: WorkflowContext,
    client: SMIAPIClient,
    chunk_refs: List[Dict[str, Any]],
    exclusion_criteria: Dict[str, str],
    chunk_label: str,
    chunk_index: int,
    min_chunk_size: int,
    max_retries: int,
    retry_sleep_seconds: float,
    known_failure: Optional[Exception] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Screen one chunk without recursively treating every HTTP 400 as size-related.

    Behavior:
    - transient failures (network, 429, selected 5xx) are retried;
    - exhausted transient failures may still split down to min_chunk_size;
    - HTTP 400 is not blindly retried;
    - a 400 is probed with two half-size requests;
    - identical 400 signatures from both halves stop recursion as a probable
      shared payload/query/API validation problem;
    - when only one half fails, the successful half is preserved and recursion
      continues only on the failing half;
    - record-specific 400s can be isolated down to one citation.
    """
    if not chunk_refs:
        return [], []

    failure: Optional[Exception] = known_failure
    if failure is None:
        try:
            result = call_ai_screen_chunk(
                ctx=ctx,
                client=client,
                chunk_refs=chunk_refs,
                exclusion_criteria=exclusion_criteria,
                chunk_label=chunk_label,
                chunk_index=chunk_index,
                max_retries=max_retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
            _save_ai_screen_chunk_result(ctx, chunk_index, result)
            return [result], []
        except Exception as exc:
            failure = exc

    assert failure is not None
    details = ai_screen_http_error_details(failure)

    # Preserve the old size-reduction behavior for transient failures after
    # their retries are exhausted. This is useful for timeout/server-pressure
    # cases and is deliberately separate from HTTP 400 validation handling.
    if ai_screen_is_transient(failure):
        if len(chunk_refs) > min_chunk_size:
            midpoint = max(1, len(chunk_refs) // 2)
            left = chunk_refs[:midpoint]
            right = chunk_refs[midpoint:]
            append_log(
                ctx,
                (
                    f"{chunk_label} exhausted transient-error retries; "
                    f"splitting into {len(left)} and {len(right)} records."
                ),
            )
            all_results: List[Dict[str, Any]] = []
            all_errors: List[Dict[str, Any]] = []
            for suffix, refs in [("A", left), ("B", right)]:
                sub_results, sub_errors = process_ai_screen_chunk_resilient(
                    ctx=ctx,
                    client=client,
                    chunk_refs=refs,
                    exclusion_criteria=exclusion_criteria,
                    chunk_label=f"{chunk_label}.{suffix} ({len(refs)} records)",
                    chunk_index=chunk_index,
                    min_chunk_size=min_chunk_size,
                    max_retries=max_retries,
                    retry_sleep_seconds=retry_sleep_seconds,
                )
                all_results.extend(sub_results)
                all_errors.extend(sub_errors)
            return all_results, all_errors

        err = {
            "chunk_index": chunk_index,
            "chunk_label": chunk_label,
            "chunk_size": len(chunk_refs),
            "error_type": type(failure).__name__,
            "error": str(failure),
            **details,
        }
        append_log(ctx, f"{chunk_label} failed after transient-error retries at chunk size {len(chunk_refs)}.")
        return [], [err]

    # Permanent errors other than 400 should not cause recursive splitting.
    if not ai_screen_is_bad_request(failure):
        err = {
            "chunk_index": chunk_index,
            "chunk_label": chunk_label,
            "chunk_size": len(chunk_refs),
            "error_type": type(failure).__name__,
            "error": str(failure),
            **details,
        }
        append_log(
            ctx,
            f"{chunk_label} failed with non-transient, non-400 error; not splitting: {type(failure).__name__}: {failure}",
        )
        return [], [err]

    parent_signature = ai_screen_error_signature(failure)

    # A single-record 400 is actionable: report the exact citation instead of
    # a vague minimum-chunk failure.
    if len(chunk_refs) == 1:
        record_label = screening_record_label(chunk_refs[0])
        err = {
            "chunk_index": chunk_index,
            "chunk_label": chunk_label,
            "chunk_size": 1,
            "error_type": "AIScreeningRecordBadRequest",
            "error": f"AI screening rejected one record: {record_label}",
            "record": record_label,
            "error_signature": parent_signature,
            **details,
        }
        append_log(
            ctx,
            (
                f"{chunk_label} isolated HTTP 400 to single record: {record_label}; "
                f"response={details.get('response_body')!r}"
            ),
        )
        return [], [err]

    midpoint = max(1, len(chunk_refs) // 2)
    left = chunk_refs[:midpoint]
    right = chunk_refs[midpoint:]
    append_log(
        ctx,
        (
            f"{chunk_label} returned HTTP 400. Probing halves once: "
            f"A={len(left)}, B={len(right)}."
        ),
    )

    probes: Dict[str, Dict[str, Any]] = {}
    for suffix, refs in [("A", left), ("B", right)]:
        sub_label = f"{chunk_label}.{suffix} ({len(refs)} records)"
        try:
            result = call_ai_screen_chunk(
                ctx=ctx,
                client=client,
                chunk_refs=refs,
                exclusion_criteria=exclusion_criteria,
                chunk_label=sub_label,
                chunk_index=chunk_index,
                max_retries=max_retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
            _save_ai_screen_chunk_result(ctx, chunk_index, result)
            probes[suffix] = {
                "status": "success",
                "refs": refs,
                "label": sub_label,
                "result": result,
            }
        except Exception as probe_exc:
            probes[suffix] = {
                "status": "error",
                "refs": refs,
                "label": sub_label,
                "exception": probe_exc,
                "signature": ai_screen_error_signature(probe_exc),
                "details": ai_screen_http_error_details(probe_exc),
            }

    left_probe = probes["A"]
    right_probe = probes["B"]

    # The parent request was rejected but both smaller requests are valid. Keep
    # the successful work; no reason to re-run those halves.
    if left_probe["status"] == "success" and right_probe["status"] == "success":
        append_log(
            ctx,
            f"{chunk_label}: both half-size probes succeeded; preserving both results.",
        )
        return [left_probe["result"], right_probe["result"]], []

    # This is the key v66 fix. If two independent halves fail with the same 400
    # validation response, recursively halving 75 -> 38 -> 19 does not diagnose
    # the problem. Surface the shared validation response immediately.
    if (
        left_probe["status"] == "error"
        and right_probe["status"] == "error"
        and ai_screen_is_bad_request(left_probe["exception"])
        and ai_screen_is_bad_request(right_probe["exception"])
        and left_probe["signature"] == right_probe["signature"]
    ):
        shared_details = left_probe["details"]
        err = {
            "chunk_index": chunk_index,
            "chunk_label": chunk_label,
            "chunk_size": len(chunk_refs),
            "error_type": "AIScreeningSharedBadRequest",
            "error": (
                "Both half-size probes returned the same HTTP 400. Recursive splitting stopped because "
                "this is likely a shared payload/query/API validation problem rather than chunk size."
            ),
            "error_signature": left_probe["signature"],
            "parent_error_signature": parent_signature,
            "left_size": len(left),
            "right_size": len(right),
            **shared_details,
        }
        append_log(
            ctx,
            (
                f"{chunk_label}: both halves returned the same HTTP 400; stopping recursion. "
                f"Probable shared request validation problem. response={shared_details.get('response_body')!r}"
            ),
        )
        return [], [err]

    all_results: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, Any]] = []

    # Successful probes are final. Only recurse into the branch that actually
    # failed, carrying its already-observed failure so we do not immediately
    # repeat the exact same request.
    for probe in [left_probe, right_probe]:
        if probe["status"] == "success":
            all_results.append(probe["result"])
            continue

        sub_results, sub_errors = process_ai_screen_chunk_resilient(
            ctx=ctx,
            client=client,
            chunk_refs=probe["refs"],
            exclusion_criteria=exclusion_criteria,
            chunk_label=probe["label"],
            chunk_index=chunk_index,
            min_chunk_size=min_chunk_size,
            max_retries=max_retries,
            retry_sleep_seconds=retry_sleep_seconds,
            known_failure=probe["exception"],
        )
        all_results.extend(sub_results)
        all_errors.extend(sub_errors)

    return all_results, all_errors

def reference_mapping_fields(ref: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(ref, dict):
        return {}
    fields = {
        "pmid": deep_first_value(ref, ["pmid", "PMID", "pubmedId", "pubmed_id"]),
        "PMID": deep_first_value(ref, ["pmid", "PMID", "pubmedId", "pubmed_id"]),
        "pmcid": deep_first_value(ref, ["pmcid", "PMCID", "pmcId", "pmc_id"]),
        "PMCID": deep_first_value(ref, ["pmcid", "PMCID", "pmcId", "pmc_id"]),
        "doi": deep_first_value(ref, ["doi", "DOI", "articleDoi", "article_doi"]),
        "DOI": deep_first_value(ref, ["doi", "DOI", "articleDoi", "article_doi"]),
        "sourceRecordTitle": deep_first_value(ref, ["title", "Title", "articleTitle", "article_title"]),
    }
    return {k: v for k, v in fields.items() if v}


def attach_pdf_reference_mapping(entry: Dict[str, Any], ref: Dict[str, Any], mapped_by: str) -> Dict[str, Any]:
    out = dict(entry)
    out.update(reference_mapping_fields(ref))
    out["mappedBy"] = mapped_by
    return out


def local_zip_entry(ctx: WorkflowContext, path_value: Any, batch_label: str = "") -> Optional[Dict[str, Any]]:
    if not path_value:
        return None
    path = Path(str(path_value))
    if not path.is_absolute():
        path = (ctx.root_dir.parent / path).resolve()
    if not path.exists() or path.suffix.lower() != ".zip":
        return None
    try:
        rel = str(path.resolve().relative_to(ctx.root_dir.parent.resolve()))
    except Exception:
        rel = str(path)
    return {
        "fileName": path.name,
        "localZipPath": portable_local_path(path),
        "relativePath": rel,
        "batchLabel": batch_label,
        "sizeBytes": path.stat().st_size,
    }


def package_zip_entries(ctx: WorkflowContext, batch_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen = set()
    for result in batch_results:
        if not isinstance(result, dict):
            continue
        entry = local_zip_entry(ctx, result.get("localZipPath"), result.get("batch_label", ""))
        if entry:
            key = entry["localZipPath"]
            if key not in seen:
                seen.add(key)
                entries.append(entry)
    return entries


def build_row_level_pdf_package(ctx: WorkflowContext, pdf_entries: List[Dict[str, Any]], package_name: str = "row_level_article_pdfs.zip") -> Optional[Dict[str, Any]]:
    """Create one run-level ZIP package from confidently mapped row-level PDFs.

    This gives users both behaviors at the same time:
    1. article-specific row-level PDF links in the citation table; and
    2. one downloadable run-level package containing those same article PDFs.

    The package is built locally from already downloaded original API PDFs, so it
    does not require a second bulk PDF API request and it does not change the
    article-to-PDF mapping logic.
    """
    if not pdf_entries:
        return None
    pdf_dir = ctx.outputs_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    package_path = pdf_dir / package_name

    used_names = set()
    added = 0
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, entry in enumerate(pdf_entries, start=1):
            if not isinstance(entry, dict):
                continue
            local_value = entry.get("localPath") or entry.get("localPdfPath")
            if not local_value:
                continue
            path = Path(str(local_value))
            if not path.is_absolute():
                candidate = (ctx.root_dir.parent / path).resolve()
                if candidate.exists():
                    path = candidate
                else:
                    path = (Path.cwd() / path).resolve()
            if not path.exists() or path.suffix.lower() != ".pdf":
                continue

            pmid = as_text(entry.get("pmid") or entry.get("PMID"), "").strip()
            pmcid = as_text(entry.get("pmcid") or entry.get("PMCID"), "").strip()
            doi = as_text(entry.get("doi") or entry.get("DOI"), "").strip()
            title = as_text(entry.get("sourceRecordTitle") or entry.get("title") or path.stem, "paper").strip()
            id_part = pmid or pmcid or doi or f"record-{idx:04d}"
            id_part = re.sub(r"[^A-Za-z0-9._ -]+", "-", id_part).strip(" ._- ") or f"record-{idx:04d}"
            title_part = re.sub(r"[^A-Za-z0-9._ -]+", "-", title)[:80].strip(" ._- ") or "paper"
            arcname = f"{idx:04d}_{id_part}_{title_part}.pdf"
            while arcname in used_names:
                arcname = f"{idx:04d}_{id_part}_{title_part}_{len(used_names)+1}.pdf"
            used_names.add(arcname)
            zf.write(path, arcname)
            added += 1

    if added == 0:
        try:
            package_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    try:
        rel = str(package_path.resolve().relative_to(ctx.root_dir.parent.resolve()))
    except Exception:
        rel = str(package_path)
    return {
        "fileName": package_path.name,
        "localZipPath": portable_local_path(package_path),
        "relativePath": rel,
        "batchLabel": "Combined row-level article PDF package",
        "sizeBytes": package_path.stat().st_size,
        "pdfCount": added,
        "source": "local_package_from_row_level_pdfs",
    }


def save_pdf_manifest(ctx: WorkflowContext, references: List[Dict[str, Any]], batch_results: List[Dict[str, Any]], batch_errors: List[Dict[str, Any]], batch_size: int) -> None:
    """Write resumable, user-readable PDF acquisition manifests for large runs."""
    manifest_json = ctx.outputs_dir / "05_pdf_download_manifest.json"
    manifest_csv = ctx.outputs_dir / "05_pdf_download_manifest.csv"
    error_csv = ctx.outputs_dir / "05_pdf_download_failures.csv"

    packages = package_zip_entries(ctx, batch_results)
    completed_reference_count = 0
    successful_pdf_count = 0
    for batch in batch_results:
        if not isinstance(batch, dict):
            continue
        completed_reference_count += int(batch.get("requested_reference_count") or batch.get("batch_size") or 0)
        pdf_entries = batch.get("pdfs")
        if isinstance(pdf_entries, list):
            successful_pdf_count += len([entry for entry in pdf_entries if isinstance(entry, dict)])
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "requested_reference_count": len(references),
        "batch_size": batch_size,
        "completed_batch_count": len(batch_results),
        "completed_reference_count": min(completed_reference_count, len(references)),
        "successful_pdf_count": successful_pdf_count,
        "failed_batch_count": len(batch_errors),
        "package_zip_count": len(packages),
        "package_zips": packages,
        "batch_errors": batch_errors,
    }
    save_json(manifest_json, payload)

    # One row per original requested reference, with batch assignment. This avoids
    # loading or writing huge per-PDF metadata structures in memory for 20k+ runs.
    import csv
    with manifest_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["reference_index", "batch_number", "reference_id", "title", "status_hint"])
        writer.writeheader()
        for idx, ref in enumerate(references, start=1):
            batch_number = ((idx - 1) // max(batch_size, 1)) + 1
            writer.writerow({
                "reference_index": idx,
                "batch_number": batch_number,
                "reference_id": reference_identifier(ref),
                "title": str(ref.get("title") or ref.get("Title") or "")[:500] if isinstance(ref, dict) else "",
                "status_hint": "see 05_pdf_download.json and package ZIPs",
            })

    with error_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["batch_index", "batch_label", "batch_size", "error_type", "error"])
        writer.writeheader()
        for err in batch_errors:
            writer.writerow({
                "batch_index": err.get("batch_index", ""),
                "batch_label": err.get("batch_label", ""),
                "batch_size": err.get("batch_size", ""),
                "error_type": err.get("error_type", ""),
                "error": err.get("error", ""),
            })


def process_pdf_batch_resilient(
    ctx: WorkflowContext,
    client: SMIAPIClient,
    batch_refs: List[Dict[str, Any]],
    pdf_dir: Path,
    unpaywall_email: str,
    batch_label: str,
    batch_index: int,
    min_batch_size: int,
    max_retries: int,
    retry_sleep_seconds: float,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Download one batch, retrying and automatically splitting large failed batches.

    Large PDF acquisition often fails because one 100-reference request is too large
    or contains a slow/unavailable subset. Instead of losing the whole run, this
    function retries, then splits the failed batch into smaller sub-batches down to
    SMI_PDF_MIN_BATCH_SIZE.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 2):
        try:
            append_log(ctx, f"Starting {batch_label}, attempt {attempt}/{max_retries + 1}.")
            batch_result = client.download_pdf(
                references=batch_refs,
                unpaywall_email=unpaywall_email,
            )
            batch_result = process_pdf_api_result(
                ctx=ctx,
                client=client,
                result=batch_result,
                pdf_dir=pdf_dir,
                batch_label=batch_label,
                batch_refs=batch_refs,
            )
            batch_result["batch_index"] = batch_index
            batch_result["batch_label"] = batch_label
            batch_result["batch_size"] = len(batch_refs)
            batch_result["requested_reference_count"] = len(batch_refs)
            return [batch_result], [e for e in batch_result.get("pdfs", []) if isinstance(e, dict) and e.get("localPath")], []
        except Exception as exc:
            last_exc = exc
            append_log(ctx, f"{batch_label} attempt {attempt} failed: {type(exc).__name__}: {exc}")
            if attempt <= max_retries and retry_sleep_seconds > 0:
                time.sleep(retry_sleep_seconds)

    # Failed after retries. Split if possible.
    if len(batch_refs) > min_batch_size:
        midpoint = max(1, len(batch_refs) // 2)
        left = batch_refs[:midpoint]
        right = batch_refs[midpoint:]
        append_log(ctx, f"{batch_label} failed after retries; splitting into {len(left)} and {len(right)} references.")
        results: List[Dict[str, Any]] = []
        pdfs: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for suffix, subrefs in [("A", left), ("B", right)]:
            sub_label = f"{batch_label}.{suffix} ({len(subrefs)} references)"
            sub_results, sub_pdfs, sub_errors = process_pdf_batch_resilient(
                ctx=ctx,
                client=client,
                batch_refs=subrefs,
                pdf_dir=pdf_dir,
                unpaywall_email=unpaywall_email,
                batch_label=sub_label,
                batch_index=batch_index,
                min_batch_size=min_batch_size,
                max_retries=max_retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
            results.extend(sub_results)
            pdfs.extend(sub_pdfs)
            errors.extend(sub_errors)
        return results, pdfs, errors

    err = {
        "batch_index": batch_index,
        "batch_label": batch_label,
        "batch_size": len(batch_refs),
        "error_type": type(last_exc).__name__ if last_exc else "UnknownError",
        "error": str(last_exc) if last_exc else "Unknown PDF batch failure",
    }
    append_log(ctx, f"{batch_label} failed permanently at minimum batch size {len(batch_refs)}.")
    return [], [], [err]


def process_pdf_api_result(
    ctx: WorkflowContext,
    client: SMIAPIClient,
    result: Any,
    pdf_dir: Path,
    batch_label: str = "",
    batch_refs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Normalize one PDF API response into local original-PDF entries.

    The API may return per-paper PDF URLs, a direct PDF, or a ZIP package.
    This function downloads/extracts only original API-provided PDFs; it does
    not generate fake/local preview PDFs.
    """
    if not isinstance(result, dict):
        result = {"payload": result}

    api_pdf_entries: List[Dict[str, Any]] = []

    per_file_entries = download_individual_api_pdfs(client, result, pdf_dir)
    if per_file_entries:
        api_pdf_entries.extend(per_file_entries)
        append_log(ctx, f"{batch_label} downloaded {len(per_file_entries)} original individual PDFs listed by the API.")

    download_url = result.get("downloadUrl") or result.get("download_url") or result.get("url")
    file_name = result.get("fileName") or result.get("file_name") or "api_pdf_download"

    if download_url:
        raw_name = (file_name or "api_pdf_download").replace("\\", "/").split("/")[-1]
        raw_name = re.sub(r"[^A-Za-z0-9._ -]+", "-", raw_name).strip(" ._- ") or "api_pdf_download"
        if batch_label:
            # v36: parallel PDF batches can receive the same package file name
            # from the API. Prefix with a sanitized batch label before calling
            # unique_target so simultaneous downloads do not collide.
            batch_prefix = re.sub(r"[^A-Za-z0-9._ -]+", "-", batch_label).strip(" ._- ")[:60]
            if batch_prefix and not raw_name.startswith(batch_prefix):
                raw_name = f"{batch_prefix}-{raw_name}"
        if not re.search(r"\.(pdf|zip)$", raw_name, flags=re.I):
            if ".pdf" in str(download_url).lower():
                raw_name += ".pdf"
            elif ".zip" in str(download_url).lower():
                raw_name += ".zip"
            else:
                raw_name += ".bin"

        downloaded_path = unique_target(pdf_dir / raw_name)
        resolved_url = client.download_file(download_url, downloaded_path)

        if is_probably_pdf(downloaded_path):
            originals_dir = pdf_dir / "api_original_pdfs"
            originals_dir.mkdir(parents=True, exist_ok=True)
            pdf_name = clean_file_name(raw_name, "paper.pdf")
            target_pdf = unique_target(originals_dir / pdf_name)
            if downloaded_path.resolve() != target_pdf.resolve():
                shutil.move(str(downloaded_path), target_pdf)
            else:
                target_pdf = downloaded_path
            direct_entry = {
                "title": Path(target_pdf).stem,
                "fileName": target_pdf.name,
                "localPath": portable_local_path(target_pdf),
                "downloadUrl": download_url,
                "resolvedDownloadUrl": resolved_url,
                "status": "downloaded",
                "source": "original_api_pdf_direct",
                "is_original_pdf": True,
            }
            probe = pdf_text_probe(target_pdf) if os.environ.get("SMI_PDF_TEXT_PROBE", "1") != "0" else ""
            if probe:
                direct_entry["pdfTextProbe"] = probe
            if batch_refs and len(batch_refs) == 1:
                direct_entry = attach_pdf_reference_mapping(direct_entry, batch_refs[0], "single_reference_direct_pdf")
            api_pdf_entries.append(direct_entry)
            result["localPdfPath"] = portable_local_path(target_pdf)
            append_log(ctx, f"{batch_label} downloaded direct original API PDF to {target_pdf}.")
        elif zipfile.is_zipfile(downloaded_path):
            local_zip_path = downloaded_path
            if local_zip_path.suffix.lower() != ".zip":
                target_zip = unique_target(local_zip_path.with_suffix(".zip"))
                shutil.move(str(local_zip_path), target_zip)
                local_zip_path = target_zip
            result["localZipPath"] = portable_local_path(local_zip_path)
            extracted_pdf_entries = extract_pdf_entries_from_zip(
                local_zip_path,
                pdf_dir,
                reference_records=batch_refs or [],
                allow_order_mapping=os.environ.get("SMI_PDF_ALLOW_BATCH_ORDER_MAPPING") == "1",
            )
            if extracted_pdf_entries:
                api_pdf_entries.extend(extracted_pdf_entries)
                append_log(ctx, f"{batch_label} extracted {len(extracted_pdf_entries)} original PDFs from API ZIP.")
            append_log(ctx, f"{batch_label} downloaded PDF package to {local_zip_path}.")
        else:
            append_log(ctx, f"{batch_label} top-level PDF download URL returned a non-PDF/non-ZIP file: {downloaded_path}.")
            result["downloadedNonPdfPath"] = portable_local_path(downloaded_path)

    if api_pdf_entries:
        deduped = []
        seen = set()
        for entry in api_pdf_entries:
            key = entry.get("localPath") or entry.get("fileName") or json.dumps(entry, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        existing_entries = result.get("pdfs") if isinstance(result.get("pdfs"), list) else []
        result["pdfs"] = deduped + [e for e in existing_entries if isinstance(e, dict) and not e.get("localPath")]
        result["source"] = result.get("source") or "api_original_pdf_download"
        result["status"] = result.get("status") or "success"
    else:
        result["pdfs"] = []
        result["status"] = result.get("status") or "no_extractable_pdfs"
    return result


def dedupe_pdf_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for entry in entries:
        key = entry.get("localPath") or entry.get("fileName") or entry.get("downloadUrl") or json.dumps(entry, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped

def make_pdf_unavailable_result(ctx: WorkflowContext, note: str) -> Dict[str, Any]:
    """Record that original PDFs could not be downloaded without creating fake paper PDFs."""
    pdf_dir = ctx.outputs_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    return {
        "status": "unavailable",
        "source": "api_unavailable",
        "note": note,
        "fileName": "",
        "localZipPath": "",
        "pdfs": [],
    }


def make_demo_pdf_result(ctx: WorkflowContext, note: str = "local fallback") -> Dict[str, Any]:
    records = previous_records(ctx)
    pdf_dir = ctx.outputs_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_entries = []
    for i, rec in enumerate(records[:10], start=1):
        title = get_first_record_value(rec, ["title", "article_title"]) or f"Record {i}"
        pdf_path = pdf_dir / safe_pdf_filename(i, title)
        pdf_path.write_bytes(article_pdf_bytes(rec, i))
        pdf_entries.append({
            "title": title,
            "fileName": pdf_path.name,
            "localPath": portable_local_path(pdf_path),
            "status": "downloaded",
            "source": "local_article_preview",
        })
    zip_path = pdf_dir / "local_pdf_package.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in pdf_entries:
            zf.write(entry["localPath"], arcname=entry["fileName"])
    return {
        "status": "success",
        "source": "local_fallback",
        "note": note,
        "fileName": zip_path.name,
        "localZipPath": portable_local_path(zip_path),
        "pdfs": pdf_entries,
    }

def stage_literature_search(ctx: WorkflowContext, client: SMIAPIClient) -> None:
    write_status(ctx, "running", "literature_search")
    cfg = ctx.config["literature_search"]
    if use_local_fallback(ctx):
        result = make_demo_literature_result(ctx, "Explicit local demo mode enabled; using bundled demo records.")
    else:
        require_live_api_key(ctx, "literature_search")
        try:
            # v28 strict all-steps mode: apply the working-set limit at search
            # so the AI-screening endpoint does not receive a huge unbounded
            # reference set. For 1,000 records, search returns a 1,000-record
            # working set, then deduplication, AI screening, retrieval, PDF,
            # intelligence, and Knowledge Space all operate on that same set.
            requested_limit = config_limit(cfg.get("limit"))
            original_query = str(cfg.get("query") or "")
            search_query = repair_search_query_text(original_query)
            if search_query != original_query:
                append_log(
                    ctx,
                    f"Repaired likely mojibake in literature search query: {original_query!r} -> {search_query!r}.",
                )
                # Keep downstream query-derived screening criteria aligned with the
                # exact normalized query sent to the literature-search endpoint.
                cfg["query"] = search_query

            result = client.literature_search(
                search_query,
                cfg.get("database", ["pubmed"]),
                cfg.get("start_date"),
                cfg.get("end_date"),
                requested_limit,
            )
            result = limit_response_records(result, requested_limit)

            # Some upstream search deployments return zero for otherwise valid
            # pasted titles containing diacritics or punctuation. Only for a
            # plain/title-like query, retry safe spelling-equivalent variants.
            # Structured Boolean/PubMed searches are never broadened here.
            if response_record_count(result) == 0:
                fallback_queries = zero_result_title_fallback_queries(search_query)
                if fallback_queries:
                    append_log(
                        ctx,
                        "Literature search returned 0 records for a title-like query; "
                        f"trying {len(fallback_queries)} conservative recall fallback(s).",
                    )
                for fallback_query in fallback_queries:
                    append_log(ctx, f"Trying title-search fallback query: {fallback_query!r}.")
                    fallback_result = client.literature_search(
                        fallback_query,
                        cfg.get("database", ["pubmed"]),
                        cfg.get("start_date"),
                        cfg.get("end_date"),
                        requested_limit,
                    )
                    fallback_result = limit_response_records(fallback_result, requested_limit)
                    fallback_count = response_record_count(fallback_result)
                    append_log(
                        ctx,
                        "Title-search fallback completed with "
                        f"recordCount={fallback_count if fallback_count is not None else 'unknown'}: "
                        f"{fallback_query!r}.",
                    )
                    if fallback_count is None or fallback_count > 0:
                        result = fallback_result
                        append_log(ctx, f"Using successful title-search fallback query: {fallback_query!r}.")
                        break

            append_log(ctx, f"Literature search requested_limit={requested_limit if requested_limit is not None else 'ALL'}.")
        except Exception as exc:
            # Preserve the upstream API validation message in workflow.log.
            # This is diagnostic-only: it does not change the search payload,
            # retry behavior, or any downstream workflow stage.
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    response_body = str(response.text or "")
                except Exception:
                    response_body = ""
                response_body = re.sub(r"\s+", " ", response_body).strip()[:4000]
                append_log(
                    ctx,
                    (
                        "Literature search API failure: "
                        f"status={getattr(response, 'status_code', None)}; "
                        f"url={getattr(response, 'url', '')}; "
                        f"response={response_body!r}"
                    ),
                )
            fail_stage(ctx, "literature_search", exc)
            raise
    save_json(ctx.outputs_dir / "01_literature_search.json", result)
    update_metadata(ctx, "literature_search", result)

    search_count = response_record_count(result)
    if search_count == 0:
        query_used = str(cfg.get("query") or "")
        mark_no_records_completion(
            ctx,
            "literature_search",
            (
                "Literature search completed successfully but returned 0 records. "
                "The workflow stopped cleanly before deduplication and AI screening. "
                f"Search query: {query_used!r}."
            ),
        )
        return

    append_log(ctx, "Completed literature search.")

def stage_deduplication(ctx: WorkflowContext, client: SMIAPIClient) -> None:
    write_status(ctx, "running", "deduplication")

    metadata = load_json(ctx.metadata_file, default={})

    references_id = find_response_id(
        metadata["artifacts"]["literature_search"],
        ["references_id", "citations_id", "id", "file_id"],
    )

    dedup_cfg = ctx.config.get("deduplication", {})
    dedup_limit = config_limit(dedup_cfg.get("limit"))

    # Defensive resume/backward-compatibility guard: older runs may already have
    # a zero-result search artifact but no workflow-control marker. Do not call
    # /deduplication for an empty reference set.
    search_artifact = metadata.get("artifacts", {}).get("literature_search")
    if response_record_count(search_artifact) == 0 and not use_local_fallback(ctx):
        result = {
            "id": references_id,
            "references_id": references_id,
            "recordCount": 0,
            "duplicateRecordCount": 0,
            "deduplication_status": "skipped_no_search_records",
            "applied_limit": dedup_limit,
        }
        save_json(ctx.outputs_dir / "02_deduplication.json", result)
        update_metadata(ctx, "deduplication", result)
        mark_no_records_completion(
            ctx,
            "deduplication",
            "Deduplication was skipped because the literature search contained 0 records; AI screening was not started.",
        )
        return

    if use_local_fallback(ctx):
        result = make_stage_result(ctx, "deduplication", "deduplicated_references_id", "Explicit local demo mode; deduplication simulated.")
        result["applied_limit"] = dedup_limit
    else:
        require_live_api_key(ctx, "deduplication")
        if not references_id:
            exc = ValueError("Could not find references_id from literature search response.")
            fail_stage(ctx, "deduplication", exc)
            raise exc
        try:
            result = client.deduplicate(references_id, limit=dedup_limit)
            result = limit_response_records(result, dedup_limit)
        except requests.exceptions.ReadTimeout as exc:
            # Some API deployments create a full references_id at search time and then
            # ignore the requested limit during /deduplication. For limited exploratory
            # runs, do not block the whole app forever; transparently pass the search
            # references_id forward and let AI screening/retrieval/PDF stages enforce
            # the same limit. This is intentionally recorded in the output and log.
            if dedup_limit is not None and os.environ.get("SMI_DEDUP_TIMEOUT_PASSTHROUGH", "0") == "1":
                result = {
                    "references_id": references_id,
                    "deduplicated_references_id": references_id,
                    "deduplication_status": "skipped_after_timeout_limited_passthrough",
                    "applied_limit": dedup_limit,
                    "warning": (
                        "Deduplication timed out. The workflow passed the search "
                        "references_id forward and will enforce the requested limit "
                        "during AI screening, retrieval, PDF download, intelligence, "
                        "and Knowledge Space stages. Set SMI_DEDUP_TIMEOUT_PASSTHROUGH=0 "
                        "to make this timeout fail the run instead."
                    ),
                    "timeout_error": str(exc),
                }
                append_log(ctx, f"Deduplication timed out; using limited passthrough with effective_limit={dedup_limit}.")
            else:
                fail_stage(ctx, "deduplication", exc)
                raise
        except Exception as exc:
            fail_stage(ctx, "deduplication", exc)
            raise

    save_json(ctx.outputs_dir / "02_deduplication.json", result)
    update_metadata(ctx, "deduplication", result)
    append_log(ctx, f"Completed deduplication. effective_limit={dedup_limit if dedup_limit is not None else 'ALL'}.")

def stage_screening(ctx: WorkflowContext, client: SMIAPIClient) -> None:
    write_status(ctx, "running", "ai_screening")

    metadata = load_json(ctx.metadata_file, default={})

    references_id = find_response_id(
        metadata["artifacts"].get("deduplication"),
        [
            "references_id",
            "deduplicated_references_id",
            "dedup_id",
            "deduplication_id",
            "id",
            "file_id",
        ],
    )

    cfg = ctx.config["ai_screening"]
    screen_limit = config_limit(cfg.get("limit"))

    # Defensive guard for resumed/older zero-record runs. A zero-record
    # deduplication result is a valid empty search outcome, not an AI-screening
    # transport failure, so do not probe /results or /ai-screening.
    dedup_artifact = metadata.get("artifacts", {}).get("deduplication")
    if response_record_count(dedup_artifact) == 0 and not use_local_fallback(ctx):
        result = {
            "chunked_ai_screening": False,
            "recordCount": 0,
            "processed_record_count": 0,
            "retained_record_count": 0,
            "decisionCounts": {
                "Relevant": 0,
                "Unclear": 0,
                "Not relevant": 0,
            },
            "screening_status": "skipped_no_records",
        }
        save_json(ctx.outputs_dir / "03_ai_screening.json", result)
        update_metadata(ctx, "ai_screening", result)
        mark_no_records_completion(
            ctx,
            "ai_screening",
            "AI screening was skipped because the deduplicated literature set contains 0 records.",
        )
        return

    exclusion_criteria = resolve_ai_screen_exclusion_criteria(ctx, cfg)

    if cfg.get("enabled", True) is False:
        exc = ValueError("AI screening is disabled in the config, but strict all-steps mode requires AI screening.")
        fail_stage(ctx, "ai_screening", exc)
        raise exc

    if use_local_fallback(ctx):
        result = make_stage_result(ctx, "ai_screening", "screening_id", "Explicit local demo mode; AI screening simulated.")
    else:
        require_live_api_key(ctx, "ai_screening")
        if not references_id:
            exc = ValueError("Could not find references_id from deduplication response.")
            fail_stage(ctx, "ai_screening", exc)
            raise exc

        try:
            chunk_size = ai_screen_chunk_size(cfg, screen_limit)
            max_retries = coerce_positive_int(
                os.environ.get("SMI_AI_SCREEN_RETRIES") or cfg.get("retries"),
                1,
            )
            retry_sleep_seconds = coerce_nonnegative_float(
                os.environ.get("SMI_AI_SCREEN_RETRY_SLEEP_SECONDS") or cfg.get("retry_sleep_seconds"),
                5.0,
            )

            # The current /ai-screening endpoint returns aggregate decisionCounts
            # plus an opaque references_* id that is not registered with /results.
            # Therefore, retrieve the concrete citations first and screen one
            # citation per API request. This is the only reliable way to map each
            # Relevant/Unclear/Not relevant decision back to the exact local record.
            input_records = read_records_for_chunked_screening(
                ctx, client, references_id, screen_limit, chunk_size
            )
            if not input_records:
                exc = RuntimeError(
                    "AI screening could not obtain concrete citation records for record-level screening. "
                    "The upstream /ai-screening response ids are not retrievable through /results, so "
                    "continuing would lose the mapping between screening decisions and records."
                )
                fail_stage(ctx, "ai_screening", exc)
                raise exc

            if os.environ.get("SMI_AI_SCREEN_CHUNKING", "1") == "0":
                # Preserve the user's no-chunking preference for artifact grouping.
                # API calls are still record-level because the endpoint does not
                # provide a retrievable batch decision mapping.
                effective_chunk_size = len(input_records)
            else:
                effective_chunk_size = chunk_size

            chunks = [
                input_records[i:i + effective_chunk_size]
                for i in range(0, len(input_records), effective_chunk_size)
            ]
            total_records = len(input_records)
            total_chunks = len(chunks)
            append_log(
                ctx,
                (
                    f"AI screening will use record-level local decision mapping for {total_records} records "
                    f"in {total_chunks} checkpoint chunk(s), chunk_size={effective_chunk_size}. "
                    "Opaque references_* ids returned by /ai-screening will be kept for audit only and "
                    "will not be sent to /results."
                ),
            )

            ai_parallel_enabled = env_bool("SMI_AI_SCREEN_PARALLEL", True)
            ai_workers = bounded_worker_count("SMI_AI_SCREEN_MAX_WORKERS", 2, max_allowed=100)
            if not ai_parallel_enabled:
                ai_workers = 1
            ai_workers = max(1, min(ai_workers, total_records))
            append_log(
                ctx,
                f"AI screening record-level workers={ai_workers} (parallel={'enabled' if ai_workers > 1 else 'disabled'}).",
            )

            # Pre-allocate slots so the original record order is preserved even
            # when futures finish out of order.
            chunk_slots: Dict[int, List[Optional[Dict[str, Any]]]] = {
                idx: [None] * len(chunk) for idx, chunk in enumerate(chunks, start=1)
            }
            chunk_results: List[Dict[str, Any]] = []
            chunk_errors: List[Dict[str, Any]] = []
            completed_chunks = set()
            completed_records = 0

            def checkpoint_partial() -> None:
                save_json(ctx.outputs_dir / "03_ai_screening_partial.json", {
                    "chunked_ai_screening": True,
                    "screening_mode": "record_level_local_decisions",
                    "parallel": ai_workers > 1,
                    "max_workers": ai_workers,
                    "completed_chunk_units": len(chunk_results),
                    "failed_chunk_units": len(chunk_errors),
                    "completed_record_count": completed_records,
                    "requested_record_count": total_records,
                    "chunk_size": effective_chunk_size,
                    "chunks": sorted(chunk_results, key=lambda x: x.get("chunk_index") or 0),
                    "chunk_errors": chunk_errors,
                })

            def finish_record(
                item: Dict[str, Any],
                chunk_index: int,
                slot_index: int,
            ) -> None:
                nonlocal completed_records
                chunk_slots[chunk_index][slot_index] = item
                completed_records += 1

                slots = chunk_slots[chunk_index]
                if chunk_index not in completed_chunks and all(x is not None for x in slots):
                    typed_slots = [x for x in slots if isinstance(x, dict)]
                    chunk_result = build_local_screening_chunk_result(
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                        source_records=chunks[chunk_index - 1],
                        record_results=typed_slots,
                    )
                    chunk_results.append(chunk_result)
                    chunk_results.sort(key=lambda x: x.get("chunk_index") or 0)
                    completed_chunks.add(chunk_index)
                    _save_ai_screen_chunk_result(ctx, chunk_index, chunk_result)
                    append_log(
                        ctx,
                        (
                            f"Completed AI screening chunk {chunk_index}/{total_chunks}: "
                            f"Relevant={chunk_result['decisionCounts']['Relevant']}, "
                            f"Unclear={chunk_result['decisionCounts']['Unclear']}, "
                            f"Not relevant={chunk_result['decisionCounts']['Not relevant']}."
                        ),
                    )
                    checkpoint_partial()
                elif completed_records % 25 == 0 or completed_records == total_records:
                    append_log(
                        ctx,
                        f"AI screening record-level progress: {completed_records}/{total_records} records completed.",
                    )

            if ai_workers > 1:
                with ThreadPoolExecutor(max_workers=ai_workers) as executor:
                    future_map = {}
                    global_index = 0
                    for chunk_index, chunk in enumerate(chunks, start=1):
                        for slot_index, record in enumerate(chunk):
                            global_index += 1
                            check_stop_requested(ctx, "ai_screening")
                            future = executor.submit(
                                screen_single_record_with_retries,
                                ctx,
                                client,
                                record,
                                exclusion_criteria,
                                global_index,
                                total_records,
                                chunk_index,
                                max_retries,
                                retry_sleep_seconds,
                            )
                            future_map[future] = (global_index, chunk_index, slot_index, record)

                    for future in as_completed(future_map):
                        check_stop_requested(ctx, "ai_screening")
                        global_index, chunk_index, slot_index, record = future_map[future]
                        try:
                            item = future.result()
                            finish_record(item, chunk_index, slot_index)
                        except Exception as exc:
                            details = ai_screen_http_error_details(exc)
                            chunk_errors.append({
                                "record_index": global_index,
                                "chunk_index": chunk_index,
                                "record": screening_record_label(record),
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                **details,
                            })
                            completed_records += 1
                            checkpoint_partial()
            else:
                global_index = 0
                for chunk_index, chunk in enumerate(chunks, start=1):
                    for slot_index, record in enumerate(chunk):
                        global_index += 1
                        check_stop_requested(ctx, "ai_screening")
                        try:
                            item = screen_single_record_with_retries(
                                ctx=ctx,
                                client=client,
                                record=record,
                                exclusion_criteria=exclusion_criteria,
                                record_index=global_index,
                                total_records=total_records,
                                chunk_index=chunk_index,
                                max_retries=max_retries,
                                retry_sleep_seconds=retry_sleep_seconds,
                            )
                            finish_record(item, chunk_index, slot_index)
                        except Exception as exc:
                            details = ai_screen_http_error_details(exc)
                            chunk_errors.append({
                                "record_index": global_index,
                                "chunk_index": chunk_index,
                                "record": screening_record_label(record),
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                **details,
                            })
                            completed_records += 1
                            checkpoint_partial()

            if chunk_errors:
                exc = RuntimeError(
                    f"AI screening failed for {len(chunk_errors)} record(s). "
                    "No records were silently dropped. See outputs/03_ai_screening_partial.json "
                    "and logs/workflow.log."
                )
                fail_stage(ctx, "ai_screening", exc)
                raise exc

            combined_records: List[Dict[str, Any]] = []
            total_decisions = {"Relevant": 0, "Not relevant": 0, "Unclear": 0}
            for item in sorted(chunk_results, key=lambda x: x.get("chunk_index") or 0):
                combined_records.extend(extract_records_from_response(item))
                counts = item.get("decisionCounts") or {}
                for key in total_decisions:
                    try:
                        total_decisions[key] += int(counts.get(key) or 0)
                    except (TypeError, ValueError):
                        pass

            result = {
                "chunked_ai_screening": True,
                "screening_status": "screened_in_chunks",
                "screening_mode": "record_level_local_decisions",
                "references_id": references_id,
                "applied_limit": screen_limit,
                "requested_record_count": total_records,
                "processed_record_count": total_records,
                # Preserve the existing UI meaning: number actually screened.
                "screened_record_count": total_records,
                "retained_record_count": len(combined_records),
                "decisionCounts": total_decisions,
                "chunk_size": effective_chunk_size,
                "chunk_count": total_chunks,
                "parallel": ai_workers > 1,
                "max_workers": ai_workers,
                "completed_chunk_unit_count": len(chunk_results),
                "chunks": sorted(chunk_results, key=lambda x: x.get("chunk_index") or 0),
                # Kept for shape compatibility; opaque ids are intentionally not
                # used for retrieval because the upstream API rejects them.
                "screening_ids": [],
                "records": combined_records,
            }
            append_log(
                ctx,
                (
                    f"Completed record-level AI screening for {total_records} records: "
                    f"Relevant={total_decisions['Relevant']}, "
                    f"Unclear={total_decisions['Unclear']}, "
                    f"Not relevant={total_decisions['Not relevant']}; "
                    f"retained={len(combined_records)}."
                ),
            )
            append_log(ctx, f"AI screening effective_limit={screen_limit if screen_limit is not None else 'ALL'}.")
        except Exception as exc:
            fail_stage(ctx, "ai_screening", exc)
            raise

    save_json(ctx.outputs_dir / "03_ai_screening.json", result)
    update_metadata(ctx, "ai_screening", result)
    append_log(ctx, "Completed AI screening.")


def stage_retrieve_records(ctx: WorkflowContext, client: SMIAPIClient) -> None:

    write_status(ctx, "running", "retrieve_screened_records")

    metadata = load_json(ctx.metadata_file, default={})

    screening_result_id = find_response_id(
        metadata["artifacts"].get("ai_screening"),
        [
            "screening_id",
            "screened_id",
            "results_id",
            "result_id",
            "id",
            "file_id",
        ],
    )

    cfg = ctx.config.get("record_retrieval", {})
    ai_artifact = metadata["artifacts"].get("ai_screening") or {}
    ai_status = ai_artifact.get("screening_status") if isinstance(ai_artifact, dict) else None

    if isinstance(ai_artifact, dict) and ai_artifact.get("chunked_ai_screening"):
        require_live_api_key(ctx, "retrieve_screened_records")
        retrieval_limit = config_limit(cfg.get("number_of_records"))
        start_index = cfg.get("start_index", 0)
        all_records: List[Dict[str, Any]] = []
        retrieval_chunks: List[Dict[str, Any]] = []
        chunks = [c for c in ai_artifact.get("chunks", []) if isinstance(c, dict)]
        append_log(ctx, f"Retrieving records from {len(chunks)} AI-screening chunk result(s).")
        for idx, chunk in enumerate(chunks, start=1):
            check_stop_requested(ctx, "retrieve_screened_records")
            chunk_records = extract_records_from_response(chunk)
            if chunk_records:
                all_records.extend(chunk_records)
                retrieval_chunks.append({"chunk_index": idx, "source": "records_embedded_in_ai_screening_chunk", "record_count": len(chunk_records)})
                continue
            chunk_id = find_response_id(chunk, ["screening_id", "screened_id", "results_id", "result_id", "id", "file_id"])
            if not chunk_id:
                exc = ValueError(f"Could not find result id for AI-screening chunk {idx}.")
                fail_stage(ctx, "retrieve_screened_records", exc)
                raise exc
            chunk_size = config_limit(chunk.get("chunk_size")) or retrieval_limit or 100
            try:
                response = client.retrieve_records(
                    result_id=chunk_id,
                    result_type=cfg.get("type", "screened"),
                    start_index=start_index,
                    number_of_records=chunk_size,
                )
                response = limit_response_records(response, chunk_size)
                records = extract_records_from_response(response)
                all_records.extend(records)
                retrieval_chunks.append({"chunk_index": idx, "chunk_result_id": chunk_id, "record_count": len(records), "response": response})
            except Exception as exc:
                fail_stage(ctx, "retrieve_screened_records", exc)
                raise
        if retrieval_limit is not None:
            all_records = all_records[:retrieval_limit]
        result = {
            "chunked_retrieval": True,
            "source": "chunked_ai_screening_results",
            "record_count": len(all_records),
            "applied_limit": retrieval_limit,
            "records": all_records,
            "chunk_results": retrieval_chunks,
        }
        save_json(ctx.outputs_dir / "04_retrieved_records.json", result)
        update_metadata(ctx, "retrieved_records", result)
        append_log(ctx, f"Retrieved {len(all_records)} records from chunked AI screening.")
        return

    if use_local_fallback(ctx):
        result = make_stage_result(ctx, "retrieved_records", "result_id", "Explicit local demo mode; record retrieval simulated.")
    else:
        require_live_api_key(ctx, "retrieve_screened_records")
        if not screening_result_id:
            exc = ValueError("Could not find result id from AI screening response.")
            fail_stage(ctx, "retrieve_screened_records", exc)
            raise exc
        try:
            retrieval_limit = config_limit(cfg.get("number_of_records"))
            start_index = cfg.get("start_index", 0)
            if ai_status in {
                "skipped_by_user_large_scale_abstract_mode",
                "skipped_after_ai_screening_failure_passthrough",
            }:
                # The upstream API has used different result types for retrieving
                # records from an unscreened/deduplicated reference set. Try the
                # configured type first, then safe aliases. The first successful
                # response is saved with the retrieval type used.
                raw_types = [
                    os.environ.get("SMI_UNSCREENED_RETRIEVAL_TYPE", ""),
                    cfg.get("type", ""),
                    "references",
                    "deduplicated",
                    "records",
                    "screened",
                ]
                types_to_try = []
                for item in raw_types:
                    item = str(item).strip()
                    if item and item not in types_to_try:
                        types_to_try.append(item)
                last_exc = None
                result = None
                for result_type in types_to_try:
                    try:
                        candidate = client.retrieve_records(
                            result_id=screening_result_id,
                            result_type=result_type,
                            start_index=start_index,
                            number_of_records=retrieval_limit,
                        )
                        candidate = limit_response_records(candidate, retrieval_limit)
                        candidate["retrieval_type_used"] = result_type if isinstance(candidate, dict) else result_type
                        result = candidate
                        append_log(ctx, f"Record retrieval used passthrough result_type={result_type}.")
                        break
                    except Exception as exc:
                        last_exc = exc
                        append_log(ctx, f"Passthrough retrieval type failed: {result_type}: {exc}")
                if result is None:
                    raise last_exc or RuntimeError("Passthrough record retrieval failed for all result types.")
            else:
                result = client.retrieve_records(
                    result_id=screening_result_id,
                    result_type=cfg.get("type", "screened"),
                    start_index=start_index,
                    number_of_records=retrieval_limit,
                )
                result = limit_response_records(result, retrieval_limit)
            append_log(ctx, f"Record retrieval effective_limit={retrieval_limit if retrieval_limit is not None else 'ALL'}.")
        except Exception as exc:
            fail_stage(ctx, "retrieve_screened_records", exc)
            raise

    save_json(ctx.outputs_dir / "04_retrieved_records.json", result)
    update_metadata(ctx, "retrieved_records", result)
    append_log(ctx, "Retrieved screened records.")

def stage_pdf_download(ctx: WorkflowContext, client: SMIAPIClient) -> None:
    write_status(ctx, "running", "pdf_download")

    cfg = ctx.config.get("pdf_download", {})

    if not cfg.get("enabled", True):
        result = {
            "enabled": False,
            "message": "PDF download stage skipped because pdf_download.enabled is false.",
        }
        save_json(ctx.outputs_dir / "05_pdf_download.json", result)
        update_metadata(ctx, "pdf_download", result)
        append_log(ctx, "Skipped PDF download.")
        return

    retrieved_records_path = ctx.outputs_dir / "04_retrieved_records.json"

    if not retrieved_records_path.exists():
        raise FileNotFoundError(
            f"Cannot run PDF download. Missing file: {retrieved_records_path}"
        )

    retrieved_response = load_json(retrieved_records_path, default={})
    records = extract_records_from_response(retrieved_response)

    if not records:
        raise ValueError(
            "Could not extract records from 04_retrieved_records.json for PDF download."
        )

    # v65: no PDF-specific cap. The PDF stage attempts PDFs for every record
    # already retrieved into 04_retrieved_records.json. Record retrieval may still
    # control the overall workflow size, but there is no separate PDF limit here.
    references = records
    append_log(ctx, f"PDF download will attempt all retrieved records: {len(references)}.")

    unpaywall_email = (
        cfg.get("unpaywall_email")
        or os.environ.get("UNPAYWALL_EMAIL")
        or ""
    )

    pdf_dir = ctx.outputs_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if use_local_fallback(ctx):
        # PDF policy: the app must not pretend generated summaries are original papers.
        # In local/demo mode, do not create downloadable/previewable fake paper PDFs unless
        # the developer explicitly opts in with SMI_SHOW_GENERATED_PDFS=1.
        if os.environ.get("SMI_SHOW_GENERATED_PDFS") == "1":
            result = make_demo_pdf_result(ctx, "Local demo mode; generated local preview PDFs. These are not publisher PDFs.")
        else:
            result = make_pdf_unavailable_result(ctx, "Local/demo mode does not have access to original API paper PDFs. No generated preview PDFs were substituted. Run live mode with CONNECT_API_KEY and SMI_DEMO_MODE unset to download exact API PDFs.")
    else:
        batch_size = coerce_positive_int(
            os.environ.get("SMI_PDF_BATCH_SIZE") or cfg.get("batch_size"),
            10,
        )
        min_batch_size = coerce_positive_int(
            os.environ.get("SMI_PDF_MIN_BATCH_SIZE") or cfg.get("min_batch_size"),
            1,
        )
        max_retries = coerce_positive_int(
            os.environ.get("SMI_PDF_BATCH_RETRIES") or cfg.get("batch_retries"),
            2,
        )
        retry_sleep_seconds = coerce_nonnegative_float(
            os.environ.get("SMI_PDF_BATCH_RETRY_SLEEP_SECONDS") or cfg.get("batch_retry_sleep_seconds"),
            5.0,
        )
        # Package-first PDF mode is the default: download API ZIP/package batches,
        # keep the run-level package, extract PDFs, and map row-level links only
        # when PMID/PMCID/DOI or strong title/text evidence makes the match safe.
        # Set SMI_ROW_LEVEL_PDFS=1 only when you want slower one-article-at-a-time
        # downloads for maximum row-level coverage.
        row_level_pdfs = env_bool("SMI_ROW_LEVEL_PDFS", bool(cfg.get("row_level_pdfs", False)))
        if row_level_pdfs:
            batch_size = 1
            min_batch_size = 1
            append_log(ctx, "Row-level PDF mode enabled: requesting one reference per PDF batch for safest article-to-PDF mapping.")
        else:
            append_log(ctx, "Package-first PDF mode enabled: downloading run-level/batch PDF packages first, then inspecting extracted PDFs for safe row-level mapping.")
        # Large-scale PDF acquisition must be a batch/checkpoint process. A single
        # huge API request can time out or return a package that is too large to
        # download reliably. We request small batch ZIPs, checkpoint after every
        # batch, and automatically split failed batches down to single-reference
        # requests when needed.
        if os.environ.get("SMI_PDF_FORCE_SINGLE") == "1":
            batch_size = 1
            min_batch_size = 1
        if len(references) <= batch_size:
            batches = [references]
        else:
            batches = [references[i:i + batch_size] for i in range(0, len(references), batch_size)]
        append_log(ctx, f"PDF download will request {len(references)} references in {len(batches)} batch(es), batch_size={batch_size}, min_batch_size={min_batch_size}, retries={max_retries}.")

        combined_batch_results: List[Dict[str, Any]] = []
        combined_pdf_entries: List[Dict[str, Any]] = []
        batch_errors: List[Dict[str, Any]] = []

        pdf_parallel_enabled = env_bool("SMI_PDF_PARALLEL", True)
        pdf_workers = bounded_worker_count("SMI_PDF_MAX_WORKERS", 2, max_allowed=20)
        if pdf_parallel_enabled and len(batches) > 1 and pdf_workers > 1:
            append_log(ctx, f"PDF parallel mode enabled with max_workers={pdf_workers}.")
            with ThreadPoolExecutor(max_workers=pdf_workers) as executor:
                future_map = {}
                for batch_index, batch_refs in enumerate(batches, start=1):
                    check_stop_requested(ctx, "pdf_download")
                    batch_label = f"PDF batch {batch_index}/{len(batches)} ({len(batch_refs)} references)"
                    future = executor.submit(
                        process_pdf_batch_resilient,
                        ctx, client, batch_refs, pdf_dir, unpaywall_email, batch_label, batch_index,
                        min_batch_size, max_retries, retry_sleep_seconds,
                    )
                    future_map[future] = batch_label
                for future in as_completed(future_map):
                    check_stop_requested(ctx, "pdf_download")
                    batch_label = future_map[future]
                    batch_results, batch_pdfs, batch_errs = future.result()
                    combined_batch_results.extend(batch_results)
                    combined_pdf_entries.extend(batch_pdfs)
                    batch_errors.extend(batch_errs)
                    combined_batch_results.sort(key=lambda x: (x.get("batch_index") or 0, x.get("batch_label") or ""))
                    save_pdf_manifest(ctx, references, combined_batch_results, batch_errors, batch_size)
                    append_log(ctx, f"Checkpointed PDF manifest after {batch_label}; completed units: {len(combined_batch_results)}, errors: {len(batch_errors)}.")
        else:
            append_log(ctx, "PDF parallel mode disabled; running batches sequentially.")
            for batch_index, batch_refs in enumerate(batches, start=1):
                check_stop_requested(ctx, "pdf_download")
                batch_label = f"PDF batch {batch_index}/{len(batches)} ({len(batch_refs)} references)"
                batch_results, batch_pdfs, batch_errs = process_pdf_batch_resilient(
                    ctx=ctx,
                    client=client,
                    batch_refs=batch_refs,
                    pdf_dir=pdf_dir,
                    unpaywall_email=unpaywall_email,
                    batch_label=batch_label,
                    batch_index=batch_index,
                    min_batch_size=min_batch_size,
                    max_retries=max_retries,
                    retry_sleep_seconds=retry_sleep_seconds,
                )
                combined_batch_results.extend(batch_results)
                combined_pdf_entries.extend(batch_pdfs)
                batch_errors.extend(batch_errs)
                save_pdf_manifest(ctx, references, combined_batch_results, batch_errors, batch_size)
                append_log(ctx, f"Checkpointed PDF manifest after {batch_label}; completed units: {len(combined_batch_results)}, errors: {len(batch_errors)}.")

        combined_pdf_entries = dedupe_pdf_entries(combined_pdf_entries)
        package_zips = package_zip_entries(ctx, combined_batch_results)
        if row_level_pdfs:
            combined_package = build_row_level_pdf_package(ctx, combined_pdf_entries)
            if combined_package:
                existing_keys = {pkg.get("localZipPath") for pkg in package_zips if isinstance(pkg, dict)}
                if combined_package.get("localZipPath") not in existing_keys:
                    package_zips.insert(0, combined_package)
                append_log(ctx, f"Created combined run-level ZIP package from {combined_package.get('pdfCount', 0)} row-level PDFs: {combined_package.get('fileName')}.")
        if combined_pdf_entries or package_zips:
            result = {
                "enabled": True,
                "status": "partial_success" if batch_errors else "success",
                "source": "api_original_pdf_download_batched_resilient",
                "requested_reference_count": len(references),
                "batch_size": batch_size,
                "min_batch_size": min_batch_size,
                "batch_count": len(batches),
                "parallel": pdf_parallel_enabled and len(batches) > 1 and pdf_workers > 1,
                "max_workers": pdf_workers if pdf_parallel_enabled else 1,
                "row_level_pdfs": row_level_pdfs,
                "completed_batch_unit_count": len(combined_batch_results),
                "successful_pdf_count": len(combined_pdf_entries),
                "package_zip_count": len(package_zips),
                "failed_batch_count": len(batch_errors),
                "pdfs": combined_pdf_entries,
                "packageZips": package_zips,
                "batch_results": combined_batch_results,
                "batch_errors": batch_errors,
            }
            # Expose the first package link for backwards compatibility. The UI
            # also displays every package ZIP in packageZips.
            if package_zips:
                result["localZipPath"] = package_zips[0].get("localZipPath", "")
                result["fileName"] = package_zips[0].get("fileName", "")
        else:
            note = (
                "The PDF API returned no extractable original PDFs or package ZIPs for this run. "
                f"Requested {len(references)} references in {len(batches)} batch(es). "
                f"Failed batches: {len(batch_errors)}. No generated summary PDFs were substituted."
            )
            if os.environ.get("SMI_ALLOW_GENERATED_PDF_FALLBACK") == "1":
                result = make_demo_pdf_result(ctx, note + " Generated fallback previews because SMI_ALLOW_GENERATED_PDF_FALLBACK=1.")
            else:
                result = make_pdf_unavailable_result(ctx, note)
                result["batch_size"] = batch_size
                result["min_batch_size"] = min_batch_size
                result["batch_count"] = len(batches)
                result["parallel"] = pdf_parallel_enabled and len(batches) > 1 and pdf_workers > 1
                result["max_workers"] = pdf_workers if pdf_parallel_enabled else 1
                result["row_level_pdfs"] = row_level_pdfs
                result["batch_results"] = combined_batch_results
                result["batch_errors"] = batch_errors
                result["packageZips"] = package_zips
        save_pdf_manifest(ctx, references, combined_batch_results, batch_errors, batch_size)


    save_json(ctx.outputs_dir / "05_pdf_download.json", result)
    update_metadata(ctx, "pdf_download", result)
    append_log(ctx, "Completed PDF download stage.")



def topic_norm(text: Any) -> str:
    text = "" if text is None else str(text)
    text = str(text).lower()
    text = text.replace("pm 2.5", "pm2.5").replace("pm2 5", "pm2.5").replace("pm25", "pm2.5")
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def topic_terms(query: str) -> List[str]:
    if not query:
        return []
    query = re.sub(r"\b(and|or)\b", " ", query, flags=re.I)
    terms: List[str] = []
    for raw in re.split(r"[+,;/|]+|\s{2,}", query):
        term = topic_norm(raw)
        if term and term not in {"and", "or", "the", "papers", "paper", "articles", "article", "related", "to"}:
            terms.append(term)
    return terms


def record_topic_text(record: Dict[str, Any]) -> str:
    pieces: List[str] = []
    if isinstance(record, dict):
        for key in [
            "title", "Title", "article_title", "abstract", "Abstract", "summary", "description",
            "snippet", "journal", "journal_title", "keywords", "mesh_terms", "MeSH",
        ]:
            value = record.get(key)
            if isinstance(value, list):
                pieces.extend(str(x) for x in value if x)
            elif value:
                pieces.append(str(value))
    return topic_norm(" ".join(pieces))


def record_matches_topic(record: Dict[str, Any], query: str) -> bool:
    terms = topic_terms(query)
    if not terms:
        return True
    text = record_topic_text(record)
    return all(term in text for term in terms)


def merge_pdf_download_result(ctx: WorkflowContext, harvest_result: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a targeted harvest into 05_pdf_download.json so citation rows can map PDFs."""
    main_path = ctx.outputs_dir / "05_pdf_download.json"
    existing = load_json(main_path, default={})
    if not isinstance(existing, dict):
        existing = {}
    merged = dict(existing)
    merged["enabled"] = True
    merged["source"] = "api_original_pdf_download_with_targeted_harvests"
    merged["status"] = harvest_result.get("status") or existing.get("status") or "success"
    merged.setdefault("pdfs", [])
    merged.setdefault("packageZips", [])
    merged.setdefault("batch_results", [])
    merged.setdefault("batch_errors", [])
    merged.setdefault("targeted_harvests", [])

    merged["pdfs"] = dedupe_pdf_entries([e for e in (merged.get("pdfs") or []) if isinstance(e, dict)] + [e for e in (harvest_result.get("pdfs") or []) if isinstance(e, dict)])

    def _dedupe_by_path(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen = set()
        for item in items:
            key = item.get("localZipPath") or item.get("relativePath") or item.get("fileName") or json.dumps(item, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    merged["packageZips"] = _dedupe_by_path([e for e in (merged.get("packageZips") or []) if isinstance(e, dict)] + [e for e in (harvest_result.get("packageZips") or []) if isinstance(e, dict)])
    merged["batch_results"] = [e for e in (merged.get("batch_results") or []) if isinstance(e, dict)] + [e for e in (harvest_result.get("batch_results") or []) if isinstance(e, dict)]
    merged["batch_errors"] = [e for e in (merged.get("batch_errors") or []) if isinstance(e, dict)] + [e for e in (harvest_result.get("batch_errors") or []) if isinstance(e, dict)]
    merged["targeted_harvests"] = [e for e in (merged.get("targeted_harvests") or []) if isinstance(e, dict)] + [{
        "topic_query": harvest_result.get("topic_query", ""),
        "selected_reference_count": harvest_result.get("selected_reference_count", 0),
        "successful_pdf_count": harvest_result.get("successful_pdf_count", 0),
        "package_zip_count": harvest_result.get("package_zip_count", 0),
        "generated_at": harvest_result.get("generated_at", utc_now()),
        "result_file": harvest_result.get("result_file", ""),
    }]
    merged["successful_pdf_count"] = len(merged.get("pdfs") or [])
    merged["package_zip_count"] = len(merged.get("packageZips") or [])
    if merged.get("packageZips"):
        merged["localZipPath"] = merged["packageZips"][0].get("localZipPath", "")
        merged["fileName"] = merged["packageZips"][0].get("fileName", "")
    save_json(main_path, merged)
    update_metadata(ctx, "pdf_download", merged)
    return merged


def run_targeted_pdf_harvest(ctx: WorkflowContext, topic_query: str, limit: Optional[int] = 250) -> Dict[str, Any]:
    """Download PDFs only for records matching a focused topic/lens."""
    write_status(ctx, "running", "targeted_pdf_harvest", f"Targeted PDF harvest for topic: {topic_query or 'all selected records'}")
    client = SMIAPIClient(ctx.api_base_url)
    retrieved_path = ctx.outputs_dir / "04_retrieved_records.json"
    if not retrieved_path.exists():
        raise FileNotFoundError(f"Cannot run targeted PDF harvest. Missing file: {retrieved_path}")
    records = extract_records_from_response(load_json(retrieved_path, default={}))
    if not records:
        raise ValueError("Could not extract retrieved records for targeted PDF harvest.")

    selected = [rec for rec in records if record_matches_topic(rec, topic_query)]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        result = {
            "enabled": True,
            "status": "no_matching_records",
            "source": "targeted_pdf_harvest",
            "topic_query": topic_query,
            "selected_reference_count": 0,
            "message": "No retrieved records matched the topic query.",
            "generated_at": utc_now(),
        }
        return result

    pdf_dir = ctx.outputs_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    batch_size = coerce_positive_int(os.environ.get("SMI_PDF_BATCH_SIZE"), 10)
    min_batch_size = coerce_positive_int(os.environ.get("SMI_PDF_MIN_BATCH_SIZE"), 1)
    max_retries = coerce_positive_int(os.environ.get("SMI_PDF_BATCH_RETRIES"), 1)
    retry_sleep_seconds = coerce_nonnegative_float(os.environ.get("SMI_PDF_BATCH_RETRY_SLEEP_SECONDS"), 2.0)
    if os.environ.get("SMI_PDF_FORCE_SINGLE") == "1":
        batch_size = 1
        min_batch_size = 1
    unpaywall_email = os.environ.get("UNPAYWALL_EMAIL") or ""
    batches = [selected[i:i + batch_size] for i in range(0, len(selected), batch_size)]
    append_log(ctx, f"Targeted PDF harvest topic={topic_query!r}; selected={len(selected)}; batches={len(batches)}; batch_size={batch_size}.")

    combined_batch_results: List[Dict[str, Any]] = []
    combined_pdf_entries: List[Dict[str, Any]] = []
    batch_errors: List[Dict[str, Any]] = []
    pdf_parallel_enabled = env_bool("SMI_PDF_PARALLEL", True)
    pdf_workers = bounded_worker_count("SMI_PDF_MAX_WORKERS", 2, max_allowed=20)

    if pdf_parallel_enabled and len(batches) > 1 and pdf_workers > 1:
        append_log(ctx, f"Targeted PDF harvest parallel mode enabled with max_workers={pdf_workers}.")
        with ThreadPoolExecutor(max_workers=pdf_workers) as executor:
            future_map = {}
            for batch_index, batch_refs in enumerate(batches, start=1):
                check_stop_requested(ctx, "targeted_pdf_harvest")
                batch_label = f"Targeted PDF batch {batch_index}/{len(batches)} ({len(batch_refs)} references)"
                future = executor.submit(process_pdf_batch_resilient, ctx, client, batch_refs, pdf_dir, unpaywall_email, batch_label, batch_index, min_batch_size, max_retries, retry_sleep_seconds)
                future_map[future] = batch_label
            for future in as_completed(future_map):
                check_stop_requested(ctx, "targeted_pdf_harvest")
                batch_results, batch_pdfs, batch_errs = future.result()
                combined_batch_results.extend(batch_results)
                combined_pdf_entries.extend(batch_pdfs)
                batch_errors.extend(batch_errs)
    else:
        for batch_index, batch_refs in enumerate(batches, start=1):
            check_stop_requested(ctx, "targeted_pdf_harvest")
            batch_label = f"Targeted PDF batch {batch_index}/{len(batches)} ({len(batch_refs)} references)"
            batch_results, batch_pdfs, batch_errs = process_pdf_batch_resilient(ctx, client, batch_refs, pdf_dir, unpaywall_email, batch_label, batch_index, min_batch_size, max_retries, retry_sleep_seconds)
            combined_batch_results.extend(batch_results)
            combined_pdf_entries.extend(batch_pdfs)
            batch_errors.extend(batch_errs)

    combined_pdf_entries = dedupe_pdf_entries(combined_pdf_entries)
    package_zips = package_zip_entries(ctx, combined_batch_results)
    harvest_id = f"targeted_pdf_harvest_{dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^A-Za-z0-9]+', '-', topic_query or 'all').strip('-')[:40] or 'all'}"
    harvest_dir = ctx.outputs_dir / "targeted_pdf_harvests"
    harvest_dir.mkdir(parents=True, exist_ok=True)
    result_path = harvest_dir / f"{harvest_id}.json"
    result = {
        "enabled": True,
        "status": "partial_success" if batch_errors else "success",
        "source": "targeted_pdf_harvest",
        "topic_query": topic_query,
        "selected_reference_count": len(selected),
        "batch_size": batch_size,
        "min_batch_size": min_batch_size,
        "batch_count": len(batches),
        "parallel": pdf_parallel_enabled and len(batches) > 1 and pdf_workers > 1,
        "max_workers": pdf_workers if pdf_parallel_enabled else 1,
        "successful_pdf_count": len(combined_pdf_entries),
        "package_zip_count": len(package_zips),
        "failed_batch_count": len(batch_errors),
        "pdfs": combined_pdf_entries,
        "packageZips": package_zips,
        "batch_results": combined_batch_results,
        "batch_errors": batch_errors,
        "generated_at": utc_now(),
    }
    if package_zips:
        result["localZipPath"] = package_zips[0].get("localZipPath", "")
        result["fileName"] = package_zips[0].get("fileName", "")
    result["result_file"] = portable_local_path(result_path)
    save_json(result_path, result)

    # Write a compact CSV of selected references for auditability.
    import csv
    selection_csv = harvest_dir / f"{harvest_id}_selected_records.csv"
    with selection_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["index", "pmid", "pmcid", "doi", "title"])
        writer.writeheader()
        for i, rec in enumerate(selected, start=1):
            writer.writerow({
                "index": i,
                "pmid": deep_first_value(rec, ["pmid", "PMID", "pubmedId", "pubmed_id"]),
                "pmcid": deep_first_value(rec, ["pmcid", "PMCID", "pmcId", "pmc_id"]),
                "doi": deep_first_value(rec, ["doi", "DOI", "articleDoi", "article_doi"]),
                "title": deep_first_value(rec, ["title", "Title", "articleTitle", "article_title"]),
            })
    merge_pdf_download_result(ctx, result)
    metadata = load_json(ctx.metadata_file, default={})
    metadata.setdefault("artifacts", {})["targeted_pdf_harvest"] = result
    save_json(ctx.metadata_file, metadata)
    write_status(ctx, "completed", "targeted_pdf_harvest", f"Targeted PDF harvest completed for {len(selected)} matching records. PDFs: {len(combined_pdf_entries)}. Packages: {len(package_zips)}. Failures: {len(batch_errors)}.")
    append_log(ctx, f"Completed targeted PDF harvest: topic={topic_query!r}, selected={len(selected)}, pdfs={len(combined_pdf_entries)}, packages={len(package_zips)}, errors={len(batch_errors)}.")
    return result

def stage_intelligence_extraction(ctx: WorkflowContext) -> None:
    """Generate structured intelligence outputs from retrieved records.

    This add-on stage preserves the original workflow and writes additional
    JSON/CSV/Markdown outputs used by the intelligence pages in the app.
    """
    cfg = ctx.config.get("intelligence", {})
    if not cfg.get("enabled", True):
        result = {"enabled": False, "message": "Intelligence extraction skipped."}
        save_json(ctx.outputs_dir / "07_intelligence_extraction_skipped.json", result)
        update_metadata(ctx, "intelligence_extraction", result)
        append_log(ctx, "Skipped intelligence extraction.")
        return

    write_status(ctx, "running", "intelligence_extraction")

    if generate_intelligence_outputs is None:
        result = {"enabled": False, "message": "smi_intelligence module is not available."}
        save_json(ctx.outputs_dir / "07_intelligence_extraction_error.json", result)
        update_metadata(ctx, "intelligence_extraction", result)
        append_log(ctx, "Intelligence extraction module unavailable.")
        return

    modules = cfg.get("modules") or DEFAULT_MODULES
    counts = generate_intelligence_outputs(ctx.run_dir, modules)
    knowledge_counts = {}
    if generate_knowledge_space_outputs is not None:
        try:
            knowledge_counts = generate_knowledge_space_outputs(ctx.run_dir)
            append_log(ctx, f"Generated Knowledge Space outputs: {knowledge_counts}.")
        except Exception as exc:
            knowledge_counts = {"error": f"{type(exc).__name__}: {exc}"}
            append_log(ctx, f"Knowledge Space generation failed: {type(exc).__name__}: {exc}")
    result = {"enabled": True, "modules": modules, "counts": counts, "knowledge_space": knowledge_counts, "generated_at": utc_now()}
    save_json(ctx.outputs_dir / "07_intelligence_extraction.json", result)
    update_metadata(ctx, "intelligence_extraction", result)
    append_log(ctx, "Completed intelligence extraction.")

def stage_summarize(ctx: WorkflowContext) -> None:
    checkpoint = "post-pdf-download"

    if ctx.config.get("approval_gates", {}).get(checkpoint, True):
        if not require_approval(ctx, checkpoint):
            write_status(
                ctx,
                "waiting_for_review",
                "summary",
                (
                    "PDF download is complete. Human approval is required before "
                    "generating the summary. Add `APPROVED: post-pdf-download` "
                    "to approvals.md."
                ),
            )
            append_log(ctx, f"Paused for approval: {checkpoint}.")
            return

    write_status(ctx, "running", "summary")

    metadata = load_json(ctx.metadata_file, default={})
    summary_path = ctx.outputs_dir / "06_summary.md"

    summary_path.write_text(
        f"""# SMI Workflow Summary

## Run
- Subproject: `{ctx.subproject}`
- Run ID: `{ctx.run_id}`
- Generated: {utc_now()}

## Artifact Inventory

```json
{json.dumps(metadata.get("artifacts", {}), indent=2)}

## Interpretation
This starter summary records workflow artifacts and API responses. Future versions can add evidence tables, extracted associations, quality assessment, and narrative scientific conclusions.
""", encoding="utf-8")
    update_metadata(ctx, "summary", {"path": str(summary_path)})
    append_log(ctx, "Generated summary.")


def stage_archive(ctx: WorkflowContext) -> None:
    checkpoint = "final-approval"
    if ctx.config.get("approval_gates", {}).get(checkpoint, True) and not require_approval(ctx, checkpoint):
        write_status(ctx, "waiting_for_review", "archive", f"Final approval required. Add `APPROVED: {checkpoint}` to approvals.md.")
        append_log(ctx, f"Paused for approval: {checkpoint}.")
        return
    write_status(ctx, "archiving", "archive")
    archive_dir = ctx.root_dir / "archive" / ctx.subproject / ctx.run_id
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    if archive_dir.exists():
        raise FileExistsError(f"Archive already exists: {archive_dir}")
    shutil.copytree(ctx.run_dir, archive_dir)
    metadata = load_json(ctx.metadata_file, default={})
    metadata["state"] = "completed"
    metadata["archived_at"] = utc_now()
    metadata["archive_dir"] = str(archive_dir)
    save_json(ctx.metadata_file, metadata)
    write_status(ctx, "completed", "archive", f"Archived to {archive_dir}")
    append_log(ctx, f"Archived run to {archive_dir}.")


def run_workflow(ctx: WorkflowContext, resume: bool = False) -> None:
    client = SMIAPIClient(ctx.api_base_url)
    stages = [
        ("literature_search", lambda: stage_literature_search(ctx, client), "literature_search"),
        ("deduplication", lambda: stage_deduplication(ctx, client), "deduplication"),
        ("ai_screening", lambda: stage_screening(ctx, client), "ai_screening"),
        ("retrieved_records", lambda: stage_retrieve_records(ctx, client), "retrieved_records"),
        ("pdf_download", lambda: stage_pdf_download(ctx, client), "pdf_download"),
        ("intelligence_extraction", lambda: stage_intelligence_extraction(ctx), "intelligence_extraction"),
        ("summary", lambda: stage_summarize(ctx), "summary"),
        ("archive", lambda: stage_archive(ctx), "archive"),
    ]
    for artifact_key, fn, label in stages:
        check_stop_requested(ctx, label)
        metadata = load_json(ctx.metadata_file, default={})
        if resume and artifact_key in metadata.get("artifacts", {}):
            append_log(ctx, f"Skipping completed stage: {label}.")
            control = metadata.get("workflow_control") or {}
            if control.get("stop"):
                print(f"Workflow completed early: {control.get('note') or control.get('reason') or 'no records'}")
                return
            continue
        fn()
        check_stop_requested(ctx, label)

        # A valid zero-result search is a completed empty run, not a failed AI
        # screening run. Stages set this marker when there is nothing downstream
        # to process. This also keeps resume behavior deterministic.
        metadata = load_json(ctx.metadata_file, default={})
        control = metadata.get("workflow_control") or {}
        if control.get("stop"):
            print(f"Workflow completed early: {control.get('note') or control.get('reason') or 'no records'}")
            return

        status = ctx.status_file.read_text(encoding="utf-8")
        if "waiting_for_review" in status:
            print(f"Paused at stage: {label}")
            print(f"Edit approvals file: {ctx.approvals_file}")
            return
    print(f"Workflow finished. Run directory: {ctx.run_dir}")


def find_latest_run(root_dir: Path, subproject: str) -> Optional[str]:
    runs_dir = root_dir / "subprojects" / subproject / "runs"
    if not runs_dir.exists():
        return None
    runs = sorted([p.name for p in runs_dir.iterdir() if p.is_dir()])
    return runs[-1] if runs else None


DEFAULT_WORKFLOW_MD = """# SMI Workflow

## Workflow Name
Default SMI Literature Intelligence Workflow

## Stages

| Order | Stage | Description | Human Gate |
|---:|---|---|---|
| 1 | Literature Search | Query selected literature databases through API. | No |
| 2 | Deduplication | Remove duplicate citation records. | No |
| 3 | AI Screening | Apply exclusion criteria to citation records. | Yes: pre-ai-screening |
| 4 | Screened Record Retrieval | Retrieve screened records for review. | Yes: post-ai-screening |
| 5 | Summary | Generate a Markdown summary of outputs. | Yes: pre-final-summary |
| 6 | Archive | Copy completed run to archive. | Yes: final-approval |
"""

EXAMPLE_CONFIG = """literature_search:
  query: "(PM2.5 OR particulate matter) AND asthma AND geospatial"
  start_date: "2026-01-01"
  end_date: "2026-06-30"
  database:
    - pubmed
  # Use null for no limit / all available records when supported by the API.
  limit: 250

ai_screening:
  # v30: screen large runs in smaller chunks instead of one large API call.
  chunk_size: 100
  min_chunk_size: 25
  retries: 1
  exclusion_criteria: {}
  limit: 100

record_retrieval:
  type: screened
  start_index: 0
  number_of_records: 100

intelligence:
  enabled: true
  modules:
    - exposure_health_associations
    - research_datasets
    - geospatial_datasets
    - cohorts
    - chemical_watchlist

approval_gates:
  pre-ai-screening: true
  post-ai-screening: true
  pre-final-summary: true
  final-approval: true
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="SMI Markdown-first workflow runner")
    parser.add_argument("--root", default=".", help="SMI repository root directory")
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize SMI repository")
    create_parser = subparsers.add_parser("create-subproject", help="Create a new subproject")
    create_parser.add_argument("name", help="Subproject name")
    config_parser = subparsers.add_parser("write-example-config", help="Write example config.yaml")
    config_parser.add_argument("--path", default="config.yaml")
    run_parser = subparsers.add_parser("run", help="Create and run a workflow")
    run_parser.add_argument("--subproject", required=True)
    run_parser.add_argument("--config", required=True)
    resume_parser = subparsers.add_parser("resume", help="Resume latest or selected run")
    resume_parser.add_argument("--subproject", required=True)
    resume_parser.add_argument("--run-id", default=None)
    harvest_parser = subparsers.add_parser("pdf-harvest", help="Download PDFs for a focused topic from an existing run")
    harvest_parser.add_argument("--subproject", required=True)
    harvest_parser.add_argument("--run-id", required=True)
    harvest_parser.add_argument("--query", default="")
    harvest_parser.add_argument("--limit", type=int, default=250)
    args = parser.parse_args()
    root_dir = Path(args.root).resolve()

    if args.command == "init":
        initialize_repo(root_dir)
        print(f"Initialized SMI repository: {root_dir}")
    elif args.command == "create-subproject":
        initialize_repo(root_dir)
        print(f"Created subproject: {create_subproject(root_dir, args.name)}")
    elif args.command == "write-example-config":
        Path(args.path).write_text(EXAMPLE_CONFIG, encoding="utf-8")
        print(f"Wrote example config: {args.path}")
    elif args.command == "run":
        ctx = create_run(root_dir, args.subproject, Path(args.config).resolve(), args.api_base_url)
        run_workflow(ctx, resume=False)
    elif args.command == "resume":
        run_id = args.run_id or find_latest_run(root_dir, args.subproject)
        if not run_id:
            raise SystemExit(f"No run found for subproject: {args.subproject}")
        metadata_path = root_dir / "subprojects" / args.subproject / "runs" / run_id / "metadata.json"
        metadata = load_json(metadata_path, default={})
        config_path = Path(metadata.get("config_path", "config.yaml")).resolve()
        ctx = WorkflowContext(root_dir=root_dir, subproject=args.subproject, run_id=run_id, api_base_url=args.api_base_url, config=load_yaml(config_path))
        run_workflow(ctx, resume=True)
    elif args.command == "pdf-harvest":
        metadata_path = root_dir / "subprojects" / args.subproject / "runs" / args.run_id / "metadata.json"
        metadata = load_json(metadata_path, default={})
        config_path = Path(metadata.get("config_path", "config.yaml")).resolve()
        config = load_yaml(config_path) if config_path.exists() else {}
        ctx = WorkflowContext(root_dir=root_dir, subproject=args.subproject, run_id=args.run_id, api_base_url=args.api_base_url, config=config)
        result = run_targeted_pdf_harvest(ctx, args.query, args.limit)
        print(json.dumps({k: result.get(k) for k in ["status", "topic_query", "selected_reference_count", "successful_pdf_count", "package_zip_count", "failed_batch_count"]}, indent=2))


if __name__ == "__main__":
    main()
