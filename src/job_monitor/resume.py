from __future__ import annotations

import re
from pathlib import Path

from .models import ResumeProfile

SKILL_ALIASES = {
    "sql": ["sql", "postgresql", "oracle", "bigquery"],
    "python": ["python"],
    "tableau": ["tableau"],
    "power bi": ["power bi", "powerbi"],
    "dbt": ["dbt"],
    "gcp": ["gcp", "google cloud", "bigquery"],
    "aws": ["aws", "amazon web services"],
    "r": [" r ", "r programming"],
    "snowflake": ["snowflake"],
    "looker": ["looker", "lookml"],
    "excel": ["excel"],
    "spark": ["spark", "pyspark"],
    "airflow": ["airflow"],
    "statistics": ["statistics", "statistical"],
    "machine learning": ["machine learning", "ml"],
}

_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#.-]*")
_WORD_BOUNDARY_TEMPLATE = r"(?<![a-z0-9]){term}(?![a-z0-9])"
_STOPWORDS = {
    "about",
    "across",
    "also",
    "and",
    "are",
    "based",
    "business",
    "can",
    "data",
    "for",
    "from",
    "has",
    "have",
    "into",
    "job",
    "led",
    "more",
    "of",
    "on",
    "or",
    "our",
    "resume",
    "role",
    "team",
    "teams",
    "that",
    "the",
    "this",
    "through",
    "to",
    "using",
    "with",
    "work",
    "worked",
    "years",
}


def _contains_term(text: str, term: str) -> bool:
    normalized = f" {text.lower()} "
    if len(term) <= 2:
        return f" {term} " in normalized
    pattern = _WORD_BOUNDARY_TEMPLATE.format(term=re.escape(term.lower()))
    return re.search(pattern, normalized) is not None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = item.strip(" .,-:/;()[]{}").lower()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _resume_tokens(text: str) -> list[str]:
    tokens = []
    for token in _TOKEN_RE.findall(text.lower()):
        value = token.strip(".-")
        if value in _STOPWORDS:
            continue
        if len(value) < 3 and value not in {"ai", "bi", "ml", "r"}:
            continue
        if value.isdigit():
            continue
        tokens.append(value)
    return tokens


def _resume_phrases(tokens: list[str]) -> list[str]:
    phrases: list[str] = []
    for size in (3, 2):
        for index in range(max(0, len(tokens) - size + 1)):
            phrase_tokens = tokens[index : index + size]
            phrase = " ".join(phrase_tokens)
            if 6 <= len(phrase) <= 48:
                phrases.append(phrase)
    return phrases


def build_resume_profile(text: str, source_path: str | None = None) -> ResumeProfile:
    skills = [
        canonical
        for canonical, aliases in SKILL_ALIASES.items()
        if any(_contains_term(text, alias) for alias in aliases)
    ]
    tokens = _resume_tokens(text)
    keywords = _unique(skills + _resume_phrases(tokens) + tokens)
    return ResumeProfile(source_path=source_path, keywords=keywords[:120], skills=_unique(skills))


def load_resume(path: Path | None, text: str | None = None) -> ResumeProfile | None:
    if text:
        return build_resume_profile(text, source_path="RESUME_TEXT")
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"resume file not found: {path}")
    return build_resume_profile(path.read_text(encoding="utf-8"), source_path=str(path))
