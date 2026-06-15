"""Material *price* cache + on-demand cost research.

Sister module to `material_cache.py`. Same JSON-backed write-through pattern,
but for USD-per-kg pricing rather than mechanical properties.

Lookup waterfall:
    1. Local table (fast, hand-curated industry-typical numbers).
    2. JSON cache (any prior Gemini-derived lookup persists across sessions).
    3. Gemini 3 Pro reasoning lookup ("cost research agent") — pulls a
       canonical USD-per-kg estimate from its training-data knowledge of
       published market reports / commodity indices.
    4. Conservative default ($1.50/kg for unknown polymers).

Every entry is timestamped + carries a source + confidence tag. The cache
file is auditable plain JSON so an engineer can sanity-check / hand-edit a
number before it shows up in a customer-facing report.

NOTE: the platform doesn't crawl the open web directly — that requires
unrestricted egress + a contract. Gemini 3 Pro is the proxy; it has the
published commodity prices (PlasticsExchange, ICIS, Kompass-style aggregator
ranges) in its training corpus. Every web-derived price is labeled
`source="gemini-3-pro · web-derived"` and `confidence="estimated"` so the
guardrail and report layers downgrade verdicts that depend on it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import PROJECT_ROOT, settings
from ..llm.gemini_client import get_gemini


CACHE_PATH = PROJECT_ROOT / "data" / "price_cache.json"


# ── Local table — hand-curated, conservative, USD/kg ─────────────────────
LOCAL_PRICES_USD_PER_KG: dict[str, float] = {
    "PET":              1.50,
    "PETG":             2.20,
    "HDPE":             1.20,
    "LDPE":             1.10,
    "PP":               1.30,
    "PVC":              1.00,
    "PS":               1.40,
    "Glass":            0.45,
    "Aluminum":         2.80,
    "Steel":            1.10,
    "Tinplate":         1.40,
    "Corrugated B-flute": 0.80,
    "Kraft Paperboard": 0.95,
    "rPET":             1.65,
    "Bioplastic PLA":   3.20,
}

# Long-form name → short key. Mirrors PRICE_NAME_CANONICAL in optimization.py
# so callers using either form get the same answer.
NAME_ALIASES: dict[str, str] = {
    "polyethylene terephthalate":          "PET",
    "recycled pet":                        "rPET",
    "rpet":                                "rPET",
    "high-density polyethylene":           "HDPE",
    "high density polyethylene":           "HDPE",
    "low-density polyethylene":            "LDPE",
    "low density polyethylene":            "LDPE",
    "polypropylene":                       "PP",
    "polyvinyl chloride":                  "PVC",
    "polystyrene":                         "PS",
    "glass":                               "Glass",
    "soda-lime glass":                     "Glass",
    "aluminium":                           "Aluminum",
    "aluminum":                            "Aluminum",
    "tin":                                 "Tinplate",
    "tinplate":                            "Tinplate",
    "steel":                               "Steel",
    "corrugated":                          "Corrugated B-flute",
    "cardboard":                           "Corrugated B-flute",
    "kraft":                               "Kraft Paperboard",
    "petg":                                "PETG",
    "polylactic acid":                     "Bioplastic PLA",
    "pla":                                 "Bioplastic PLA",
}

DEFAULT_FALLBACK_USD_PER_KG = 1.50    # conservative for unknown polymers


# ── Helpers ──────────────────────────────────────────────────────────────

def _canonical(name: str) -> str:
    """Return the canonical key. Falls back to the original casing if no
    alias matches — Gemini lookups are keyed by the input, not the canonical
    form, so we don't lose specificity for genuinely-novel materials."""
    if not name:
        return ""
    if name in LOCAL_PRICES_USD_PER_KG:
        return name
    return NAME_ALIASES.get(name.strip().lower(), name.strip())


def _key(name: str) -> str:
    return (name or "").strip().lower()


def _load() -> dict[str, dict[str, Any]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(d: dict[str, dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
    tmp.replace(CACHE_PATH)


def _cache_get(name: str) -> Optional[dict[str, Any]]:
    return _load().get(_key(name))


def _cache_put(name: str, price: float, *, source: str,
               confidence: str = "estimated", notes: str = "") -> dict[str, Any]:
    entry = {
        "name": name,
        "price_usd_per_kg": round(float(price), 4),
        "source": source,
        "confidence": confidence,
        "notes": notes,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    d = _load()
    d[_key(name)] = entry
    _save(d)
    return entry


# ── Cost-research agent: Gemini-3-Pro web lookup ─────────────────────────

_PRICE_RESEARCH_PROMPT = """You are a packaging-cost research agent.

For the given material name, return a CURRENT industry-typical bulk pellet /
sheet / commodity price expressed in USD per kilogram for CPG packaging
purchasing volumes. Use what you know from published market reports
(PlasticsExchange, ICIS, USDA / Plastics News commodity tables, MatWeb pricing
indications). If grade matters and the user didn't specify a grade, return the
most common packaging grade.

Rules:
- The number must be a single best-estimate USD-per-kg figure, not a range.
- If the material is genuinely unknown / novel and you can't justify a number,
  return null for price_usd_per_kg.
- One short sentence in `notes` explaining what grade / market your number
  reflects ("PlasticsExchange Sept 2025 · bottle-grade pellet").
- Do NOT include any prose outside the JSON.

Return STRICTLY this schema:
{
  "name": "<canonical material name>",
  "price_usd_per_kg": <number or null>,
  "currency": "USD",
  "notes": "<one short sentence explaining grade + market reference>",
  "source_hint": "<short reference, e.g. 'PlasticsExchange · bottle-grade resin'>"
}
"""


def _fetch_via_gemini(name: str) -> Optional[dict[str, Any]]:
    """Call Gemini 3 Pro for a single USD/kg figure. Returns None on failure
    or when the model declined to commit to a number."""
    gemini = get_gemini()
    if not gemini.available:
        return None
    try:
        raw = gemini.reason_json(
            _PRICE_RESEARCH_PROMPT,
            f"Material name: {name}",
            temperature=0.05,
        )
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    price = raw.get("price_usd_per_kg")
    if not isinstance(price, (int, float)) or price <= 0 or price > 200:
        # >200 USD/kg is almost certainly hallucination for packaging materials.
        return None
    return {
        "name": str(raw.get("name") or name),
        "price_usd_per_kg": float(price),
        "currency": "USD",
        "notes": str(raw.get("notes") or "")[:240],
        "source_hint": str(raw.get("source_hint") or settings.GEMINI_REASONING_MODEL),
    }


# ── Public entry point used by every caller ──────────────────────────────

def lookup_price(name: str, *, allow_web: bool = True) -> dict[str, Any]:
    """Return a price dict for the given material — never raises, always
    returns SOMETHING the dashboard can render.

    Output shape:
        {
          "name": str,                           # canonical name we resolved to
          "price_usd_per_kg": float,             # always a positive number
          "source": "local" | "cache" | "web" | "fallback",
          "confidence": "verified" | "estimated" | "fallback",
          "notes": str,                          # short explanation
          "ts": iso timestamp,
        }
    """
    if not name:
        return {
            "name": "(unknown)",
            "price_usd_per_kg": DEFAULT_FALLBACK_USD_PER_KG,
            "source": "fallback",
            "confidence": "fallback",
            "notes": "No material specified — using conservative polymer default.",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    canon = _canonical(name)

    # 1 — local hand-curated table (fastest, highest confidence)
    if canon in LOCAL_PRICES_USD_PER_KG:
        return {
            "name": canon,
            "price_usd_per_kg": LOCAL_PRICES_USD_PER_KG[canon],
            "source": "local",
            "confidence": "verified",
            "notes": f"Curated industry-typical bulk price for {canon}.",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    # 2 — JSON cache from prior Gemini lookups
    cached = _cache_get(name) or _cache_get(canon)
    if cached and cached.get("price_usd_per_kg"):
        return {
            "name":             cached.get("name", canon),
            "price_usd_per_kg": float(cached["price_usd_per_kg"]),
            "source":           "cache",
            "confidence":       cached.get("confidence", "estimated"),
            "notes":            cached.get("notes", ""),
            "ts":               cached.get("ts"),
        }

    # 3 — cost-research agent via Gemini 3 Pro
    if allow_web:
        fetched = _fetch_via_gemini(name)
        if fetched and fetched.get("price_usd_per_kg"):
            entry = _cache_put(
                fetched["name"],
                fetched["price_usd_per_kg"],
                source=f"gemini-3-pro · web-derived ({fetched.get('source_hint','')})".rstrip(" ()"),
                confidence="estimated",
                notes=fetched.get("notes", ""),
            )
            return {
                "name":             entry["name"],
                "price_usd_per_kg": entry["price_usd_per_kg"],
                "source":           "web",
                "confidence":       entry["confidence"],
                "notes":            entry["notes"],
                "ts":               entry["ts"],
            }

    # 4 — conservative fallback so the dashboard NEVER blanks
    return {
        "name": canon or name,
        "price_usd_per_kg": DEFAULT_FALLBACK_USD_PER_KG,
        "source": "fallback",
        "confidence": "fallback",
        "notes": (
            f"Live-price lookup for '{name}' returned no answer; "
            f"using conservative polymer default of "
            f"${DEFAULT_FALLBACK_USD_PER_KG:.2f}/kg."
        ),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
