"""
price_extractor.py — Local, pluggable price extractor (Oceania tracker)
=======================================================================

Ported from the Node `sealed-price-tracker` project's provider model into a
single, dependency-light Python module that the Streamlit app imports *and*
that can be run standalone from cron for a true weekly background refresh:

    python price_extractor.py            # one-shot refresh of every product

Design goals (exactly what was asked for):
  * **Pluggable providers** — each provider declares whether it is *usable*
    (eBay only becomes usable once API keys are present) and how to fetch a
    price. Adding a source = adding one function + a registry entry.
  * **Works with no credentials** — the `mock` provider is always on, so the
    app is fully functional before you ever add eBay keys.
  * **Works even if a live API is down or broken** — every provider call is
    wrapped; one failing/closed source never sinks the run, it just falls
    through to the next provider (mock is the guaranteed last resort). This
    mirrors the Node app's `Promise.allSettled` resilience.
  * **Normalises everything to USD** — the Streamlit dashboard's model is
    `usd_market_price × live_fx`, so each quote (which may be in AUD, USD, …)
    is converted to USD via Frankfurter before being stored.

eBay keys: drop them into a local `.env` file (see `.env.example`) — the eBay
provider auto-activates on next run/restart. Until then everything uses mock.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import math
import os
import sqlite3
import statistics
import time
from contextlib import closing

import requests

# Optional: load a local .env so you can paste eBay keys without exporting them.
try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 - dotenv is optional
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = "pokemon_oceania_tracker.db"
FRANKFURTER_URL = "https://api.frankfurter.app/latest"
POKEMONTCG_CARD_URL = "https://api.pokemontcg.io/v2/cards/{id}"
HTTP_TIMEOUT = 8  # seconds
REFRESH_INTERVAL_HOURS = 24 * 7

# Currencies we may need to convert *from* when normalising a quote to USD.
FX_SYMBOLS = ["AUD", "NZD", "GBP", "EUR", "CAD", "JPY"]

# Offline fallback rates (1 USD -> X). Used only if Frankfurter is unreachable.
FALLBACK_FX = {
    "USD": 1.0,
    "AUD": 1.52,
    "NZD": 1.66,
    "GBP": 0.79,
    "EUR": 0.92,
    "CAD": 1.37,
    "JPY": 157.0,
}


def ebay_config() -> dict:
    """Read eBay settings live from the environment (so adding keys + restart
    is enough to activate the provider).

    ``EBAY_ENV`` selects the host: ``sandbox`` uses api.sandbox.ebay.com (keys
    whose App ID contains ``-SBX-``), anything else uses production. Sandbox keys
    authenticate but return almost no real listings — production keys are needed
    for real lowest-listing prices.
    """
    env = os.environ.get("EBAY_ENV", "production").strip().lower()
    host = "https://api.sandbox.ebay.com" if env == "sandbox" else "https://api.ebay.com"
    return {
        "client_id": os.environ.get("EBAY_CLIENT_ID", ""),
        "client_secret": os.environ.get("EBAY_CLIENT_SECRET", ""),
        # NB: eBay has no dedicated NZ marketplace; EBAY_AU is the Oceania default.
        "marketplace_id": os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_AU"),
        "env": env,
        "oauth_url": f"{host}/identity/v1/oauth2/token",
        "browse_url": f"{host}/buy/browse/v1/item_summary/search",
    }


# ---------------------------------------------------------------------------
# FX helpers (normalise any quote currency to USD)
# ---------------------------------------------------------------------------

def fetch_usd_rates(symbols: list[str] | None = None) -> tuple[dict[str, float], bool]:
    """Fetch live USD -> {symbols} rates. Returns ``(rates, is_live)``.

    Falls back to approximate hard-coded rates if Frankfurter is unreachable so
    normalisation always works offline.
    """
    symbols = symbols or FX_SYMBOLS
    try:
        resp = requests.get(
            FRANKFURTER_URL,
            params={"from": "USD", "to": ",".join(symbols)},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        rates = {k: float(v) for k, v in resp.json()["rates"].items()}
        rates["USD"] = 1.0
        return rates, True
    except Exception:  # noqa: BLE001 - any failure -> fallback
        return dict(FALLBACK_FX), False


def to_usd(amount: float, currency: str, rates: dict[str, float]) -> float | None:
    """Convert ``amount`` in ``currency`` to USD. Returns ``None`` if unknown."""
    rate = rates.get((currency or "USD").upper()) or FALLBACK_FX.get(
        (currency or "USD").upper()
    )
    if not rate:
        return None
    return amount / rate


# ===========================================================================
# Providers
# ===========================================================================
# Each provider is a dict: {name, is_enabled() -> bool, fetch(product, rates)}.
# `fetch` returns a normalised quote dict (with `usd_median`) or None.
# `product` is a plain dict with keys: name, set_name, tcg_or_collectr_id,
# search_query, usd_market_price.


# --- mock: always on, no credentials --------------------------------------

def _hash_int(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def mock_fetch(product: dict, rates: dict[str, float]) -> dict | None:
    """Deterministic, plausible sealed-product price (USD). Stable within a day,
    drifts slowly across days — ported from the Node `mock` provider."""
    name = product.get("name") or product.get("tcg_or_collectr_id") or "item"
    base = 40 + (_hash_int(name) % 360)  # baseline $40–$399
    day_bucket = int(time.time() // 86400)
    noise = abs(math.sin(_hash_int(f"{name}:{day_bucket}")))  # 0..1
    drift = (noise - 0.5) * 0.25  # ±12.5%
    median = round(base * (1 + drift), 2)
    spread = round(median * 0.18, 2)
    return {
        "source": "mock",
        "currency": "USD",
        "price_low": round(median - spread, 2),
        "price_median": median,
        "price_high": round(median + spread, 2),
        "sample_size": 12 + (_hash_int(name) % 30),
        "usd_median": median,
        "usd_price": median,
        "raw": {"note": "simulated price — no live data"},
    }


# --- eBay Browse API: active asking prices (needs keys) --------------------

_ebay_token = {"value": None, "expires_at": 0.0}


def ebay_enabled() -> bool:
    c = ebay_config()
    return bool(c["client_id"] and c["client_secret"])


def _ebay_access_token() -> str:
    """OAuth2 client-credentials grant with a cached token (ported faithfully
    from the Node implementation)."""
    c = ebay_config()
    now = time.time()
    if _ebay_token["value"] and now < _ebay_token["expires_at"] - 60:
        return _ebay_token["value"]

    basic = base64.b64encode(
        f"{c['client_id']}:{c['client_secret']}".encode("utf-8")
    ).decode("ascii")
    resp = requests.post(
        c["oauth_url"],
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    _ebay_token["value"] = data["access_token"]
    _ebay_token["expires_at"] = now + float(data.get("expires_in", 7200))
    return _ebay_token["value"]


def ebay_fetch(product: dict, rates: dict[str, float]) -> dict | None:
    """eBay Browse API → the LOWEST current asking price from active listings.

    The requirement is to track the cheapest available listing, so each listing
    is normalised to USD first and the *minimum* is what we store (converting
    before taking the min keeps it correct even if results mix currencies).

    (Sold/completed comps need the partner-gated Marketplace Insights API; the
    free Browse API exposes active-listing asking prices — treat as a trend
    signal, and note it excludes shipping.) Ported from the Node `ebay` provider.
    """
    c = ebay_config()
    query = product.get("search_query") or product.get("name")
    if not query:
        return None

    token = _ebay_access_token()
    resp = requests.get(
        c["browse_url"],
        params={
            "q": query,
            "limit": "50",
            "filter": "buyingOptions:{FIXED_PRICE}",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": c["marketplace_id"],
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    items = data.get("itemSummaries") or []
    usd_prices: list[float] = []  # every listing, normalised to USD
    local_prices: list[float] = []  # raw values, for display in listing currency
    for it in items:
        # Fixed-price ("Buy It Now") listings only — never auctions. The request
        # filter already asks for FIXED_PRICE, but an item can carry several
        # buying options, so we also skip anything offering AUCTION here. That
        # keeps live/incomplete bid amounts out of the lowest-price calculation.
        options = it.get("buyingOptions") or []
        if "FIXED_PRICE" not in options or "AUCTION" in options:
            continue
        price = it.get("price") or {}
        try:
            value = float(price.get("value"))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        usd = to_usd(value, price.get("currency") or c["marketplace_id"], rates)
        if usd is None or usd <= 0:
            continue
        usd_prices.append(usd)
        local_prices.append(value)
    if not usd_prices:
        return None

    usd_low = min(usd_prices)
    currency = (items[0].get("price") or {}).get("currency") or "AUD"
    return {
        "source": "ebay",
        "currency": currency,
        "price_low": round(min(local_prices), 2),
        "price_median": round(statistics.median(local_prices), 2),
        "price_high": round(max(local_prices), 2),
        "sample_size": len(usd_prices),
        "usd_low": round(usd_low, 2),
        "usd_median": round(statistics.median(usd_prices), 2),
        # The stored market price = the LOWEST listing (in USD).
        "usd_price": round(usd_low, 2),
        "raw": {"total": data.get("total"), "sampled": len(usd_prices)},
    }


# --- eBay listing browse: discover sealed products for a set ---------------

# Keywords that mark a listing as a *sealed product* (not a single card / lot of
# commons). Kept deliberately broad so we surface "all potential sealed products".
_SEALED_KEYWORDS = (
    "sealed", "booster box", "booster bundle", "elite trainer", "etb",
    "build & battle", "build and battle", "booster pack", "blister",
    "collection", "premium collection", "tin", "case", "bundle", "display",
    "poster collection", "binder", "surprise box",
)


def _sealed_rank(title_lower: str) -> int:
    """Rank a listing by how likely it is a *primary* sealed product, so booster
    boxes/cases/ETBs surface above single packs & blisters (which are cheapest and
    would otherwise dominate a cheapest-first list)."""
    if "case" in title_lower:
        return 5
    if "booster box" in title_lower:
        return 4
    if "elite trainer" in title_lower or "etb" in title_lower:
        return 3
    if any(k in title_lower for k in (
        "booster bundle", "build & battle", "build and battle",
        "ultra premium", "premium collection", "super premium", "display",
    )):
        return 2
    if any(k in title_lower for k in ("collection", "tin", "bundle", "binder")):
        return 1
    return 0  # single packs / blisters / loose


def search_sealed_products(query: str, limit: int = 50, rates: dict[str, float] | None = None) -> list[dict]:
    """Browse eBay for active FIXED_PRICE **sealed** listings matching ``query``.

    Returns a list of listing dicts the user can look through, each shaped so it
    slots straight into the logger's product picker::

        {id, name, search_query, usd_price, price_local, currency, url, condition}

    Auctions are excluded (fixed-price only). Results are de-duplicated by title
    (keeping the cheapest) and sorted cheapest-first. Returns ``[]`` if eBay is
    disabled, the call fails, or nothing matches — callers fall back to the
    curated catalog. NB: with no/invalid keys this is always empty.
    """
    if not ebay_enabled() or not query:
        return []
    if rates is None:
        rates, _ = fetch_usd_rates()
    c = ebay_config()
    try:
        token = _ebay_access_token()
        resp = requests.get(
            c["browse_url"],
            params={
                "q": query,
                "limit": str(limit),
                "filter": "buyingOptions:{FIXED_PRICE}",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": c["marketplace_id"],
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - resilience: fall back to catalog
        print(f"[price_extractor] search_sealed_products failed for {query!r}: {exc}")
        return []

    best: dict[str, dict] = {}  # title(lower) -> cheapest listing record
    for it in data.get("itemSummaries") or []:
        options = it.get("buyingOptions") or []
        if "FIXED_PRICE" not in options or "AUCTION" in options:
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        low = title.lower()
        if not any(kw in low for kw in _SEALED_KEYWORDS):
            continue
        price = it.get("price") or {}
        try:
            value = float(price.get("value"))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        currency = price.get("currency") or c["marketplace_id"]
        usd = to_usd(value, currency, rates)
        if usd is None or usd <= 0:
            continue
        # Stable id from the title so picking the "same" product across separate
        # searches maps to the same product row (weaker than catalog slugs since
        # live titles vary — a known limitation of live-sourced products).
        rec = {
            "id": f"ebay-{_hash_int(low):x}",
            "name": title,
            "search_query": title,
            "usd_price": round(usd, 2),
            "price_local": round(value, 2),
            "currency": currency,
            "url": it.get("itemWebUrl"),
            "condition": it.get("condition"),
        }
        if low not in best or usd < best[low]["usd_price"]:
            best[low] = rec
    # Primary sealed products first (boxes/cases/ETBs), cheapest within each tier.
    return sorted(
        best.values(),
        key=lambda r: (-_sealed_rank(r["name"].lower()), r["usd_price"]),
    )


# --- pokemontcg.io: USD prices for single cards (no key) -------------------

def pokemontcg_fetch(product: dict, rates: dict[str, float]) -> dict | None:
    """USD market price for a single card, if `tcg_or_collectr_id` is a real
    card id. Returns None for sealed product (not in the card API)."""
    tcg_id = product.get("tcg_or_collectr_id")
    if not tcg_id:
        return None
    resp = requests.get(POKEMONTCG_CARD_URL.format(id=tcg_id), timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        return None
    data = resp.json().get("data", {})

    market = None
    for variant in (data.get("tcgplayer") or {}).get("prices", {}).values():
        if variant.get("market"):
            market = float(variant["market"])
            break
    if market is None:
        cm = (data.get("cardmarket") or {}).get("prices", {})
        if cm.get("trendPrice"):
            market = float(cm["trendPrice"])
    if market is None:
        return None
    return {
        "source": "pokemontcg",
        "currency": "USD",
        "price_low": market,
        "price_median": market,
        "price_high": market,
        "sample_size": 1,
        "usd_median": market,
        "usd_price": market,
        "raw": {"id": tcg_id},
    }


# --- registry --------------------------------------------------------------
# Order = priority. eBay (best for sealed) first, then pokemontcg (cards),
# then mock as the guaranteed fallback.
PROVIDERS = [
    {"name": "ebay", "is_enabled": ebay_enabled, "fetch": ebay_fetch},
    {"name": "pokemontcg", "is_enabled": lambda: True, "fetch": pokemontcg_fetch},
    {"name": "mock", "is_enabled": lambda: True, "fetch": mock_fetch},
]


def active_providers() -> list[dict]:
    return [p for p in PROVIDERS if p["is_enabled"]()]


def provider_status() -> list[dict]:
    """Which providers are usable right now (for the UI status panel)."""
    out = []
    for p in PROVIDERS:
        name = p["name"]
        if name == "ebay" and p["is_enabled"]() and ebay_config()["env"] == "sandbox":
            # Be honest: sandbox authenticates but has almost no real listings.
            name = "ebay (sandbox — no live data)"
        out.append({"name": name, "usable": bool(p["is_enabled"]())})
    return out


def quote_price(quote: dict | None) -> float | None:
    """The canonical USD value to store from a quote: ``usd_price`` (the lowest
    listing for eBay) falling back to ``usd_median`` for older-style quotes."""
    if not quote:
        return None
    value = quote.get("usd_price")
    if value is None:
        value = quote.get("usd_median")
    return None if value is None else float(value)


def get_quote(product: dict, rates: dict[str, float]) -> dict | None:
    """Try active providers in priority order; first non-empty quote wins.

    Each provider call is isolated: a broken/closed/rate-limited API logs a
    warning and falls through to the next provider. `mock` always succeeds, so
    this effectively never returns None — the app keeps working no matter what.
    """
    for provider in active_providers():
        try:
            quote = provider["fetch"](product, rates)
            if quote:
                return quote
        except Exception as exc:  # noqa: BLE001 - resilience is the whole point
            print(
                f"[price_extractor] provider '{provider['name']}' failed for "
                f"{product.get('name')!r}: {exc}"
            )
            continue
    return None


# ===========================================================================
# Database access (standalone — does not import the Streamlit app)
# ===========================================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_schema() -> None:
    """Make sure the tables this module touches exist.

    Creates `products`/`purchase_lots` (so a cron-only run works on a fresh DB),
    adds the `search_query` column used by eBay, a `tracked_products` cache list,
    and an `app_meta` key/value table holding the `last_price_refresh` timestamp.
    """
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tcg_or_collectr_id TEXT UNIQUE,
                name              TEXT NOT NULL,
                set_name          TEXT,
                usd_market_price  REAL NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS purchase_lots (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id              INTEGER NOT NULL,
                purchase_price_local    REAL NOT NULL,
                initial_quantity        INTEGER NOT NULL,
                current_sealed_quantity INTEGER NOT NULL,
                ripped_quantity         INTEGER NOT NULL DEFAULT 0,
                purchase_date           TEXT NOT NULL,
                local_currency          TEXT NOT NULL CHECK (local_currency IN ('AUD','NZD')),
                FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_products (
                product_id      INTEGER PRIMARY KEY,
                tracked_at      TEXT NOT NULL,
                last_scanned_at TEXT,
                FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE CASCADE
            );
            """
        )
        # Migration: add search_query to products if it isn't there yet.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(products);")]
        if "search_query" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN search_query TEXT;")
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO tracked_products (product_id, tracked_at)
            SELECT DISTINCT product_id, ?
            FROM purchase_lots
            """,
            (now,),
        )


def get_meta(key: str) -> str | None:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """INSERT INTO app_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )


def track_product(product_id: int) -> None:
    """Add a product to the tracked cache list if it is not already present."""
    ensure_schema()
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tracked_products (product_id, tracked_at)
            VALUES (?, ?)
            """,
            (product_id, _dt.datetime.now(_dt.timezone.utc).isoformat()),
        )


def untrack_product_if_unused(product_id: int) -> None:
    """Remove a product from the tracked cache once no lots remain."""
    ensure_schema()
    with closing(get_connection()) as conn, conn:
        row = conn.execute(
            "SELECT 1 FROM purchase_lots WHERE product_id = ? LIMIT 1",
            (product_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "DELETE FROM tracked_products WHERE product_id = ?",
                (product_id,),
            )


def tracked_products() -> list[dict]:
    """Return the products that should be refreshed."""
    ensure_schema()
    with closing(get_connection()) as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT p.*, t.tracked_at, t.last_scanned_at
                FROM tracked_products t
                JOIN products p ON p.id = t.product_id
                ORDER BY p.name
                """
            )
        ]


# ===========================================================================
# Refresh orchestration
# ===========================================================================

def get_usd_price(product: dict, rates: dict[str, float] | None = None) -> tuple[float, str]:
    """Fetch a single USD price for the add-product flow. Always returns a
    usable number (mock fallback), plus the source name."""
    ensure_schema()
    if rates is None:
        rates, _ = fetch_usd_rates()
    quote = get_quote(product, rates)
    price = quote_price(quote)
    if price is not None:
        return price, quote["source"]
    return 0.0, "none"


def refresh_all_prices(verbose: bool = True) -> list[dict]:
    """Refresh `usd_market_price` for the tracked cache list, then stamp
    `last_price_refresh`. Returns a per-product summary."""
    ensure_schema()
    rates, fx_live = fetch_usd_rates()
    if verbose:
        print(f"FX rates {'(live)' if fx_live else '(offline fallback)'}: {rates}")

    products = tracked_products()
    if not products:
        if verbose:
            print("No tracked products found; skipping price refresh.")
        return []

    summary: list[dict] = []
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for product in products:
        quote = get_quote(product, rates)
        price = quote_price(quote)
        if price is None:
            continue
        with closing(get_connection()) as conn, conn:
            conn.execute(
                "UPDATE products SET usd_market_price = ? WHERE id = ?",
                (price, product["id"]),
            )
            conn.execute(
                "UPDATE tracked_products SET last_scanned_at = ? WHERE product_id = ?",
                (now, product["id"]),
            )
        summary.append(
            {
                "id": product["id"],
                "name": product["name"],
                "usd": price,
                "source": quote["source"],
            }
        )
        if verbose:
            print(f"  {product['name']}: ${price} ({quote['source']})")

    set_meta(
        "last_price_refresh",
        _dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    return summary


def last_refresh_at() -> str | None:
    return get_meta("last_price_refresh")


def is_refresh_due(hours: int = REFRESH_INTERVAL_HOURS) -> bool:
    """True if prices have never been refreshed or the last refresh is older
    than `hours` (default 7 days)."""
    last = get_meta("last_price_refresh")
    if not last:
        return True
    try:
        last_dt = _dt.datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - last_dt) >= _dt.timedelta(hours=hours)


# ===========================================================================
# CLI entry point — run from cron for a true weekly background refresh:
#   0 6 * * 1  cd /path/to/PokemonStockTracker && python price_extractor.py
# ===========================================================================

if __name__ == "__main__":
    print("Active providers:", [p["name"] for p in active_providers()])
    updated = refresh_all_prices(verbose=True)
    print(f"Done — updated {len(updated)} product(s); last_price_refresh stamped.")
