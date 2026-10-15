from __future__ import annotations

import difflib
import re
from statistics import mean

from app.odoo.client import OdooAPI

_NOISE = {"llc", "ltd", "co", "company", "inc", "the", "and"}
FUZZY_THRESHOLD = 0.62
SIMILAR_PRODUCT_THRESHOLD = 0.85


def name_domain(field: str, q: str) -> list:
    """AND of per-word ilike matches; trailing 's' is dropped so 'workstations' finds 'Workstation'."""
    words = [w for w in re.split(r"\W+", q.lower()) if w]
    out = []
    for w in words:
        stem = w[:-1] if len(w) > 3 and w.endswith("s") else w
        out.append([field, "ilike", stem])
    return out or [[field, "ilike", q]]


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


def fuzzy_find(odoo: OdooAPI, model: str, field: str, q: str, fields: list[str], base_domain: list | None = None,
               limit: int = 3, pool: int = 500, threshold: float = FUZZY_THRESHOLD) -> list[dict]:
    """Closest records by name, flagged match='fuzzy'. Only used when an exact search found nothing."""
    rows = odoo.search_read(model, base_domain or [], list({"id", field, *fields}), limit=pool)
    scored = sorted(((similarity(q, r[field] or ""), r) for r in rows), key=lambda t: -t[0])
    return [{**r, "match": "fuzzy", "score": s} for s, r in scored[:limit] if s >= threshold]
