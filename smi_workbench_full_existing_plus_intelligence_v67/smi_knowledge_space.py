from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None

try:
    from sklearn.cluster import MiniBatchKMeans, KMeans  # type: ignore
    from sklearn.decomposition import PCA, TruncatedSVD  # type: ignore
except Exception:  # pragma: no cover
    MiniBatchKMeans = None
    KMeans = None
    PCA = None
    TruncatedSVD = None

try:
    import umap  # type: ignore
except Exception:  # pragma: no cover
    umap = None

try:
    from smi_intelligence import (
        COHORTS,
        DATASETS,
        EXPOSURES,
        GEOSPATIAL,
        OUTCOMES,
        MODULE_SPECS,
        extract_records_from_response,
        first_value,
        read_json,
        record_text,
        abstract_text,
        title_of,
        citation_of,
        extract_sample_size,
        extract_population_size,
        extract_statistical_methods,
        normalize_exposure_terms,
    )
except Exception:  # pragma: no cover
    COHORTS = {}
    DATASETS = {}
    EXPOSURES = {}
    GEOSPATIAL = {}
    OUTCOMES = []
    MODULE_SPECS = {}

    def read_json(path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def first_value(record: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
        for key in keys:
            value = record.get(key)
            if value not in (None, "", []):
                return str(value)
        return default

    def extract_records_from_response(response: Any) -> List[Dict[str, Any]]:
        if isinstance(response, list):
            return [x for x in response if isinstance(x, dict)]
        if isinstance(response, dict):
            for key in ["records", "results", "data", "items", "payload"]:
                val = response.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
                if isinstance(val, dict):
                    nested = extract_records_from_response(val)
                    if nested:
                        return nested
        return []

    def record_text(record: Dict[str, Any]) -> str:
        return " ".join(str(v) for v in record.values())

    def abstract_text(record: Dict[str, Any]) -> str:
        return first_value(record, ["abstract", "abstract_text", "abstractText", "summary", "snippet", "description"])

    def title_of(record: Dict[str, Any]) -> str:
        return first_value(record, ["title", "article_title", "name"], "Untitled record")

    def citation_of(record: Dict[str, Any]) -> str:
        pmid = first_value(record, ["pmid", "PMID"])
        doi = first_value(record, ["doi", "DOI"])
        return f"PMID: {pmid}" if pmid else (f"DOI: {doi}" if doi else title_of(record))

    def extract_sample_size(text: str) -> str:
        return "not extracted"

    def extract_population_size(text: str) -> str:
        return "not extracted"

    def extract_statistical_methods(text: str) -> str:
        return "not extracted"

    def normalize_exposure_terms(terms: Iterable[str]) -> List[str]:
        out: List[str] = []
        for term in terms:
            term = str(term or "").strip()
            if term and term not in out:
                out.append(term)
        return out


KNOWLEDGE_FILES = {
    "vectors_json": "13_knowledge_vectors.json",
    "vectors_csv": "13_knowledge_vectors.csv",
    "similarity_csv": "14_article_similarity.csv",
    "clusters_json": "15_knowledge_clusters.json",
    "clusters_csv": "15_knowledge_clusters.csv",
    "novelty_csv": "16_novelty_scores.csv",
    "support_dispute_csv": "17_support_dispute_scores.csv",
    "priority_csv": "18_priority_review_queue.csv",
    "summary_md": "19_knowledge_map_summary.md",
    "auto_variables_json": "20_auto_variable_suggestions.json",
    "auto_variables_csv": "20_auto_variable_suggestions.csv",
    "evidence_matrix_csv": "21_exposure_outcome_matrix.csv",
    "scatter_json": "22_knowledge_map_points.json",
    "umap_json": "24_umap_points.json",
    "umap_csv": "24_umap_points.csv",
}

CORE_VARIABLES = [
    "exposure", "exposure_type", "chemical_class", "health_outcome", "organ_system",
    "dataset", "cohort", "population", "life_stage", "study_design", "geography",
    "effect_direction", "effect_estimate", "risk_of_bias", "publication_year",
    "sample_size", "population_size", "statistical_methods",
]

STUDY_DESIGNS = [
    "cohort", "case-control", "cross-sectional", "randomized", "clinical trial",
    "ecological", "time-series", "case-crossover", "animal", "in vitro", "systematic review",
    "meta-analysis", "protocol", "narrative review", "prospective", "retrospective",
]
POPULATIONS = [
    "children", "infants", "pregnant", "pregnancy", "maternal", "workers", "older adults",
    "adolescents", "women", "men", "low-income", "urban", "rural", "general population",
    "birth cohort", "pregnant women", "preschool", "adults", "nurses",
]
DIRECTIONS = {
    "positive": ["positive association", "increased", "higher risk", "elevated", "associated with increased", "increase in", "risk of"],
    "negative": ["negative association", "lower", "decreased", "reduced", "inverse association", "associated with lower"],
    "null": ["no association", "not associated", "null", "no significant"],
    "mixed": ["mixed", "inconsistent", "heterogeneous", "opposing effects"],
}

# Expanded stopword list for auto variables. This intentionally includes
# transition words and manuscript boilerplate such as however/although/therefore.
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "of", "in", "on", "to", "for",
    "by", "at", "as", "into", "over", "under", "after", "before", "during", "through", "per", "via",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does",
    "did", "can", "could", "may", "might", "must", "should", "would", "will", "shall",
    "we", "our", "ours", "their", "this", "that", "these", "those", "it", "its", "they", "them", "there", "here",
    "i", "you", "he", "she", "him", "her", "his", "hers", "who", "whom", "whose", "which", "what", "where", "when", "why", "how",
    "however", "although", "though", "therefore", "thus", "hence", "whereas", "while", "moreover", "furthermore",
    "nevertheless", "nonetheless", "besides", "additionally", "consequently", "because", "since", "unless", "until",
    "including", "included", "includes", "respectively", "overall", "approximately", "about", "among", "between", "within", "from", "with", "without",
    "also", "all", "any", "both", "each", "either", "neither", "several", "many", "most", "some", "such", "same", "different",
    "abstract", "article", "paper", "manuscript", "study", "studies", "result", "results",
    "method", "methods", "background", "objective", "objectives", "aim", "aims", "purpose",
    "conclusion", "conclusions", "discussion", "introduction", "copyright", "author", "authors",
    "journal", "published", "available", "review", "narrative", "systematic", "meta", "analysis", "analyses",
    "associated", "association", "associations", "effect", "effects", "exposure", "exposures",
    "outcome", "outcomes", "health", "environmental", "using", "used", "use", "data", "database",
    "model", "models", "risk", "evidence", "research", "estimated", "estimate", "estimates",
    "examined", "assessed", "evaluated", "investigated", "significant", "significantly", "observed",
    "population", "sample", "samples", "cohort", "dataset", "datasets", "chemical", "chemicals", "system", "systems",
    "group", "groups", "level", "levels", "factor", "factors", "measure", "measures", "variable", "variables",
    "united", "states", "years", "year", "months", "month", "days", "day", "baseline", "follow", "up",
    "confidence", "interval", "odds", "ratio", "hazard", "relative", "regression", "adjusted", "crude",
    "higher", "lower", "increased", "decreased", "reduced", "elevated", "long", "short", "term", "long-term", "short-term",
}

GENERIC_PHRASES = {
    "public health", "human health", "environmental health", "risk factors", "health effects",
    "health outcomes", "environmental exposures", "statistical analysis", "sensitivity analysis",
}

KNOWLEDGE_TEXT_POLICY = "abstract_only"


def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: List[Dict[str, Any]], columns: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        keys: List[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        columns = keys or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def norm(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(norm(x) for x in value)
    if isinstance(value, dict):
        return " ".join(norm(x) for x in value.values())
    return re.sub(r"\s+", " ", str(value)).strip()


def knowledge_text(record: Dict[str, Any]) -> str:
    """Use abstracts only for Knowledge Space. Titles remain labels only."""
    text = ""
    try:
        text = abstract_text(record)
    except Exception:
        text = ""
    if text:
        return norm(text)
    fields = ["abstract", "abstract_text", "abstractText", "Abstract", "summary", "snippet", "description"]
    return "\n".join(norm(record.get(f, "")) for f in fields if norm(record.get(f, ""))).strip()


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "var"


def clean_auto_variable_phrase(phrase: str) -> str:
    phrase = re.sub(r"\s+", " ", str(phrase or "")).strip(" ,.;:()[]{}\"'")
    if not phrase:
        return ""
    phrase = re.sub(r"^(background|objective|objectives|methods?|results?|conclusions?|discussion|introduction)\s*:?\s*", "", phrase, flags=re.I)
    phrase = re.sub(r"\b(et\s+al|pmid|doi|copyright|license)\b", " ", phrase, flags=re.I)
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]*|\d+(?:\.\d+)?", phrase)
    cleaned: List[str] = []
    for w in words:
        wl = w.lower().strip("-")
        if wl in STOPWORDS:
            continue
        if len(wl) < 3 and not re.search(r"\d", wl):
            continue
        cleaned.append(w)
    while cleaned and cleaned[0].lower() in STOPWORDS:
        cleaned.pop(0)
    while cleaned and cleaned[-1].lower() in STOPWORDS:
        cleaned.pop()
    if not cleaned:
        return ""
    out = " ".join(cleaned)
    out_low = out.lower()
    if out_low in STOPWORDS or out_low in GENERIC_PHRASES or len(out) < 4:
        return ""
    # Reject phrase if it is only generic analysis/manuscript language after cleanup.
    content_words = [w.lower() for w in re.findall(r"[A-Za-z0-9\-]+", out) if w.lower() not in STOPWORDS]
    if not content_words or len(set(content_words)) == 1 and content_words[0] in STOPWORDS:
        return ""
    if len(content_words) > 6:
        return ""
    return out[:90]


def sample_size_bucket(value: str) -> str:
    text = str(value or "")
    m = re.search(r"\d+(?:,\d{3})*|\d+(?:\.\d+)?", text)
    if not m:
        return "unknown"
    num = float(m.group(0).replace(",", ""))
    if "million" in text.lower():
        num *= 1_000_000
    elif "thousand" in text.lower():
        num *= 1_000
    if num < 100:
        return "lt_100"
    if num < 1000:
        return "100_to_999"
    if num < 10000:
        return "1000_to_9999"
    if num < 100000:
        return "10000_to_99999"
    return "gte_100000"


def text_has(text: str, term: str) -> bool:
    if not term:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text.lower()) is not None


def detect_terms(text: str, dictionary: Iterable[str]) -> List[str]:
    return sorted({term for term in dictionary if text_has(text, str(term))}, key=lambda x: x.lower())


def record_year(record: Dict[str, Any]) -> int:
    for key in ["publication_date", "pub_date", "date", "year", "PublicationDate"]:
        value = first_value(record, [key])
        m = re.search(r"(19|20)\d{2}", str(value))
        if m:
            return int(m.group(0))
    text = record_text(record)
    m = re.search(r"(19|20)\d{2}", text)
    return int(m.group(0)) if m else 0


def detect_direction(text: str) -> str:
    low = text.lower()
    for label, phrases in DIRECTIONS.items():
        if any(p in low for p in phrases):
            return label
    return "unclear"


def hash_token_feature(token: str, buckets: int = 160) -> str:
    digest = hashlib.sha1(token.encode("utf-8", errors="ignore")).hexdigest()
    return f"txt_{int(digest[:8], 16) % buckets:03d}"


def token_features(text: str, buckets: int = 160) -> Counter:
    tokens = re.findall(r"[a-z][a-z0-9\-]{2,}", text.lower())
    counts: Counter = Counter()
    for token in tokens:
        if token in STOPWORDS or len(token) < 3:
            continue
        counts[hash_token_feature(token, buckets)] += 1
    total = sum(counts.values()) or 1
    for key in list(counts.keys()):
        counts[key] = counts[key] / total
    return counts


def extract_candidate_phrases(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Counter = Counter()
    examples: Dict[str, str] = {}
    types: Dict[str, str] = {}
    trusted_sources = [
        ("exposure", EXPOSURES.keys()),
        ("health_outcome", OUTCOMES),
        ("dataset", DATASETS.keys()),
        ("geospatial_dataset", GEOSPATIAL.keys()),
        ("cohort", COHORTS.keys()),
        ("study_design", STUDY_DESIGNS),
        ("population", POPULATIONS),
    ]
    for record in records:
        text = knowledge_text(record)
        if not text:
            continue
        title = title_of(record)
        low = text.lower()
        for vtype, terms in trusted_sources:
            for term in terms:
                if text_has(low, str(term)):
                    key = str(term).strip()
                    counts[key] += 1
                    examples.setdefault(key, title)
                    types.setdefault(key, vtype)

        raw_phrases: List[str] = []
        raw_phrases.extend(re.findall(r"\b[A-Z][A-Za-z0-9\-]*(?:\s+[A-Z][A-Za-z0-9\-]*){0,3}\b", text))
        raw_phrases.extend(re.findall(r"\b(?:satellite-derived|urinary|serum|blood|ambient|prenatal|postnatal|occupational|residential|neighborhood|maternal|childhood|outdoor|indoor|wildfire|extreme|long-term|short-term)\s+[a-z0-9\-]+(?:\s+[a-z0-9\-]+){0,2}\b", low))
        raw_phrases.extend(re.findall(r"\b[a-z0-9\-]+\s+(?:exposure|pollution|smoke|heat|temperature|asthma|mortality|hypertension|birth|cohort|biomarker|metabolite|dataset|model)\b", low))
        for phrase in raw_phrases:
            clean = clean_auto_variable_phrase(phrase)
            if len(clean) < 4 or len(clean) > 90:
                continue
            words = [w.lower() for w in re.findall(r"[A-Za-z0-9\-]+", clean)]
            if not words or all(w in STOPWORDS for w in words):
                continue
            if clean.lower() in GENERIC_PHRASES:
                continue
            counts[clean] += 1
            examples.setdefault(clean, title)
            types.setdefault(clean, "auto_discovered")

    rows: List[Dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for term, count in counts.most_common(env_int("SMI_AUTO_VARIABLE_MAX", 500) * 2):
        term_slug = slug(term)
        if term_slug in seen_slugs:
            continue
        seen_slugs.add(term_slug)
        vtype = types.get(term, "auto_discovered")
        ontology = vtype != "auto_discovered"
        auto_added = bool(ontology or count >= env_int("SMI_AUTO_VARIABLE_MIN_COUNT", 3))
        rows.append({
            "variable": term,
            "variable_slug": term_slug,
            "variable_type": vtype,
            "count": count,
            "ontology_match": "yes" if ontology else "no",
            "auto_added_to_vector": "yes" if auto_added else "no",
            "review_status": "auto_added" if auto_added else "suggested",
            "example_title": examples.get(term, ""),
            "rationale": "Trusted dictionary/ontology match" if ontology else ("Appears in at least 3 abstracts" if count >= 3 else "Candidate abstract phrase; reviewer should approve before use"),
            "text_source": KNOWLEDGE_TEXT_POLICY,
        })
        if len(rows) >= env_int("SMI_AUTO_VARIABLE_MAX", 500):
            break
    return rows


def load_approved_variable_slugs(run_dir: Path) -> set[str]:
    path = run_dir / "outputs" / KNOWLEDGE_FILES["auto_variables_json"]
    data = read_json(path, default=[])
    slugs = set()
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            status = str(row.get("review_status", "")).lower()
            auto_added = str(row.get("auto_added_to_vector", "")).lower() == "yes"
            if status in {"accepted", "auto_added"} or auto_added:
                slugs.add(str(row.get("variable_slug") or slug(str(row.get("variable", "")))))
    return slugs


def build_article_features(record: Dict[str, Any], auto_vars: List[Dict[str, Any]], approved_slugs: set[str]) -> Tuple[Dict[str, float], Dict[str, Any]]:
    text = knowledge_text(record)
    title = title_of(record)
    citation = citation_of(record)
    low = text.lower()
    exposures = normalize_exposure_terms(detect_terms(low, EXPOSURES.keys()))
    outcomes = detect_terms(low, OUTCOMES)
    datasets = detect_terms(low, DATASETS.keys())
    geos = detect_terms(low, GEOSPATIAL.keys())
    cohorts = detect_terms(low, COHORTS.keys())
    designs = detect_terms(low, STUDY_DESIGNS)
    pops = detect_terms(low, POPULATIONS)
    direction = detect_direction(low)
    year = record_year(record)
    sample_size = extract_sample_size(text)
    population_size = extract_population_size(text)
    statistical_methods = extract_statistical_methods(text)

    features: Dict[str, float] = {}

    def add(prefix: str, terms: Iterable[str], weight: float = 1.0) -> None:
        for term in terms:
            features[f"{prefix}:{slug(str(term))}"] = weight

    add("exposure", exposures, 1.5)
    add("outcome", outcomes, 1.5)
    add("dataset", datasets, 0.9)
    add("geospatial", geos, 0.8)
    add("cohort", cohorts, 0.9)
    add("study_design", designs, 0.7)
    add("population", pops, 0.7)
    if direction != "unclear":
        features[f"direction:{direction}"] = 0.6
    if year:
        features["year_normalized"] = max(0.0, min(1.0, (year - 1990) / 40.0)) * 0.35
    if sample_size != "not extracted":
        features[f"sample_size:{sample_size_bucket(sample_size)}"] = 0.45
    if population_size != "not extracted":
        features[f"population_size:{sample_size_bucket(population_size)}"] = 0.35
    if statistical_methods != "not extracted":
        for method in statistical_methods.split("; ")[:5]:
            features[f"stat_method:{slug(method)}"] = 0.6
    for row in auto_vars:
        term = str(row.get("variable", ""))
        s = str(row.get("variable_slug") or slug(term))
        if s in approved_slugs and text_has(low, term):
            features[f"auto:{s}"] = 0.75
    features.update(token_features(low))
    meta = {
        "article_id": citation,
        "title": title,
        "citation": citation,
        "publication_year": year,
        "exposures": "; ".join(exposures),
        "outcomes": "; ".join(outcomes),
        "datasets": "; ".join(datasets),
        "geospatial_datasets": "; ".join(geos),
        "cohorts": "; ".join(cohorts),
        "study_designs": "; ".join(designs),
        "populations": "; ".join(pops),
        "effect_direction": direction,
        "sample_size": sample_size,
        "population_size": population_size,
        "statistical_methods": statistical_methods,
        "knowledge_text_source": KNOWLEDGE_TEXT_POLICY,
        "abstract_available": "yes" if bool(text.strip()) else "no",
        "filter_text": " ".join([title, citation, text, "; ".join(exposures), "; ".join(outcomes), "; ".join(datasets), "; ".join(cohorts)]).lower(),
        "feature_count": len(features),
    }
    return features, meta


def cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(float(a[k]) * float(b[k]) for k in common)
    na = math.sqrt(sum(float(v) ** 2 for v in a.values()))
    nb = math.sqrt(sum(float(v) ** 2 for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def dense_matrix(vectors: List[Dict[str, float]]) -> Tuple[List[str], List[List[float]]]:
    keys = sorted({k for v in vectors for k in v})
    rows = [[float(v.get(k, 0.0)) for k in keys] for v in vectors]
    return keys, rows


def normalize_rows(rows: List[List[float]]) -> List[List[float]]:
    out: List[List[float]] = []
    for row in rows:
        n = math.sqrt(sum(x * x for x in row)) or 1.0
        out.append([x / n for x in row])
    return out


def scale_pairs(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not points:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    def scale(vals: List[float]) -> List[float]:
        lo, hi = min(vals), max(vals)
        if abs(hi - lo) < 1e-12:
            return [0.0 for _ in vals]
        return [((v - lo) / (hi - lo)) * 2 - 1 for v in vals]
    sx, sy = scale(xs), scale(ys)
    return list(zip(sx, sy))


def pca_2d(rows: List[List[float]]) -> List[Tuple[float, float]]:
    n = len(rows)
    if n == 0:
        return []
    if n == 1:
        return [(0.0, 0.0)]
    d = len(rows[0]) if rows else 0
    if d == 0:
        return [(0.0, 0.0) for _ in rows]
    if np is not None and PCA is not None:
        try:
            arr = np.asarray(rows, dtype=float)
            reducer = PCA(n_components=2, random_state=42)
            coords = reducer.fit_transform(arr)
            return scale_pairs([(float(x), float(y)) for x, y in coords])
        except Exception:
            pass
    # Pure-Python fallback for small/medium runs.
    means = [sum(row[j] for row in rows) / n for j in range(d)]
    centered = [[row[j] - means[j] for j in range(d)] for row in rows]
    def matvec(vec: List[float], data: List[List[float]]) -> List[float]:
        tmp = [sum(row[j] * vec[j] for j in range(d)) for row in data]
        return [sum(data[i][j] * tmp[i] for i in range(n)) / max(1, n - 1) for j in range(d)]
    def power_component(data: List[List[float]], seed: int) -> List[float]:
        vec = [0.0] * d
        vec[seed % d] = 1.0
        for _ in range(45):
            new = matvec(vec, data)
            length = math.sqrt(sum(x * x for x in new)) or 1.0
            vec = [x / length for x in new]
        return vec
    pc1 = power_component(centered, 0)
    scores1 = [sum(row[j] * pc1[j] for j in range(d)) for row in centered]
    deflated = [[centered[i][j] - scores1[i] * pc1[j] for j in range(d)] for i in range(n)]
    pc2 = power_component(deflated, 1)
    scores2 = [sum(row[j] * pc2[j] for j in range(d)) for row in centered]
    return scale_pairs(list(zip(scores1, scores2)))


def umap_2d(rows: List[List[float]]) -> Tuple[List[Tuple[Optional[float], Optional[float]]], str]:
    n = len(rows)
    if n == 0:
        return [], "no_records"
    if n == 1:
        return [(0.0, 0.0)], "single_record"
    if np is None or umap is None:
        return [(None, None) for _ in rows], "umap_not_installed"
    max_n = env_int("SMI_UMAP_MAX_RECORDS", 100000)
    if n > max_n:
        return [(None, None) for _ in rows], f"skipped_over_{max_n}_records"
    try:
        arr = np.asarray(rows, dtype=float)
        reducer = umap.UMAP(
            n_components=2,
            n_neighbors=min(30, max(2, n - 1)),
            min_dist=0.08,
            metric="cosine",
            random_state=42,
            low_memory=True,
        )
        coords = reducer.fit_transform(arr)
        scaled = scale_pairs([(float(x), float(y)) for x, y in coords])
        return scaled, "computed"
    except Exception as exc:
        return [(None, None) for _ in rows], f"failed_{type(exc).__name__}"



def agglomerative_clusters(sim: List[List[float]], threshold: float = 0.52) -> List[int]:
    n = len(sim)
    clusters: List[List[int]] = [[i] for i in range(n)]
    while True:
        best_pair = None
        best_score = -1.0
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                vals = [sim[a][b] for a in clusters[i] for b in clusters[j]]
                avg = sum(vals) / len(vals) if vals else 0.0
                if avg > best_score:
                    best_score = avg
                    best_pair = (i, j)
        if best_pair is None or best_score < threshold:
            break
        i, j = best_pair
        clusters[i] = clusters[i] + clusters[j]
        del clusters[j]
    labels = [0] * n
    ordered = sorted(clusters, key=lambda c: (-len(c), min(c)))
    for cluster_id, members in enumerate(ordered, start=1):
        for idx in members:
            labels[idx] = cluster_id
    return labels


def kmeans_clusters(rows: List[List[float]], n_clusters: Optional[int] = None) -> Tuple[List[int], str]:
    n = len(rows)
    if n == 0:
        return [], "no_records"
    if n == 1:
        return [1], "single_record"
    if np is None or MiniBatchKMeans is None:
        # Fallback: cluster by strongest structured feature hash.
        return hash_clusters_from_rows(rows), "hash_fallback"
    k_max = env_int("SMI_KNOWLEDGE_MAX_CLUSTERS", 120)
    if n_clusters is None:
        n_clusters = max(2, min(k_max, int(math.sqrt(max(2, n) / 2)) + 2))
    n_clusters = max(2, min(n_clusters, n, k_max))
    try:
        arr = np.asarray(rows, dtype=float)
        if n > 2000:
            model = MiniBatchKMeans(n_clusters=n_clusters, random_state=42, batch_size=min(4096, max(256, n_clusters * 20)), n_init="auto")
        else:
            model = KMeans(n_clusters=n_clusters, random_state=42, n_init="auto") if KMeans is not None else MiniBatchKMeans(n_clusters=n_clusters, random_state=42)
        labels0 = model.fit_predict(arr)
        counts = Counter(int(x) for x in labels0)
        ordered = {old: i + 1 for i, (old, _) in enumerate(counts.most_common())}
        return [ordered[int(x)] for x in labels0], f"kmeans_{n_clusters}_clusters"
    except Exception:
        return hash_clusters_from_rows(rows), "hash_fallback_after_kmeans_error"


def hash_clusters_from_rows(rows: List[List[float]]) -> List[int]:
    labels = []
    for row in rows:
        if not row:
            labels.append(1)
        else:
            best = max(range(len(row)), key=lambda i: abs(row[i]))
            labels.append((best % 24) + 1)
    # Re-number by size.
    counts = Counter(labels)
    mapping = {old: i + 1 for i, (old, _) in enumerate(counts.most_common())}
    return [mapping[x] for x in labels]


def cluster_theme(members: List[Dict[str, Any]]) -> str:
    exp = Counter()
    out = Counter()
    data = Counter()
    for row in members:
        for term in str(row.get("exposures", "")).split("; "):
            if term:
                exp[term] += 1
        for term in str(row.get("outcomes", "")).split("; "):
            if term:
                out[term] += 1
        for field in ["datasets", "cohorts", "geospatial_datasets"]:
            for term in str(row.get(field, "")).split("; "):
                if term:
                    data[term] += 1
    parts: List[str] = []
    if exp:
        parts.append(exp.most_common(1)[0][0])
    if out:
        parts.append(out.most_common(1)[0][0])
    if data and len(parts) < 2:
        parts.append(data.most_common(1)[0][0])
    return " + ".join(parts) if parts else "Mixed environmental health topic"


def compute_pairwise_similarity(vectors: List[Dict[str, float]], metas: List[Dict[str, Any]], max_pairwise: int) -> Tuple[List[List[float]], List[Dict[str, Any]], str]:
    n = len(vectors)
    if n <= max_pairwise:
        sim = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        rows: List[Dict[str, Any]] = []
        for i in range(n):
            for j in range(i + 1, n):
                value = round(cosine(vectors[i], vectors[j]), 4)
                sim[i][j] = sim[j][i] = value
                rows.append({
                    "article_i": metas[i]["article_id"],
                    "title_i": metas[i]["title"],
                    "article_j": metas[j]["article_id"],
                    "title_j": metas[j]["title"],
                    "cosine_similarity": value,
                    "cosine_distance": round(1.0 - value, 4),
                })
        return sim, rows, "full_pairwise"
    # Large-scale mode: do not materialize an n x n matrix. Sample neighbor pairs only.
    sample_n = min(max_pairwise, n)
    rng = random.Random(42)
    sample = sorted(rng.sample(range(n), sample_n))
    rows = []
    sim = [[1.0 if i == j else 0.0 for j in range(min(n, 1))] for i in range(min(n, 1))]
    for offset, i in enumerate(sample):
        scored: List[Tuple[float, int]] = []
        for j in sample:
            if i == j:
                continue
            scored.append((cosine(vectors[i], vectors[j]), j))
        for value, j in sorted(scored, reverse=True)[:5]:
            rows.append({
                "article_i": metas[i]["article_id"],
                "title_i": metas[i]["title"],
                "article_j": metas[j]["article_id"],
                "title_j": metas[j]["title"],
                "cosine_similarity": round(value, 4),
                "cosine_distance": round(1.0 - value, 4),
                "note": f"sampled similarity only; full pairwise skipped for {n} records",
            })
    return sim, rows, f"sampled_{sample_n}_records_full_pairwise_skipped"


def approximate_novelty_scores(metas: List[Dict[str, Any]], labels: List[int], vectors: List[Dict[str, float]], full_sim: Optional[List[List[float]]] = None) -> List[Dict[str, Any]]:
    rows = []
    n = len(metas)
    for i, meta in enumerate(metas):
        if full_sim and len(full_sim) == n:
            year = int(meta.get("publication_year") or 0)
            prior = []
            for j, other in enumerate(metas):
                if i == j:
                    continue
                oy = int(other.get("publication_year") or 0)
                if year and oy and oy <= year:
                    prior.append(full_sim[i][j])
            max_prior = max(prior) if prior else 0.0
        else:
            # Approximation: most items in large clusters are less novel; singleton/small clusters are more novel.
            cluster_size = sum(1 for x in labels if x == labels[i])
            max_prior = min(0.95, math.log(max(cluster_size, 1), 10) / 3.0)
        novelty = round(1.0 - max_prior, 4)
        label = "potentially emerging" if novelty >= 0.65 else ("somewhat novel" if novelty >= 0.40 else "established/near existing cluster")
        rows.append({
            "article_id": meta["article_id"],
            "title": meta["title"],
            "publication_year": int(meta.get("publication_year") or 0),
            "max_similarity_to_prior_articles": round(max_prior, 4),
            "novelty_score": novelty,
            "novelty_label": label,
        })
    return rows


def support_dispute_rows(metas: List[Dict[str, Any]], cluster_labels: List[int]) -> List[Dict[str, Any]]:
    by_cluster: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for meta, cid in zip(metas, cluster_labels):
        by_cluster[cid].append(meta)
    majority: Dict[int, str] = {}
    for cid, members in by_cluster.items():
        dirs = Counter(m.get("effect_direction") for m in members if m.get("effect_direction") not in {"", "unclear", None})
        majority[cid] = dirs.most_common(1)[0][0] if dirs else "unclear"
    rows = []
    for meta, cid in zip(metas, cluster_labels):
        direction = meta.get("effect_direction") or "unclear"
        maj = majority.get(cid, "unclear")
        if direction == "unclear" or maj == "unclear":
            label = "unclear"
            conflict_score = 0.0
        elif direction == maj:
            label = "supports cluster majority"
            conflict_score = 0.0
        elif direction == "mixed":
            label = "extends/mixed evidence"
            conflict_score = 0.4
        else:
            label = "potentially disputes cluster majority"
            conflict_score = 1.0
        rows.append({
            "article_id": meta["article_id"],
            "title": meta["title"],
            "cluster_id": cid,
            "article_direction": direction,
            "cluster_majority_direction": maj,
            "support_dispute_label": label,
            "conflict_score": conflict_score,
        })
    return rows


def priority_rows(metas: List[Dict[str, Any]], novelty: List[Dict[str, Any]], dispute: List[Dict[str, Any]], cluster_labels: List[int]) -> List[Dict[str, Any]]:
    nov = {row["article_id"]: float(row["novelty_score"]) for row in novelty}
    con = {row["article_id"]: float(row["conflict_score"]) for row in dispute}
    cluster_counts = Counter(cluster_labels)
    rows = []
    for i, meta in enumerate(metas):
        cluster_size = cluster_counts[cluster_labels[i]]
        centrality_penalty = min(1.0, math.log(max(cluster_size, 1), 10) / 3.0)
        recent = 0.0
        year = int(meta.get("publication_year") or 0)
        if year:
            recent = max(0.0, min(1.0, (year - 2010) / 20.0))
        chemical_flag = 1.0 if any(x in str(meta.get("exposures", "")).lower() for x in ["pfas", "pfoa", "pfos", "new chemical", "glyphosate", "phthalate", "bpa"]) else 0.0
        dataset_flag = 1.0 if meta.get("datasets") or meta.get("cohorts") or meta.get("geospatial_datasets") else 0.0
        score = (0.35 * nov.get(meta["article_id"], 0.0) + 0.25 * con.get(meta["article_id"], 0.0) + 0.15 * recent + 0.10 * chemical_flag + 0.10 * dataset_flag + 0.05 * (1.0 - centrality_penalty))
        reason_bits = []
        if nov.get(meta["article_id"], 0.0) >= 0.4:
            reason_bits.append("novel compared with prior/cluster records")
        if con.get(meta["article_id"], 0.0) > 0:
            reason_bits.append("may conflict with cluster direction")
        if dataset_flag:
            reason_bits.append("contains dataset/cohort/geospatial signal")
        if chemical_flag:
            reason_bits.append("contains chemical or chemical-class signal")
        if not reason_bits:
            reason_bits.append("representative article in its cluster")
        rows.append({
            "rank": 0,
            "article_id": meta["article_id"],
            "title": meta["title"],
            "cluster_id": cluster_labels[i],
            "priority_score": round(score, 4),
            "priority_level": "high" if score >= 0.55 else ("medium" if score >= 0.30 else "low"),
            "review_reason": "; ".join(reason_bits),
            "exposures": meta.get("exposures", ""),
            "outcomes": meta.get("outcomes", ""),
            "sample_size": meta.get("sample_size", ""),
            "population_size": meta.get("population_size", ""),
            "statistical_methods": meta.get("statistical_methods", ""),
        })
    rows.sort(key=lambda r: (-float(r["priority_score"]), str(r["title"])))
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


def exposure_outcome_matrix(run_dir: Path, metas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    assoc_path = run_dir / "outputs" / "07_exposure_health_associations.json"
    assoc = read_json(assoc_path, default=[])
    pairs: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    if isinstance(assoc, list) and assoc:
        for row in assoc:
            exp = str(row.get("exposure_name") or "not extracted")
            out = str(row.get("health_outcome") or "not extracted")
            direction = str(row.get("effect_direction") or "unclear").lower()
            pairs[(exp, out)]["count"] += 1
            pairs[(exp, out)][direction] += 1
    else:
        for meta in metas:
            exps = [x for x in str(meta.get("exposures", "")).split("; ") if x] or ["not extracted"]
            outs = [x for x in str(meta.get("outcomes", "")).split("; ") if x] or ["not extracted"]
            for exp in exps:
                for out in outs:
                    pairs[(exp, out)]["count"] += 1
                    pairs[(exp, out)][str(meta.get("effect_direction") or "unclear")] += 1
    rows = []
    for (exp, out), counts in sorted(pairs.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        rows.append({
            "exposure": exp,
            "health_outcome": out,
            "article_count": counts["count"],
            "positive": counts["positive"],
            "negative": counts["negative"],
            "null": counts["null"],
            "mixed": counts["mixed"],
            "unclear": counts["unclear"],
        })
    return rows


def projection_rows(vector_rows: List[Dict[str, Any]], method: str, sampled_only: bool = False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    x_key = f"{method}_x"
    y_key = f"{method}_y"
    for row in vector_rows:
        x = row.get(x_key)
        y = row.get(y_key)
        if sampled_only and (x in (None, "") or y in (None, "")):
            continue
        rows.append({
            "article_id": row.get("article_id"),
            "title": row.get("title"),
            "cluster_id": row.get("cluster_id"),
            "x": x,
            "y": y,
            "priority_score": row.get("priority_score", 0),
            "exposures": row.get("exposures", ""),
            "outcomes": row.get("outcomes", ""),
        })
    return rows


def generate_knowledge_space_outputs(run_dir: Path, cluster_threshold: float = 0.52) -> Dict[str, Any]:
    outputs = run_dir / "outputs"
    retrieved = read_json(outputs / "04_retrieved_records.json", default={})
    records = extract_records_from_response(retrieved)
    if not records:
        records = extract_records_from_response(read_json(outputs / "01_literature_search.json", default={}))
    if not records:
        for key in ["vectors_json", "clusters_json", "scatter_json", "umap_json"]:
            write_json(outputs / KNOWLEDGE_FILES[key], [])
        for key in ["vectors_csv", "similarity_csv", "clusters_csv", "novelty_csv", "support_dispute_csv", "priority_csv", "evidence_matrix_csv", "umap_csv"]:
            write_csv(outputs / KNOWLEDGE_FILES[key], [])
        for key in ["auto_variables_json", "auto_variables_csv"]:
            try:
                (outputs / KNOWLEDGE_FILES[key]).unlink()
            except FileNotFoundError:
                pass
        (outputs / KNOWLEDGE_FILES["summary_md"]).write_text("# Knowledge Space Summary\n\nNo records were available.\n", encoding="utf-8")
        return {"records": 0, "clusters": 0, "auto_variables": 0}

    # Auto-variable suggestions are intentionally disabled in this build.
    # Knowledge Space now uses core extracted fields and hashed abstract-text features only.
    auto_vars: List[Dict[str, Any]] = []
    approved_slugs: set[str] = set()
    for key in ["auto_variables_json", "auto_variables_csv"]:
        try:
            (outputs / KNOWLEDGE_FILES[key]).unlink()
        except FileNotFoundError:
            pass

    sparse_vectors: List[Dict[str, float]] = []
    metas: List[Dict[str, Any]] = []
    for record in records:
        features, meta = build_article_features(record, auto_vars, approved_slugs)
        sparse_vectors.append(features)
        metas.append(meta)

    keys, matrix = dense_matrix(sparse_vectors)
    norm_matrix = normalize_rows(matrix)
    n = len(records)
    large_mode = n > env_int("SMI_KNOWLEDGE_FULL_PAIRWISE_MAX", 3000)

    coords_pca = pca_2d(norm_matrix)
    coords_umap, umap_status = umap_2d(norm_matrix)

    if large_mode:
        labels, cluster_method = kmeans_clusters(norm_matrix)
        sim_matrix: Optional[List[List[float]]] = None
    else:
        sim_base = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                value = round(cosine(sparse_vectors[i], sparse_vectors[j]), 4)
                sim_base[i][j] = sim_base[j][i] = value
        labels = agglomerative_clusters(sim_base, cluster_threshold)
        cluster_method = "agglomerative_cosine"
        sim_matrix = sim_base

    max_pairwise = env_int("SMI_KNOWLEDGE_FULL_PAIRWISE_MAX", 3000)
    sim_for_file, sim_rows, sim_status = compute_pairwise_similarity(sparse_vectors, metas, max_pairwise)
    write_csv(outputs / KNOWLEDGE_FILES["similarity_csv"], sim_rows)

    novelty = approximate_novelty_scores(metas, labels, sparse_vectors, sim_matrix)
    dispute = support_dispute_rows(metas, labels)
    priority = priority_rows(metas, novelty, dispute, labels)
    priority_by_id = {p["article_id"]: p for p in priority}

    vector_rows: List[Dict[str, Any]] = []
    for idx, (meta, features, pca_coord, umap_coord, cid) in enumerate(zip(metas, sparse_vectors, coords_pca, coords_umap, labels), start=1):
        pri = priority_by_id.get(meta["article_id"], {})
        vector_rows.append({
            **{k: v for k, v in meta.items() if k != "filter_text"},
            "record_index": idx,
            "cluster_id": cid,
            "priority_score": pri.get("priority_score", 0),
            "pca_x": round(float(pca_coord[0]), 4) if pca_coord[0] is not None else "",
            "pca_y": round(float(pca_coord[1]), 4) if pca_coord[1] is not None else "",
            "umap_x": round(float(umap_coord[0]), 4) if umap_coord[0] is not None else "",
            "umap_y": round(float(umap_coord[1]), 4) if umap_coord[1] is not None else "",
            "top_features": "; ".join(sorted([k for k, v in features.items() if v >= 0.7])[:50]),
            "knowledge_vector": features,
        })
    write_json(outputs / KNOWLEDGE_FILES["vectors_json"], vector_rows)
    write_csv(outputs / KNOWLEDGE_FILES["vectors_csv"], [{k: v for k, v in row.items() if k != "knowledge_vector"} for row in vector_rows])

    cluster_rows: List[Dict[str, Any]] = []
    clusters_json: List[Dict[str, Any]] = []
    for cid in sorted(set(labels)):
        member_indices = [i for i, label in enumerate(labels) if label == cid]
        members = [vector_rows[i] for i in member_indices]
        theme = cluster_theme(members)
        cluster_rows.append({
            "cluster_id": cid,
            "theme": theme,
            "article_count": len(members),
            "main_titles": " | ".join(m["title"] for m in members[:5]),
            "cluster_method": cluster_method,
        })
        clusters_json.append({
            "cluster_id": cid,
            "theme": theme,
            "article_count": len(members),
            "cluster_method": cluster_method,
            "articles": [{"article_id": m["article_id"], "title": m["title"], "pca_x": m["pca_x"], "pca_y": m["pca_y"], "umap_x": m["umap_x"], "umap_y": m["umap_y"]} for m in members[:1000]],
        })
    write_json(outputs / KNOWLEDGE_FILES["clusters_json"], clusters_json)
    write_csv(outputs / KNOWLEDGE_FILES["clusters_csv"], cluster_rows)

    matrix_rows = exposure_outcome_matrix(run_dir, metas)
    scatter = []
    for row in vector_rows:
        filter_text = next((m.get("filter_text", "") for m in metas if m["article_id"] == row["article_id"]), "")
        scatter.append({
            "article_id": row["article_id"],
            "title": row["title"],
            "cluster_id": row["cluster_id"],
            "pca_x": row.get("pca_x", ""),
            "pca_y": row.get("pca_y", ""),
            "umap_x": row.get("umap_x", ""),
            "umap_y": row.get("umap_y", ""),
            "x": row.get("pca_x", ""),
            "y": row.get("pca_y", ""),
            "priority_score": row.get("priority_score", 0),
            "effect_direction": row.get("effect_direction", "unclear"),
            "exposures": row.get("exposures", ""),
            "outcomes": row.get("outcomes", ""),
            "sample_size": row.get("sample_size", ""),
            "population_size": row.get("population_size", ""),
            "statistical_methods": row.get("statistical_methods", ""),
            "filter_text": filter_text[:4000],
        })
    write_csv(outputs / KNOWLEDGE_FILES["novelty_csv"], novelty)
    write_csv(outputs / KNOWLEDGE_FILES["support_dispute_csv"], dispute)
    write_csv(outputs / KNOWLEDGE_FILES["priority_csv"], priority)
    write_csv(outputs / KNOWLEDGE_FILES["evidence_matrix_csv"], matrix_rows)
    write_json(outputs / KNOWLEDGE_FILES["scatter_json"], scatter)
    umap_rows = projection_rows(vector_rows, "umap", sampled_only=False)
    write_json(outputs / KNOWLEDGE_FILES["umap_json"], umap_rows)
    write_csv(outputs / KNOWLEDGE_FILES["umap_csv"], umap_rows)

    lines = [
        "# Knowledge Space Summary",
        "",
        f"Generated: {utc_now()}",
        "",
        "## What this layer does",
        "",
        "This layer converts each article into an abstract-only knowledge vector using extracted exposures, outcomes, datasets, cohorts, populations, study designs, sample size, population size, statistical methods, effect direction, publication year, and hashed abstract-text features.",
        "The map is useful because reviewers can see thematic neighborhoods, filter to a topic such as heat, find related papers quickly, detect emerging or isolated topics, and prioritize clusters that contain policy-relevant or conflicting evidence.",
        "",
        "## Large-scale behavior",
        "",
        f"- Articles vectorized: {n}",
        f"- Large-scale mode: {'yes' if large_mode else 'no'}",
        f"- Clustering method: {cluster_method}",
        f"- Similarity output mode: {sim_status}",
        f"- PCA projection: computed",
        f"- UMAP projection: {umap_status}",
        "- Knowledge text source: abstracts only",
        "",
        "## Counts",
        "",
        f"- Clusters detected: {len(cluster_rows)}",
        f"- Priority queue items: {len(priority)}",
        "",
        "## How people benefit",
        "",
        "- Search 20k-100k records without reading them linearly.",
        "- Filter to a concept such as heat, PFAS, PM2.5, asthma, pregnancy, or cardiovascular outcomes and view only that subspace.",
        "- See which papers are central, which are outliers, and which clusters represent emerging evidence.",
        "- Locate datasets, cohorts, exposures, outcomes, and statistical methods that recur across a large literature.",
        "- Build a faster systematic-review triage queue instead of manually sorting thousands of citations.",
        "",
        "## Clusters",
        "",
    ]
    for c in cluster_rows[:20]:
        lines.append(f"- Cluster {c['cluster_id']}: {c['theme']} ({c['article_count']} article(s))")
    lines.extend(["", "## Highest priority items", ""])
    for row in priority[:15]:
        lines.append(f"- Rank {row['rank']}: {row['title']} — score {row['priority_score']} ({row['review_reason']})")
    (outputs / KNOWLEDGE_FILES["summary_md"]).write_text("\n".join(lines), encoding="utf-8")

    return {"records": n, "clusters": len(cluster_rows), "priority_items": len(priority), "large_scale_mode": large_mode, "umap": umap_status}


def update_variable_decision(run_dir: Path, variable_slug: str, status: str, notes: str = "") -> bool:
    path = run_dir / "outputs" / KNOWLEDGE_FILES["auto_variables_json"]
    rows = read_json(path, default=[])
    if not isinstance(rows, list):
        return False
    changed = False
    for row in rows:
        if str(row.get("variable_slug")) == str(variable_slug):
            row["review_status"] = status
            row["reviewer_notes"] = notes
            row["reviewed_at"] = utc_now()
            row["auto_added_to_vector"] = "yes" if status in {"accepted", "auto_added"} else "no"
            changed = True
            break
    if changed:
        write_json(path, rows)
        write_csv(run_dir / "outputs" / KNOWLEDGE_FILES["auto_variables_csv"], rows)
    return changed
