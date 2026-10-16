from __future__ import annotations

import difflib
import re
from statistics import mean

from app.erp.client import ERPAPI

_NOISE = {"llc", "ltd", "co", "company", "inc", "the", "and", "trading", "contracting", "importing"}
FUZZY_THRESHOLD = 0.62


def word_stems(q: str) -> list[str]:
    """Search words; a trailing 's' is dropped so 'workstations' finds 'Workstation'."""
    words = [w for w in re.split(r"\W+", q.lower()) if w]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words] or [q.lower()]


def name_filters(field: str, q: str) -> list:
    """AND of per-word LIKE matches on one field."""
    return [[field, "like", f"%{w}%"] for w in word_stems(q)]


def _tokens(s: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", s.lower())
    return [w for w in words if w not in _NOISE] or words


def similarity(query: str, name: str) -> float:
    """0..1. Tolerates speech-to-text slips ('nazwa' ~ 'nizwa'), missing/extra letters and word order."""
    qt, nt = _tokens(query), _tokens(name)
    if not qt or not nt:
        return 0.0
    ratio = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
    whole = ratio(" ".join(qt), " ".join(nt))
    per_word = mean(max(ratio(a, b) for b in nt) for a in qt)
    return round(max(whole, per_word * 0.95), 3)


def fuzzy_find(erp: ERPAPI, doctype: str, field: str, q: str, fields: list[str], filters: list | None = None,
               limit: int = 3, pool: int = 500, threshold: float = FUZZY_THRESHOLD) -> list[dict]:
    """Closest records by name, flagged match='fuzzy'. Only used when an exact search found nothing."""
    rows = erp.get_list(doctype, filters or [], list({"name", field, *fields}), limit=pool)
    scored = sorted(((similarity(q, r.get(field) or ""), r) for r in rows), key=lambda t: -t[0])
    return [{**r, "match": "fuzzy", "score": s} for s, r in scored[:limit] if s >= threshold]
