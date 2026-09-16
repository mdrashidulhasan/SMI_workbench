from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import random
import re
import threading
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, quote

import requests

from smi_intelligence import HEW_CONTROLLED_VOCABULARY, extract_hew_resource_library
from smi_workflow import (
    DEFAULT_API_BASE_URL,
    repair_search_query_text,
    response_record_count,
    zero_result_title_fallback_queries,
    extract_records_from_response,
    find_response_id,
)

VALIDATION_ROOT = Path(os.environ.get("SMI_VALIDATION_ROOT", "./smi/validation")).resolve()
VALIDATION_ROOT.mkdir(parents=True, exist_ok=True)

_PRIMARY_FIELDS: List[Tuple[str, str]] = [
    ("reference_type", "Reference Type"),
    ("exposure_l1", "Exposure L1"),
    ("exposure_l2", "Exposure L2"),
    ("health_impact_l1", "Health Impact L1"),
    ("health_impact_l2", "Health Impact L2"),
    ("health_impact_l3", "Health Impact L3"),
    ("geography_l1", "Geography L1"),
    ("geography_l2", "Geography L2"),
    ("geographic_feature", "Geographic Feature"),
    ("data_resource_type_l1", "Data Resource Type L1"),
    ("data_resource_type_l2", "Data Resource Type L2"),
    ("model_type", "Model Type"),
    ("special_topic_l1", "Special Topic L1"),
    ("special_topic_l2", "Special Topic L2"),
]

_LASERAI_COLUMNS = {
    "reference_type": "Reference Type (extracted)",
    "exposure_l1": "Exposure L1 (extracted)",
    "exposure_l2": "Exposure L2 (extracted)",
    "health_impact_l1": "Health Impact L1 (extracted)",
    "health_impact_l2": "Health Impact L2 (extracted)",
    "health_impact_l3": "Health Impact L3 (extracted)",
    "geography_l1": "Location L1 (extracted)",
    "geography_l2": "Location L2 (extracted)",
    "geographic_feature": "Geographic Feature (extracted)",
    "data_resource_type_l1": "Data Resource Type L1 (extracted)",
    "data_resource_type_l2": "Data Resource Type L2 (extracted)",
    "model_type": "Model Type (extracted)",
    "special_topic_l1": "Special Topics L1 (extracted)",
    "special_topic_l2": "Special Topics L2 (extracted)",
}

_STOP_VALUES = {"", "not reported", "none", "n/a", "na", "not applicable"}

# Validation-only equivalence map.  These aliases do NOT change Structured
# Evidence extraction.  They only prevent a spelling/abbreviation/plural variant
# from being counted as both a false negative and a false positive.
_FIELD_LABEL_ALIASES: Dict[str, Dict[str, str]] = {
    "health_impact_l3": {
        "ptsd": "post traumatic stress disorder",
        "ptsd post traumatic stress disorder": "post traumatic stress disorder",
        "post traumatic stress disorder": "post traumatic stress disorder",
    },
    "health_impact_l2": {
        "birth outcome": "birth outcome",
        "birth outcomes": "birth outcome",
        "chronic obstructive pulmonary disease": "chronic obstructive pulmonary disease",
        "chronic obstructive pulmonary disease copd": "chronic obstructive pulmonary disease",
    },
    "exposure_l1": {
        # Typographic variants observed in the LaserAI export.
        "human confict violence": "human conflict violence",
        "human conflict violence": "human conflict violence",
        "glacier melt snow melt": "glacier snow melt",
        "glacier snow melt": "glacier snow melt",
    },
    "exposure_l2": {
        "lightening storm": "lightning storm",
        "lightning storm": "lightning storm",
    },
}
_ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()
_NCBI_LOCK = threading.Lock()
_NCBI_LAST_REQUEST = 0.0


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _validation_dir(validation_id: str) -> Path:
    clean = re.sub(r"[^A-Za-z0-9_.-]", "", str(validation_id or ""))
    return VALIDATION_ROOT / clean


def _col_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref or "")
    if not letters:
        return 0
    value = 0
    for ch in letters.group(0):
        value = value * 26 + (ord(ch) - 64)
    return value - 1


def _xlsx_first_sheet_rows(content: bytes) -> Iterable[List[Any]]:
    """Read the first XLSX worksheet using only the Python standard library."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("{*}si"):
                shared.append("".join(si.itertext()))

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet = workbook.find(".//{*}sheet")
        if sheet is None:
            return
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        target = None
        for rel in rels.findall("{*}Relationship"):
            if rel.attrib.get("Id") == rel_id:
                target = rel.attrib.get("Target")
                break
        if not target:
            raise ValueError("Could not locate the first worksheet in the uploaded XLSX file.")
        sheet_path = target.lstrip("/")
        if not sheet_path.startswith("xl/"):
            sheet_path = "xl/" + sheet_path
        sheet_root = ET.fromstring(zf.read(sheet_path))
        for row_node in sheet_root.findall(".//{*}sheetData/{*}row"):
            cells: Dict[int, Any] = {}
            max_idx = -1
            for cell in row_node.findall("{*}c"):
                idx = _col_index(cell.attrib.get("r", ""))
                max_idx = max(max_idx, idx)
                cell_type = cell.attrib.get("t", "")
                value_node = cell.find("{*}v")
                inline_node = cell.find("{*}is")
                value: Any = None
                if cell_type == "inlineStr" and inline_node is not None:
                    value = "".join(inline_node.itertext())
                elif value_node is not None:
                    raw = value_node.text or ""
                    if cell_type == "s":
                        try:
                            value = shared[int(raw)]
                        except Exception:
                            value = raw
                    elif cell_type == "b":
                        value = raw == "1"
                    elif cell_type in {"str", "e"}:
                        value = raw
                    else:
                        try:
                            value = int(raw)
                        except Exception:
                            try:
                                value = float(raw)
                            except Exception:
                                value = raw
                cells[idx] = value
            yield [cells.get(i) for i in range(max_idx + 1)]


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_doi(value: Any) -> str:
    text = _clean_cell(value).lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    return text.strip().rstrip(".")


def _numeric_pmid(value: Any) -> str:
    text = _clean_cell(value)
    if re.fullmatch(r"\d+(?:\.0)?", text):
        return text.split(".", 1)[0]
    return ""


def _normalize_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean_cell(value)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _canonical_label(field: str, value: Any) -> str:
    normalized = _normalize_label(value)
    return _FIELD_LABEL_ALIASES.get(field, {}).get(normalized, normalized)



_DEVELOPMENT_TITLES = {
    _normalize_label(title) for title in [
        "Leveraging Climate Data for Dengue Forecasting in Ba Ria Vung Tau Province, Vietnam: An Advanced Machine Learning Approach",
        "Maternal exposure to heat and its association with miscarriage in rural KwaZulu-Natal, South Africa: A population-based cohort study",
        "Symptoms Reported by Young Adults With Asthma During Wildfire Smoke Season",
        "Evolution of Spatial Risk of Malaria Infection After a Pragmatic Chemoprevention Program in Response to Severe Flooding in Rural Western Uganda",
        "Psychotropic Medication Prescriptions and Large California Wildfires",
        "Association between ambient temperature and common allergenic pollen and fungal spores: A 52-year analysis in central England, United Kingdom",
        "Investigating the association between floods and low birth weight in India: Using the geospatial approach",
        "Towards an intelligent malaria outbreak warning model based intelligent malaria outbreak warning in the northern part of Benin, West Africa",
        "Deep Learning Models for Health-Driven Forecasting of Indoor Temperatures in Heat Waves in Canada: An Exploratory Study Using Smart Thermostats",
        "Geospatial Patterns of Non-Melanoma Skin Cancer in Relation to Climate Changes in Iran",
    ]
}

def _display_set(values: Iterable[str]) -> List[str]:
    return sorted({_clean_cell(v) for v in values if _clean_cell(v)}, key=lambda x: x.lower())


def _valid_gold(value: Any) -> bool:
    return _normalize_label(value) not in _STOP_VALUES


def parse_laserai_export(content: bytes) -> List[Dict[str, Any]]:
    rows = iter(_xlsx_first_sheet_rows(content))
    try:
        headers = [_clean_cell(v) for v in next(rows)]
    except StopIteration:
        raise ValueError("The uploaded workbook is empty.")
    index = {name: i for i, name in enumerate(headers)}
    required = ["Title", "DOI", "Accession Number"] + list(_LASERAI_COLUMNS.values())
    missing = [name for name in required if name not in index]
    if missing:
        raise ValueError("This does not look like the expected LaserAI export. Missing columns: " + ", ".join(missing))

    publications: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        def value(name: str) -> Any:
            i = index.get(name, -1)
            return row[i] if i >= 0 and i < len(row) else None

        title = _clean_cell(value("Title"))
        if not title:
            continue
        doi = _normalize_doi(value("DOI"))
        accession = _clean_cell(value("Accession Number"))
        ref_number = _clean_cell(value("Reference number")) if "Reference number" in index else ""
        # LaserAI can repeat the same publication with inconsistent reference numbers
        # or identifier variants across coding rows. Collapse by normalized title so the
        # benchmark represents publications rather than coding records.
        key = "title:" + unicodedata.normalize("NFKC", title).casefold()
        pub = publications.setdefault(key, {
            "benchmark_key": key,
            "title": title,
            "doi": doi,
            "accession_number": accession,
            "pmid": _numeric_pmid(accession),
            "reference_number": ref_number,
            "first_author": _clean_cell(value("1st Author")) if "1st Author" in index else "",
            "year": _clean_cell(value("Year")) if "Year" in index else "",
            "gold": {field: set() for field, _ in _PRIMARY_FIELDS},
        })
        if not pub.get("doi") and doi:
            pub["doi"] = doi
        if not pub.get("pmid"):
            pub["pmid"] = _numeric_pmid(accession)
        for field, column in _LASERAI_COLUMNS.items():
            raw = _clean_cell(value(column))
            if raw and _valid_gold(raw):
                pub["gold"][field].add(raw)

    output: List[Dict[str, Any]] = []
    for pub in publications.values():
        pub["gold"] = {field: _display_set(values) for field, values in pub["gold"].items()}
        output.append(pub)
    output.sort(key=lambda p: (str(p.get("year", "")), p.get("title", "")), reverse=True)
    return output


def _stratum(pub: Dict[str, Any]) -> str:
    for field in ("exposure_l1", "health_impact_l1", "special_topic_l1", "model_type", "reference_type"):
        vals = pub.get("gold", {}).get(field) or []
        if vals:
            return f"{field}:{vals[0]}"
    return "other"


def select_validation_sample(publications: Sequence[Dict[str, Any]], sample_size: str, sampling_mode: str, seed: int) -> List[Dict[str, Any]]:
    pubs = list(publications)
    if str(sample_size).lower() == "all":
        return pubs
    try:
        n = max(1, min(int(sample_size), len(pubs)))
    except Exception:
        n = min(50, len(pubs))
    rng = random.Random(seed)
    if sampling_mode == "random":
        return rng.sample(pubs, n)

    # Round-robin stratified sampling across the available controlled-vocabulary families.
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for pub in pubs:
        buckets[_stratum(pub)].append(pub)
    for vals in buckets.values():
        rng.shuffle(vals)
    keys = list(buckets)
    rng.shuffle(keys)
    selected: List[Dict[str, Any]] = []
    while len(selected) < n and keys:
        next_keys: List[str] = []
        for key in keys:
            if buckets[key] and len(selected) < n:
                selected.append(buckets[key].pop())
            if buckets[key]:
                next_keys.append(key)
        keys = next_keys
    return selected


def _flatten_nested(values: Any) -> List[str]:
    out: List[str] = []
    if isinstance(values, dict):
        for key, child in values.items():
            out.append(str(key))
            out.extend(_flatten_nested(child))
    elif isinstance(values, list):
        for item in values:
            if isinstance(item, (dict, list)):
                out.extend(_flatten_nested(item))
            else:
                out.append(str(item))
    return out


def _field_vocabularies() -> Dict[str, set[str]]:
    exposure = HEW_CONTROLLED_VOCABULARY.get("exposure", {})
    health = HEW_CONTROLLED_VOCABULARY.get("health_impact", {})
    geography = HEW_CONTROLLED_VOCABULARY.get("geographic_location", {})
    special = HEW_CONTROLLED_VOCABULARY.get("special_topics", {})

    exposure_l2: List[str] = []
    for child in exposure.values():
        exposure_l2.extend(_flatten_nested(child))

    health_l2: List[str] = []
    health_l3: List[str] = []
    for child in health.values():
        if isinstance(child, dict):
            for level2, level3 in child.items():
                health_l2.append(str(level2))
                health_l3.extend(_flatten_nested(level3))
        else:
            health_l2.extend(_flatten_nested(child))

    geography_l2: List[str] = []
    for child in geography.values():
        geography_l2.extend(_flatten_nested(child))

    special_l2: List[str] = []
    for child in special.values():
        special_l2.extend(_flatten_nested(child))

    return {
        "reference_type": {_canonical_label("reference_type", v) for v in HEW_CONTROLLED_VOCABULARY.get("reference_type", [])},
        "exposure_l1": {_canonical_label("exposure_l1", v) for v in exposure.keys()},
        "exposure_l2": {_canonical_label("exposure_l2", v) for v in exposure_l2},
        "health_impact_l1": {_canonical_label("health_impact_l1", v) for v in health.keys()},
        "health_impact_l2": {_canonical_label("health_impact_l2", v) for v in health_l2},
        "health_impact_l3": {_canonical_label("health_impact_l3", v) for v in health_l3},
        "geography_l1": {_canonical_label("geography_l1", v) for v in geography.keys()},
        "geography_l2": {_canonical_label("geography_l2", v) for v in geography_l2},
        "geographic_feature": {_canonical_label("geographic_feature", v) for v in HEW_CONTROLLED_VOCABULARY.get("geographic_feature", [])},
        "data_resource_type_l1": {_canonical_label("data_resource_type_l1", v) for v in HEW_CONTROLLED_VOCABULARY.get("data_resource_type", []) if _normalize_label(v) != _normalize_label("Source Cohort Publication")},
        "data_resource_type_l2": {_canonical_label("data_resource_type_l2", "Source Cohort Publication")},
        "model_type": {_canonical_label("model_type", v) for v in HEW_CONTROLLED_VOCABULARY.get("model_type", [])},
        "special_topic_l1": {_canonical_label("special_topic_l1", v) for v in special.keys()},
        "special_topic_l2": {_canonical_label("special_topic_l2", v) for v in special_l2},
    }


_FIELD_VOCAB = _field_vocabularies()


def _split_smi(value: Any) -> List[str]:
    vals = []
    for item in re.split(r"\s*;\s*", _clean_cell(value)):
        item = item.strip()
        if item and _normalize_label(item) not in _STOP_VALUES:
            vals.append(item)
    return _display_set(vals)


def _structured_prediction(record: Dict[str, Any]) -> Dict[str, List[str]]:
    rows = extract_hew_resource_library([record])
    row = rows[0] if rows else {}
    data_resource = _split_smi(row.get("data_resource_type"))
    l2_norm = {_normalize_label("Source Cohort Publication")}
    return {
        "reference_type": _split_smi(row.get("reference_type")),
        "exposure_l1": _split_smi(row.get("exposure_l1")),
        "exposure_l2": _split_smi(row.get("exposure_l2")),
        "health_impact_l1": _split_smi(row.get("health_impact_l1")),
        "health_impact_l2": _split_smi(row.get("health_impact_l2")),
        "health_impact_l3": _split_smi(row.get("health_impact_l3")),
        "geography_l1": _split_smi(row.get("geography_l1")),
        "geography_l2": _split_smi(row.get("geography_l2")),
        "geographic_feature": _split_smi(row.get("geographic_feature")),
        "data_resource_type_l1": [v for v in data_resource if _normalize_label(v) not in l2_norm],
        "data_resource_type_l2": [v for v in data_resource if _normalize_label(v) in l2_norm],
        "model_type": _split_smi(row.get("model_type")),
        "special_topic_l1": _split_smi(row.get("special_topic_l1")),
        "special_topic_l2": _split_smi(row.get("special_topic_l2")),
        "_evidence_sentence": [_clean_cell(row.get("evidence_sentence"))],
        "_confidence": [_clean_cell(row.get("confidence"))],
    }


def _compare_labels(gold: Dict[str, List[str]], pred: Dict[str, List[str]]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    all_tp = all_fp = all_fn = 0
    vocabulary_gaps: List[Dict[str, str]] = []
    normalization_matches: List[Dict[str, str]] = []
    for field, label in _PRIMARY_FIELDS:
        gold_values = gold.get(field) or []
        pred_values = pred.get(field) or []
        gold_norm = {_canonical_label(field, v): v for v in gold_values}
        pred_norm = {_canonical_label(field, v): v for v in pred_values}
        tp_keys = set(gold_norm) & set(pred_norm)
        fp_keys = set(pred_norm) - set(gold_norm)
        fn_keys = set(gold_norm) - set(pred_norm)
        tp, fp, fn = len(tp_keys), len(fp_keys), len(fn_keys)
        all_tp += tp; all_fp += fp; all_fn += fn
        alias_matches: List[Dict[str, str]] = []
        for key in tp_keys:
            gold_display = gold_norm[key]
            pred_display = pred_norm[key]
            if _normalize_label(gold_display) != _normalize_label(pred_display):
                item = {"field": label, "gold": gold_display, "predicted": pred_display}
                alias_matches.append(item)
                normalization_matches.append(item)
        for key in fn_keys:
            if key not in _FIELD_VOCAB.get(field, set()):
                vocabulary_gaps.append({"field": label, "term": gold_norm[key]})
        precision = tp / (tp + fp) if (tp + fp) else (1.0 if not fn else 0.0)
        recall = tp / (tp + fn) if (tp + fn) else (1.0 if not fp else 0.0)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        fields[field] = {
            "label": label,
            "gold": _display_set(gold_values),
            "predicted": _display_set(pred_values),
            "matched": _display_set(gold_norm[k] for k in tp_keys),
            "missing": _display_set(gold_norm[k] for k in fn_keys),
            "extra": _display_set(pred_norm[k] for k in fp_keys),
            "normalization_matches": alias_matches,
            "tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1,
            "exact": not fp_keys and not fn_keys,
        }
    precision = all_tp / (all_tp + all_fp) if (all_tp + all_fp) else (1.0 if not all_fn else 0.0)
    recall = all_tp / (all_tp + all_fn) if (all_tp + all_fn) else (1.0 if not all_fp else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "fields": fields,
        "tp": all_tp, "fp": all_fp, "fn": all_fn,
        "precision": precision, "recall": recall, "f1": f1,
        "exact_all_fields": all(v["exact"] for v in fields.values()),
        "vocabulary_gaps": vocabulary_gaps,
        "normalization_matches": normalization_matches,
    }


def _api_verify_path() -> Any:
    custom = os.environ.get("SMI_CA_BUNDLE", "").strip()
    if custom and Path(custom).exists():
        return custom
    local = Path("./macos-ca-bundle.pem")
    return str(local) if local.exists() else True


def _validation_search_timeout() -> int:
    raw = os.environ.get("SMI_VALIDATION_SEARCH_TIMEOUT") or os.environ.get("SMI_API_TIMEOUT") or "900"
    try:
        return max(30, int(raw))
    except Exception:
        return 900


def _smi_search_once(query: str, timeout: int) -> Tuple[Optional[int], Dict[str, Any]]:
    api_key = os.environ.get("CONNECT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("CONNECT_API_KEY is not available to the validation process.")
    payload: Dict[str, Any] = {
        "query": query,
        "database": ["pubmed"],
        "limit": 5,
        "max_results": 5,
        "max_records": 5,
        "number_of_records": 5,
    }
    response = requests.post(
        DEFAULT_API_BASE_URL.rstrip("/") + "/search",
        json=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Key {api_key}"},
        timeout=timeout,
        verify=_api_verify_path(),
    )
    response.raise_for_status()
    data = response.json()
    return response_record_count(data), data


def _retryable_request_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.ReadTimeout, requests.ConnectTimeout, requests.ConnectionError)):
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status in {429, 500, 502, 503, 504}


def _search_query_with_retries(query: str, timeout: int) -> Tuple[Optional[int], Dict[str, Any], List[str]]:
    try:
        retries = max(0, min(int(os.environ.get("SMI_VALIDATION_SEARCH_RETRIES", "1")), 4))
    except Exception:
        retries = 1
    try:
        sleep_seconds = max(0.0, float(os.environ.get("SMI_VALIDATION_SEARCH_RETRY_SLEEP_SECONDS", "2")))
    except Exception:
        sleep_seconds = 2.0
    errors: List[str] = []
    for attempt in range(retries + 1):
        try:
            count, data = _smi_search_once(query, timeout)
            return count, data, errors
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt >= retries or not _retryable_request_error(exc):
                break
            time.sleep(sleep_seconds * (attempt + 1))
    return None, {}, errors


def _deep_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return _clean_cell(value)
    if isinstance(value, list):
        return _clean_cell(" ".join(_deep_text(v) for v in value if _deep_text(v)))
    if isinstance(value, dict):
        for preferred in ("#text", "text", "value", "content", "name"):
            if preferred in value and _deep_text(value.get(preferred)):
                return _deep_text(value.get(preferred))
        return _clean_cell(" ".join(_deep_text(v) for v in value.values() if _deep_text(v)))
    return _clean_cell(value)


def _deep_find(obj: Any, keys: Sequence[str]) -> str:
    wanted = {str(k).casefold() for k in keys}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).casefold() in wanted:
                text = _deep_text(value)
                if text:
                    return text
        for value in obj.values():
            found = _deep_find(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _deep_find(value, keys)
            if found:
                return found
    return ""


def _normalize_api_record(record: Dict[str, Any]) -> Dict[str, Any]:
    title = _deep_find(record, ["title", "article_title", "articleTitle", "publication_title", "publicationTitle"])
    abstract = _deep_find(record, ["abstract", "abstract_text", "abstractText", "summary", "description"])
    pmid = _numeric_pmid(_deep_find(record, ["pmid", "pubmed_id", "pubmedId", "accession_number", "accessionNumber"]))
    doi = _normalize_doi(_deep_find(record, ["doi", "DOI", "digital_object_identifier", "digitalObjectIdentifier"]))
    journal = _deep_find(record, ["journal", "journal_title", "journalTitle", "source"])
    return {"title": title, "abstract": abstract, "pmid": pmid, "doi": doi, "journal": journal}


def _identity_details(pub: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    title = _clean_cell(record.get("title"))
    doi = _normalize_doi(record.get("doi"))
    pmid = _numeric_pmid(record.get("pmid"))
    title_match = bool(title and _normalize_label(title) == _normalize_label(pub.get("title", "")))
    doi_match = bool(pub.get("doi") and doi and _normalize_doi(pub.get("doi")) == doi)
    pmid_match = bool(pub.get("pmid") and pmid and str(pub.get("pmid")) == pmid)
    return {
        "title_match": title_match,
        "doi_match": doi_match,
        "pmid_match": pmid_match,
        "identity_match": title_match or doi_match or pmid_match,
    }


def _matching_api_record(pub: Dict[str, Any], records: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for raw in records:
        record = _normalize_api_record(raw)
        ident = _identity_details(pub, record)
        score = (100 if ident["pmid_match"] else 0) + (90 if ident["doi_match"] else 0) + (80 if ident["title_match"] else 0)
        if score and (best is None or score > best[0]):
            best = (score, record)
    return best[1] if best else None


def _search_response_candidate(pub: Dict[str, Any], data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _matching_api_record(pub, extract_records_from_response(data))


def _search_response_result_id(data: Dict[str, Any]) -> str:
    return str(find_response_id(data, ["references_id", "reference_id", "referencesId", "result_id", "resultId", "search_id", "searchId"]) or "")


def test_smi_search(pub: Dict[str, Any]) -> Dict[str, Any]:
    timeout = _validation_search_timeout()
    title = repair_search_query_text(pub.get("title", ""))
    attempts: List[Dict[str, Any]] = []
    title_found = False
    identifier_found = False
    query_used = ""
    errors: List[str] = []
    source_candidate: Optional[Dict[str, Any]] = None
    result_id = ""

    candidates = [title] + zero_result_title_fallback_queries(title)
    seen_queries = set()
    for i, query in enumerate(candidates):
        query = _clean_cell(query)
        if not query or query in seen_queries:
            continue
        seen_queries.add(query)
        count, data, query_errors = _search_query_with_retries(query, timeout)
        attempts.append({
            "kind": "title" if i == 0 else "title_fallback",
            "query": query,
            "record_count": count,
            "errors": query_errors,
        })
        errors.extend(query_errors)
        if data:
            source_candidate = source_candidate or _search_response_candidate(pub, data)
            result_id = result_id or _search_response_result_id(data)
        if data and (count is None or count > 0):
            title_found = True
            query_used = query
            break

    if not title_found:
        identifier_queries: List[Tuple[str, str]] = []
        if pub.get("pmid"):
            identifier_queries.append(("pmid", str(pub["pmid"])))
        if pub.get("doi"):
            identifier_queries.append(("doi", str(pub["doi"])))
        for kind, query in identifier_queries:
            if query in seen_queries:
                continue
            seen_queries.add(query)
            count, data, query_errors = _search_query_with_retries(query, timeout)
            attempts.append({"kind": kind, "query": query, "record_count": count, "errors": query_errors})
            errors.extend(query_errors)
            if data:
                source_candidate = source_candidate or _search_response_candidate(pub, data)
                result_id = result_id or _search_response_result_id(data)
            if data and (count is None or count > 0):
                identifier_found = True
                query_used = query
                break

    # An error is reported only if nothing was found.  Individual failed attempts
    # remain visible in `attempts` even when a fallback succeeds.
    unique_errors = list(dict.fromkeys(errors))
    status = "title_found" if title_found else ("identifier_rescue" if identifier_found else ("error" if unique_errors else "not_found"))
    return {
        "status": status,
        "title_found": title_found,
        "identifier_rescue_found": identifier_found,
        "query_used": query_used,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "result_id": result_id,
        "error": " | ".join(unique_errors[-4:]) if status == "error" else "",
        "_source_candidate": source_candidate,
    }


def _ncbi_get(url: str, params: Dict[str, Any], timeout: int = 30) -> requests.Response:
    global _NCBI_LAST_REQUEST
    with _NCBI_LOCK:
        wait = 0.36 - (time.monotonic() - _NCBI_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(
            url,
            params=params,
            timeout=timeout,
            headers={"User-Agent": "SMI-Workbench-Validation/1.1"},
        )
        _NCBI_LAST_REQUEST = time.monotonic()
    response.raise_for_status()
    return response


def _pubmed_find_pmid_by_doi(doi: str) -> str:
    if not doi:
        return ""
    response = _ncbi_get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        {"db": "pubmed", "term": f'"{doi}"[AID]', "retmode": "json", "retmax": 3, "tool": "smi_workbench"},
    )
    data = response.json()
    ids = data.get("esearchresult", {}).get("idlist", [])
    return str(ids[0]) if ids else ""


def fetch_pubmed_source(pub: Dict[str, Any]) -> Dict[str, Any]:
    benchmark_pmid = str(pub.get("pmid") or "").strip()
    pmid = benchmark_pmid
    source_method = "pubmed_pmid" if pmid else "pubmed_doi"
    if not pmid and pub.get("doi"):
        try:
            pmid = _pubmed_find_pmid_by_doi(str(pub.get("doi") or ""))
        except Exception as exc:
            return {"available": False, "source_method": "pubmed_doi", "independent_source": True, "error": f"DOI-to-PubMed lookup failed: {type(exc).__name__}: {exc}"}
    if not pmid:
        return {"available": False, "source_method": source_method, "independent_source": True, "error": "No PubMed PMID could be resolved from the benchmark identifier."}
    try:
        response = _ncbi_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            {"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "smi_workbench"},
        )
        root = ET.fromstring(response.content)
        article = root.find(".//PubmedArticle")
        if article is None:
            return {"available": False, "pmid": pmid, "source_method": source_method, "independent_source": True, "error": f"PubMed returned no article for PMID {pmid}."}
        title_node = article.find(".//ArticleTitle")
        title = "".join(title_node.itertext()).strip() if title_node is not None else ""
        abstract_parts: List[str] = []
        for node in article.findall(".//Abstract/AbstractText"):
            text = "".join(node.itertext()).strip()
            label = node.attrib.get("Label", "").strip()
            if text:
                abstract_parts.append((label + ": " if label else "") + text)
        abstract = " ".join(abstract_parts)
        doi = ""
        for aid in article.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi":
                doi = (aid.text or "").strip()
        journal_node = article.find(".//Journal/Title")
        journal = "".join(journal_node.itertext()).strip() if journal_node is not None else ""
        record = {"title": title, "abstract": abstract, "pmid": pmid, "doi": doi or pub.get("doi", ""), "journal": journal}
        ident = _identity_details(pub, record)
        return {
            "available": bool(title or abstract),
            "source_method": source_method,
            "independent_source": True,
            "content_level": "title_abstract" if abstract else "title_only",
            "pmid": pmid,
            "title": title,
            "doi": doi,
            **ident,
            "record": record,
            "error": "",
        }
    except Exception as exc:
        return {"available": False, "pmid": pmid, "source_method": source_method, "independent_source": True, "error": f"{type(exc).__name__}: {exc}"}


def _public_json_get(url: str, params: Dict[str, Any], timeout: int = 35) -> Dict[str, Any]:
    try:
        retries = max(0, min(int(os.environ.get("SMI_VALIDATION_SOURCE_RETRIES", "1")), 3))
    except Exception:
        retries = 1
    errors: List[str] = []
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "SMI-Workbench-Validation/1.1"})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt >= retries or not _retryable_request_error(exc):
                raise RuntimeError(" | ".join(errors[-3:])) from exc
            time.sleep(1.5 * (attempt + 1))
    return {}


def fetch_europe_pmc_source(pub: Dict[str, Any]) -> Dict[str, Any]:
    doi = _normalize_doi(pub.get("doi"))
    if not doi:
        return {"available": False, "source_method": "europe_pmc", "independent_source": True, "error": "No DOI available for Europe PMC fallback."}
    try:
        data = _public_json_get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            {"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": 5},
        )
        results = data.get("resultList", {}).get("result", []) if isinstance(data, dict) else []
        for item in results if isinstance(results, list) else []:
            record = {
                "title": _clean_cell(item.get("title")),
                "abstract": _clean_cell(item.get("abstractText")),
                "pmid": _numeric_pmid(item.get("pmid")),
                "doi": _normalize_doi(item.get("doi")) or doi,
                "journal": _clean_cell(item.get("journalTitle")),
            }
            ident = _identity_details(pub, record)
            if ident["identity_match"]:
                return {
                    "available": bool(record["title"] or record["abstract"]),
                    "source_method": "europe_pmc_doi",
                    "independent_source": True,
                    "content_level": "title_abstract" if record["abstract"] else "title_only",
                    "pmid": record["pmid"], "title": record["title"], "doi": record["doi"],
                    **ident,
                    "record": record,
                    "error": "",
                }
        return {"available": False, "source_method": "europe_pmc_doi", "independent_source": True, "error": "Europe PMC returned no identity-matched record for the DOI."}
    except Exception as exc:
        return {"available": False, "source_method": "europe_pmc_doi", "independent_source": True, "error": f"{type(exc).__name__}: {exc}"}


def _retrieve_smi_search_record(result_id: str, pub: Dict[str, Any], timeout: int) -> Optional[Dict[str, Any]]:
    if not result_id:
        return None
    api_key = os.environ.get("CONNECT_API_KEY", "").strip()
    if not api_key:
        return None
    result_types: List[str] = []
    for item in [
        os.environ.get("SMI_VALIDATION_RETRIEVAL_TYPE", ""),
        "references", "records", "citation", "citations", "search", "deduped", "screened",
    ]:
        item = _clean_cell(item)
        if item and item not in result_types:
            result_types.append(item)
    for result_type in result_types:
        try:
            response = requests.get(
                DEFAULT_API_BASE_URL.rstrip("/") + "/results",
                params={"id": result_id, "type": result_type, "start_index": 0, "number_of_records": 10},
                headers={"Content-Type": "application/json", "Authorization": f"Key {api_key}"},
                timeout=timeout,
                verify=_api_verify_path(),
            )
            response.raise_for_status()
            data = response.json()
            match = _matching_api_record(pub, extract_records_from_response(data))
            if match:
                return match
        except Exception:
            continue
    return None


def fetch_smi_search_source(pub: Dict[str, Any], search: Dict[str, Any], embedded_candidate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    record = embedded_candidate
    if not record:
        record = _retrieve_smi_search_record(str(search.get("result_id") or ""), pub, _validation_search_timeout())
    if not record:
        return {"available": False, "source_method": "smi_search_record", "independent_source": False, "error": "No identity-matched source record could be recovered from the SMI search result."}
    ident = _identity_details(pub, record)
    return {
        "available": bool(record.get("title") or record.get("abstract")),
        "source_method": "smi_search_record",
        "independent_source": False,
        "content_level": "title_abstract" if record.get("abstract") else "title_only",
        "pmid": record.get("pmid", ""), "title": record.get("title", ""), "doi": record.get("doi", ""),
        **ident,
        "record": record,
        "error": "",
    }


def fetch_validation_source(pub: Dict[str, Any], search: Dict[str, Any], embedded_candidate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = []
    pubmed = fetch_pubmed_source(pub)
    attempts.append({"method": pubmed.get("source_method", "pubmed"), "available": pubmed.get("available", False), "identity_match": pubmed.get("identity_match", False), "error": pubmed.get("error", "")})
    if pubmed.get("available") and pubmed.get("identity_match"):
        pubmed["source_attempts"] = attempts
        return pubmed

    # DOI-based non-PubMed fallback.  This is independent of SMI's own search
    # result and therefore preferred when available.
    if pub.get("doi"):
        epmc = fetch_europe_pmc_source(pub)
        attempts.append({"method": epmc.get("source_method", "europe_pmc_doi"), "available": epmc.get("available", False), "identity_match": epmc.get("identity_match", False), "error": epmc.get("error", "")})
        if epmc.get("available") and epmc.get("identity_match"):
            epmc["source_attempts"] = attempts
            return epmc

    # Last-resort source fallback: use the identity-matched record returned by
    # the same SMI search being evaluated.  This maximizes coverage but is marked
    # non-independent so it can be reported separately.
    smi_source = fetch_smi_search_source(pub, search, embedded_candidate)
    attempts.append({"method": "smi_search_record", "available": smi_source.get("available", False), "identity_match": smi_source.get("identity_match", False), "error": smi_source.get("error", "")})
    if smi_source.get("available") and smi_source.get("identity_match"):
        smi_source["source_attempts"] = attempts
        return smi_source

    errors = [a.get("error", "") for a in attempts if a.get("error")]
    return {
        "available": False,
        "identity_match": False,
        "source_method": "unavailable",
        "independent_source": False,
        "content_level": "",
        "source_attempts": attempts,
        "error": " | ".join(errors[-4:]) or "No identity-matched validation source was available.",
    }


def _validate_one(pub: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    search = test_smi_search(pub)
    embedded_candidate = search.pop("_source_candidate", None)
    source = fetch_validation_source(pub, search, embedded_candidate)
    result: Dict[str, Any] = {
        "benchmark_key": pub.get("benchmark_key"),
        "reference_number": pub.get("reference_number"),
        "title": pub.get("title"),
        "doi": pub.get("doi"),
        "accession_number": pub.get("accession_number"),
        "pmid": source.get("pmid") or pub.get("pmid"),
        "year": pub.get("year"),
        "first_author": pub.get("first_author"),
        "gold": pub.get("gold", {}),
        "search": search,
        "source": {k: v for k, v in source.items() if k != "record"},
        "structured_evidence": {},
        "comparison": {},
        "elapsed_seconds": 0.0,
    }
    # Never score a mismatched source.  This keeps search/identity failures from
    # silently becoming extraction failures.
    if source.get("available") and source.get("identity_match") and source.get("record"):
        try:
            pred = _structured_prediction(source["record"])
            comparison = _compare_labels(pub.get("gold", {}), pred)
            result["structured_evidence"] = pred
            result["comparison"] = comparison
        except Exception as exc:
            result["extraction_error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def _aggregate(results: Sequence[Dict[str, Any]], benchmark_total: int, selected_total: int) -> Dict[str, Any]:
    field_totals: Dict[str, Dict[str, Any]] = {
        field: {"field": field, "label": label, "tp": 0, "fp": 0, "fn": 0, "papers_scored": 0, "papers_exact": 0}
        for field, label in _PRIMARY_FIELDS
    }
    search_counts = Counter()
    source_available = 0
    identity_matches = 0
    independent_sources = 0
    source_methods: Counter[str] = Counter()
    source_content_levels: Counter[str] = Counter()
    extraction_scored = 0
    vocabulary_gaps: Counter[Tuple[str, str]] = Counter()
    normalization_matches: Counter[Tuple[str, str, str]] = Counter()
    missing_terms: Counter[Tuple[str, str]] = Counter()
    extra_terms: Counter[Tuple[str, str]] = Counter()
    total_tp = total_fp = total_fn = 0

    for result in results:
        search_counts[result.get("search", {}).get("status", "unknown")] += 1
        source_info = result.get("source", {})
        if source_info.get("available"):
            source_available += 1
            source_methods[str(source_info.get("source_method") or "unknown")] += 1
            source_content_levels[str(source_info.get("content_level") or "unknown")] += 1
            if source_info.get("independent_source"):
                independent_sources += 1
        if source_info.get("identity_match"):
            identity_matches += 1
        comp = result.get("comparison") or {}
        if not comp.get("fields"):
            continue
        extraction_scored += 1
        total_tp += int(comp.get("tp", 0)); total_fp += int(comp.get("fp", 0)); total_fn += int(comp.get("fn", 0))
        for gap in comp.get("vocabulary_gaps", []):
            vocabulary_gaps[(gap.get("field", ""), gap.get("term", ""))] += 1
        for match in comp.get("normalization_matches", []):
            normalization_matches[(match.get("field", ""), match.get("gold", ""), match.get("predicted", ""))] += 1
        for field, detail in comp["fields"].items():
            agg = field_totals[field]
            agg["tp"] += detail["tp"]; agg["fp"] += detail["fp"]; agg["fn"] += detail["fn"]
            agg["papers_scored"] += 1
            agg["papers_exact"] += 1 if detail["exact"] else 0
            for term in detail.get("missing", []):
                missing_terms[(detail["label"], term)] += 1
            for term in detail.get("extra", []):
                extra_terms[(detail["label"], term)] += 1

    field_metrics: List[Dict[str, Any]] = []
    for field, label in _PRIMARY_FIELDS:
        agg = field_totals[field]
        tp, fp, fn = agg["tp"], agg["fp"], agg["fn"]
        precision = tp / (tp + fp) if tp + fp else (1.0 if not fn else 0.0)
        recall = tp / (tp + fn) if tp + fn else (1.0 if not fp else 0.0)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        field_metrics.append({
            **agg,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "exact_agreement": agg["papers_exact"] / agg["papers_scored"] if agg["papers_scored"] else 0.0,
        })

    precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else (1.0 if not total_fn else 0.0)
    recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else (1.0 if not total_fp else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    tested = len(results)
    title_found = search_counts["title_found"]
    identifier_rescue = search_counts["identifier_rescue"]
    return {
        "benchmark_publications": benchmark_total,
        "selected_publications": selected_total,
        "completed_publications": tested,
        "search": {
            "title_found": title_found,
            "identifier_rescue": identifier_rescue,
            "not_found": search_counts["not_found"],
            "error": search_counts["error"],
            "title_search_recall": title_found / tested if tested else 0.0,
            "found_after_identifier_rescue": (title_found + identifier_rescue) / tested if tested else 0.0,
        },
        "source": {
            "available": source_available,
            "identity_matches": identity_matches,
            "independent_sources": independent_sources,
            "availability_rate": source_available / tested if tested else 0.0,
            "identity_match_rate": identity_matches / source_available if source_available else 0.0,
            "independent_source_rate": independent_sources / tested if tested else 0.0,
            "methods": [{"method": method, "count": count} for method, count in source_methods.most_common()],
            "content_levels": [{"level": level, "count": count} for level, count in source_content_levels.most_common()],
        },
        "structured_evidence": {
            "papers_scored": extraction_scored,
            "tp": total_tp,
            "fp": total_fp,
            "fn": total_fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
        "field_metrics": field_metrics,
        "vocabulary_gaps": [
            {"field": field, "term": term, "count": count}
            for (field, term), count in vocabulary_gaps.most_common(50)
        ],
        "normalization_matches": [
            {"field": field, "gold": gold, "predicted": pred, "count": count}
            for (field, gold, pred), count in normalization_matches.most_common(50)
        ],
        "top_missing_terms": [
            {"field": field, "term": term, "count": count}
            for (field, term), count in missing_terms.most_common(50)
        ],
        "top_extra_terms": [
            {"field": field, "term": term, "count": count}
            for (field, term), count in extra_terms.most_common(50)
        ],
    }


def _write_exports(run_dir: Path, results: Sequence[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    results_csv = run_dir / "validation_results.csv"
    with results_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        fields = [
            "reference_number", "pmid", "doi", "title", "search_status", "title_found", "identifier_rescue_found",
            "source_available", "identity_match", "source_method", "source_independent", "source_content_level",
            "precision", "recall", "f1", "exact_all_fields", "normalization_matches",
            "missing_labels", "extra_labels", "vocabulary_gaps", "search_attempt_count", "search_error", "source_error",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for r in results:
            comp = r.get("comparison") or {}
            missing = []
            extra = []
            for detail in (comp.get("fields") or {}).values():
                missing.extend(f"{detail['label']}: {v}" for v in detail.get("missing", []))
                extra.extend(f"{detail['label']}: {v}" for v in detail.get("extra", []))
            writer.writerow({
                "reference_number": r.get("reference_number", ""),
                "pmid": r.get("pmid", ""),
                "doi": r.get("doi", ""),
                "title": r.get("title", ""),
                "search_status": r.get("search", {}).get("status", ""),
                "title_found": r.get("search", {}).get("title_found", False),
                "identifier_rescue_found": r.get("search", {}).get("identifier_rescue_found", False),
                "source_available": r.get("source", {}).get("available", False),
                "identity_match": r.get("source", {}).get("identity_match", False),
                "source_method": r.get("source", {}).get("source_method", ""),
                "source_independent": r.get("source", {}).get("independent_source", False),
                "source_content_level": r.get("source", {}).get("content_level", ""),
                "precision": round(float(comp.get("precision", 0.0)), 6) if comp else "",
                "recall": round(float(comp.get("recall", 0.0)), 6) if comp else "",
                "f1": round(float(comp.get("f1", 0.0)), 6) if comp else "",
                "exact_all_fields": comp.get("exact_all_fields", "") if comp else "",
                "normalization_matches": " | ".join(f"{m['field']}: {m['gold']} = {m['predicted']}" for m in comp.get("normalization_matches", [])),
                "missing_labels": " | ".join(missing),
                "extra_labels": " | ".join(extra),
                "vocabulary_gaps": " | ".join(f"{g['field']}: {g['term']}" for g in comp.get("vocabulary_gaps", [])),
                "search_attempt_count": r.get("search", {}).get("attempt_count", 0),
                "search_error": r.get("search", {}).get("error", ""),
                "source_error": r.get("source", {}).get("error", ""),
            })

    metrics_csv = run_dir / "field_metrics.csv"
    with metrics_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["field", "label", "papers_scored", "tp", "fp", "fn", "precision", "recall", "f1", "exact_agreement"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in summary.get("field_metrics", []):
            writer.writerow({
                "field": row.get("field", ""),
                "label": row.get("label", ""),
                "papers_scored": row.get("papers_scored", 0),
                "tp": row.get("tp", 0),
                "fp": row.get("fp", 0),
                "fn": row.get("fn", 0),
                "precision": round(float(row.get("precision", 0.0)), 6),
                "recall": round(float(row.get("recall", 0.0)), 6),
                "f1": round(float(row.get("f1", 0.0)), 6),
                "exact_agreement": round(float(row.get("exact_agreement", 0.0)), 6),
            })


def _active_job_is_alive(validation_id: str) -> bool:
    """Return True only when this Python process still owns a live validation thread."""
    with _ACTIVE_LOCK:
        job = _ACTIVE_JOBS.get(validation_id)
        if not job:
            return False
        thread = job.get("thread")
        return bool(thread is not None and thread.is_alive())


def _job_stop_requested(validation_id: str) -> bool:
    with _ACTIVE_LOCK:
        return bool(_ACTIVE_JOBS.get(validation_id, {}).get("stop_requested"))


def _reconcile_interrupted_status(validation_id: str, status: Dict[str, Any]) -> Dict[str, Any]:
    """Turn stale queued/running states into resumable interrupted states.

    Validation workers live in this FastAPI process. If Uvicorn is restarted (or the
    computer/network interruption causes the process to end), the thread disappears
    while status.json may still say "running". Detect that condition when the run is
    opened so the page no longer auto-refreshes forever and can offer Resume.
    """
    if not status:
        return status
    if status.get("state") in {"queued", "running"} and not _active_job_is_alive(validation_id):
        status = dict(status)
        status["state"] = "interrupted"
        status["updated_at"] = _now_iso()
        status["message"] = (
            "Validation was interrupted or the app was restarted. Completed results were preserved; "
            "use Resume validation to continue the remaining publications."
        )
        _safe_write_json(_validation_dir(validation_id) / "status.json", status)
    return status


def request_stop(validation_id: str) -> bool:
    with _ACTIVE_LOCK:
        job = _ACTIVE_JOBS.get(validation_id)
        if job:
            job["stop_requested"] = True
            return True
    # If the process already lost the worker, make the stale state explicit rather
    # than leaving a run permanently marked as running.
    run_dir = _validation_dir(validation_id)
    status = _read_json(run_dir / "status.json", {}) or {}
    if status.get("state") in {"queued", "running"}:
        status.update({
            "state": "interrupted",
            "updated_at": _now_iso(),
            "message": "Validation worker is no longer active. Completed results were preserved and the run can be resumed.",
        })
        _safe_write_json(run_dir / "status.json", status)
    return False


def _resume_key(row: Dict[str, Any]) -> str:
    key = str(row.get("benchmark_key") or "").strip()
    if key:
        return "key:" + key
    doi = _normalize_label(row.get("doi", ""))
    if doi:
        return "doi:" + doi
    pmid = re.sub(r"\D", "", str(row.get("pmid") or ""))
    if pmid:
        return "pmid:" + pmid
    return "title:" + _normalize_label(row.get("title", ""))


def _retryable_existing_result(row: Dict[str, Any]) -> bool:
    """Retry transport failures on resume, but keep real not-found/no-source outcomes."""
    search = row.get("search") or {}
    if str(search.get("status") or "").lower() == "error":
        return True
    source = row.get("source") or {}
    if source.get("available"):
        return False
    error = str(source.get("error") or "").lower()
    transient_markers = (
        "timeout", "timed out", "connection", "temporar", "network", "name resolution",
        "remote disconnected", "ssl", "http 429", "http 500", "http 502", "http 503", "http 504",
    )
    return bool(error and any(marker in error for marker in transient_markers))


def _run_validation(
    validation_id: str,
    publications: List[Dict[str, Any]],
    benchmark_total: int,
    config: Dict[str, Any],
    existing_results: Optional[List[Dict[str, Any]]] = None,
    selected_total: Optional[int] = None,
    resumed: bool = False,
) -> None:
    run_dir = _validation_dir(validation_id)
    results: List[Dict[str, Any]] = list(existing_results or [])
    selected_total = int(selected_total if selected_total is not None else len(publications) + len(results))
    status = {
        "validation_id": validation_id,
        "state": "running",
        "created_at": config.get("created_at"),
        "updated_at": _now_iso(),
        "benchmark_publications": benchmark_total,
        "selected_publications": selected_total,
        "completed_publications": len(results),
        "remaining_publications": len(publications),
        "current_title": "",
        "message": "Automated validation resumed." if resumed else "Automated validation is running.",
        "config": config,
        "resume_count": int(config.get("resume_count", 0) or 0),
    }
    _safe_write_json(run_dir / "status.json", status)
    # Ensure the partial checkpoint exists even when resume starts with zero new rows.
    _safe_write_json(run_dir / "results.partial.json", results)
    max_workers = max(1, min(int(config.get("max_workers", 3)), 8))
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_validate_one, pub): pub for pub in publications}
            for future in as_completed(futures):
                pub = futures[future]
                if _job_stop_requested(validation_id):
                    for pending in futures:
                        pending.cancel()
                    status["state"] = "stopped"
                    status["message"] = "Validation stopped by user. Completed results were preserved and can be resumed."
                    break
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "benchmark_key": pub.get("benchmark_key"), "title": pub.get("title"), "doi": pub.get("doi"),
                        "pmid": pub.get("pmid"), "gold": pub.get("gold", {}),
                        "search": {"status": "error", "error": f"Worker failure: {type(exc).__name__}: {exc}"},
                        "source": {"available": False, "error": "Validation worker failed."}, "structured_evidence": {}, "comparison": {},
                    }
                # A publication can be retried on resume. Replace any older row for the
                # same benchmark key instead of creating duplicates.
                key = _resume_key(result)
                results = [old for old in results if _resume_key(old) != key]
                results.append(result)
                status.update({
                    "completed_publications": len(results),
                    "remaining_publications": max(0, selected_total - len(results)),
                    "current_title": pub.get("title", ""),
                    "updated_at": _now_iso(),
                })
                _safe_write_json(run_dir / "status.json", status)
                _safe_write_json(run_dir / "results.partial.json", results)
        summary = _aggregate(results, benchmark_total, selected_total)
        _safe_write_json(run_dir / "results.json", results)
        _safe_write_json(run_dir / "summary.json", summary)
        _write_exports(run_dir, results, summary)
        if status.get("state") != "stopped":
            status["state"] = "completed"
            status["message"] = "Automated validation completed."
        status["completed_publications"] = len(results)
        status["remaining_publications"] = max(0, selected_total - len(results))
        status["updated_at"] = _now_iso()
        _safe_write_json(run_dir / "status.json", status)
    except Exception as exc:
        status.update({
            "state": "failed",
            "updated_at": _now_iso(),
            "completed_publications": len(results),
            "remaining_publications": max(0, selected_total - len(results)),
            "message": f"{type(exc).__name__}: {exc}. Completed results were preserved and can be resumed.",
        })
        _safe_write_json(run_dir / "status.json", status)
        _safe_write_json(run_dir / "results.partial.json", results)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_JOBS.pop(validation_id, None)


def start_validation(content: bytes, filename: str, sample_size: str = "50", sampling_mode: str = "stratified", seed: int = 2026, max_workers: int = 3, exclude_development: bool = True) -> str:
    publications = parse_laserai_export(content)
    if not publications:
        raise ValueError("No publications were found in the LaserAI export.")
    eligible = publications
    excluded_development_count = 0
    if exclude_development:
        eligible = [p for p in publications if _normalize_label(p.get("title", "")) not in _DEVELOPMENT_TITLES]
        excluded_development_count = len(publications) - len(eligible)
    if not eligible:
        raise ValueError("No eligible publications remain after applying the development-set exclusion.")
    selected = select_validation_sample(eligible, sample_size, sampling_mode, seed)
    validation_id = "validation-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = _validation_dir(validation_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "benchmark.xlsx").write_bytes(content)
    _safe_write_json(run_dir / "benchmark_publications.json", publications)
    _safe_write_json(run_dir / "selected_publications.json", selected)
    config = {
        "filename": filename,
        "sample_size": sample_size,
        "sampling_mode": sampling_mode,
        "seed": seed,
        "max_workers": max_workers,
        "exclude_development": bool(exclude_development),
        "excluded_development_count": excluded_development_count,
        "eligible_publications": len(eligible),
        "created_at": _now_iso(),
        "resume_count": 0,
    }
    _safe_write_json(run_dir / "config.json", config)
    _safe_write_json(run_dir / "status.json", {
        "validation_id": validation_id, "state": "queued", "created_at": config["created_at"], "updated_at": config["created_at"],
        "benchmark_publications": len(publications), "selected_publications": len(selected), "completed_publications": 0,
        "remaining_publications": len(selected), "message": "Validation queued.", "config": config,
    })
    thread = threading.Thread(
        target=_run_validation,
        args=(validation_id, selected, len(publications), config),
        kwargs={"existing_results": [], "selected_total": len(selected), "resumed": False},
        daemon=True,
        name=validation_id,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_JOBS[validation_id] = {"thread": thread, "stop_requested": False}
    thread.start()
    return validation_id


def resume_validation(validation_id: str, max_workers: Optional[int] = None) -> str:
    """Resume an interrupted/failed/stopped validation from its partial checkpoint.

    Completed publications are skipped. Rows that ended in transient transport errors
    are deliberately retried now that connectivity may have recovered.
    """
    run_dir = _validation_dir(validation_id)
    if not run_dir.exists():
        raise ValueError("Validation run not found.")
    if _active_job_is_alive(validation_id):
        raise ValueError("This validation is already running.")

    selected = _read_json(run_dir / "selected_publications.json", []) or []
    benchmark_publications = _read_json(run_dir / "benchmark_publications.json", []) or []
    config = _read_json(run_dir / "config.json", {}) or {}
    status = _read_json(run_dir / "status.json", {}) or {}
    if not isinstance(selected, list) or not selected:
        raise ValueError("The saved selected-publication list is missing; this run cannot be resumed safely.")

    prior_results = validation_results(validation_id)
    kept_results: List[Dict[str, Any]] = []
    for row in prior_results:
        if not isinstance(row, dict) or _retryable_existing_result(row):
            continue
        kept_results.append(row)
    completed_keys = {_resume_key(row) for row in kept_results}
    remaining = [pub for pub in selected if _resume_key(pub) not in completed_keys]

    if not remaining:
        raise ValueError("All selected publications already have completed non-transient results; there is nothing to resume.")

    config = dict(config)
    if max_workers is not None:
        config["max_workers"] = max(1, min(int(max_workers), 8))
    else:
        config["max_workers"] = max(1, min(int(config.get("max_workers", 3)), 8))
    config["resume_count"] = int(config.get("resume_count", 0) or 0) + 1
    config["resumed_at"] = _now_iso()
    _safe_write_json(run_dir / "config.json", config)

    # Final files from a stopped/failed attempt must not shadow the live partial
    # checkpoint while the resumed run is in progress.
    for name in ("results.json", "summary.json", "validation_results.csv", "field_metrics.csv"):
        try:
            (run_dir / name).unlink(missing_ok=True)
        except Exception:
            pass
    _safe_write_json(run_dir / "results.partial.json", kept_results)
    queued = {
        **status,
        "validation_id": validation_id,
        "state": "queued",
        "updated_at": _now_iso(),
        "benchmark_publications": len(benchmark_publications) or int(status.get("benchmark_publications", 0) or 0),
        "selected_publications": len(selected),
        "completed_publications": len(kept_results),
        "remaining_publications": len(remaining),
        "current_title": "",
        "message": f"Resume queued: {len(kept_results)} preserved, {len(remaining)} publications remaining/retrying.",
        "config": config,
        "resume_count": config["resume_count"],
    }
    _safe_write_json(run_dir / "status.json", queued)

    benchmark_total = len(benchmark_publications) or int(status.get("benchmark_publications", 0) or 0)
    thread = threading.Thread(
        target=_run_validation,
        args=(validation_id, remaining, benchmark_total, config),
        kwargs={"existing_results": kept_results, "selected_total": len(selected), "resumed": True},
        daemon=True,
        name=validation_id + "-resume",
    )
    with _ACTIVE_LOCK:
        _ACTIVE_JOBS[validation_id] = {"thread": thread, "stop_requested": False}
    thread.start()
    return validation_id


def validation_status(validation_id: str) -> Dict[str, Any]:
    status = _read_json(_validation_dir(validation_id) / "status.json", {}) or {}
    return _reconcile_interrupted_status(validation_id, status)


def validation_summary(validation_id: str) -> Dict[str, Any]:
    return _read_json(_validation_dir(validation_id) / "summary.json", {}) or {}


def validation_results(validation_id: str) -> List[Dict[str, Any]]:
    run_dir = _validation_dir(validation_id)
    data = _read_json(run_dir / "results.json", None)
    if data is None:
        data = _read_json(run_dir / "results.partial.json", [])
    return data if isinstance(data, list) else []


def list_validations(limit: int = 25) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not VALIDATION_ROOT.exists():
        return rows
    for path in sorted((p for p in VALIDATION_ROOT.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        # Use validation_status() so stale "running" states left by a process restart
        # are automatically converted to resumable "interrupted" states.
        status = validation_status(path.name)
        config = _read_json(path / "config.json", {}) or {}
        if status:
            rows.append({**status, "filename": config.get("filename", "")})
        if len(rows) >= limit:
            break
    return rows

def validation_export_file(validation_id: str, kind: str) -> Optional[Path]:
    filenames = {
        "results": "validation_results.csv",
        "metrics": "field_metrics.csv",
        "json": "results.json",
        "summary": "summary.json",
    }
    name = filenames.get(kind)
    if not name:
        return None
    path = _validation_dir(validation_id) / name
    return path if path.exists() else None


def validation_view(validation_id: str, page: int = 1, per_page: int = 50) -> Dict[str, Any]:
    status = validation_status(validation_id)
    summary = validation_summary(validation_id)
    results = validation_results(validation_id)
    # Error-first ordering makes review more useful while preserving all rows in exports.
    def sort_key(r: Dict[str, Any]) -> Tuple[int, float, str]:
        comp = r.get("comparison") or {}
        search_bad = r.get("search", {}).get("status") not in {"title_found", "identifier_rescue"}
        exact = bool(comp.get("exact_all_fields"))
        f1 = float(comp.get("f1", -1.0)) if comp else -1.0
        return (0 if (search_bad or not exact) else 1, f1, str(r.get("title", "")))
    ordered = sorted(results, key=sort_key)
    per_page = max(10, min(int(per_page), 100))
    total_pages = max(1, (len(ordered) + per_page - 1) // per_page)
    page = max(1, min(int(page), total_pages))
    start = (page - 1) * per_page
    visible = ordered[start:start + per_page]
    for row in visible:
        comp = row.get("comparison") or {}
        missing = []
        extra = []
        for detail in (comp.get("fields") or {}).values():
            missing.extend(f"{detail.get('label')}: {term}" for term in detail.get("missing", []))
            extra.extend(f"{detail.get('label')}: {term}" for term in detail.get("extra", []))
        row["missing_labels_display"] = missing
        row["extra_labels_display"] = extra
    return {
        "status": status,
        "summary": summary,
        "results": visible,
        "result_count": len(ordered),
        "page": page,
        "total_pages": total_pages,
        "per_page": per_page,
    }
