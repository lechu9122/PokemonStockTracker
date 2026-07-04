"""
Pokémon TCG Inventory & Portfolio Tracker — Oceania Edition (AUD / NZD)
=======================================================================

A local-only, single-file Streamlit application for tracking sealed Pokémon TCG
product inventory and its real-time portfolio value in the Australian (AUD) and
New Zealand (NZD) markets.

Key design points
-----------------
* **Local-only storage** — all data lives in a local SQLite file
  (``pokemon_oceania_tracker.db``). No cloud database is used.
* **Lot-based accounting** — buying the *same* product at different times for
  different prices is stored as separate *purchase lots* so cost bases are never
  incorrectly averaged together.
* **Per-lot currency** — every lot remembers the currency it was actually bought
  in (AUD or NZD). The dashboard shows a single *active* currency and converts
  every stored amount into it by cross-converting through USD using live
  Frankfurter FX rates.
* **Live pricing (pluggable)** — USD market prices are refreshed via the
    standalone ``price_extractor`` module: eBay Browse API → pokemontcg.io →
    always-on mock fallback. The app works flawlessly with no API keys and
    survives any live API being down. Prices auto-refresh weekly for tracked
    products only.

Run with:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from contextlib import closing
from dataclasses import dataclass

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

import price_extractor as extractor

# ---------------------------------------------------------------------------
# Configuration & constants
# ---------------------------------------------------------------------------

DB_PATH = "pokemon_oceania_tracker.db"

FRANKFURTER_URL = "https://api.frankfurter.app/latest"

SUPPORTED_CURRENCIES = ("AUD", "NZD")
CURRENCY_LABELS = {"AUD": "AUD 🇦🇺", "NZD": "NZD 🇳🇿"}
CURRENCY_SYMBOL = {"AUD": "A$", "NZD": "NZ$"}

# Default Oceania shipping / import premium applied to sealed-goods market value.
DEFAULT_SEALED_MULTIPLIER = 1.05

# ---------------------------------------------------------------------------
# Sealed-product catalog (drives the Inventory Logger pick-a-set → pick-a-product
# flow). There is no free public API for *sealed* product per set, so this is a
# curated catalog of recent, commonly-traded Oceania sealed products. Each entry
# is {id, name, search_query}; the dict key is the set name. To track something
# not listed here, use the "➕ Other / custom…" option in the logger.
# ---------------------------------------------------------------------------

def _sealed_products(set_name: str, prefix: str, extras: list[tuple[str, str]] | None = None) -> list[dict]:
    """Build the standard sealed line-up for a set (Booster Box, ETB, Booster
    Bundle, Build & Battle Box) plus any set-specific ``extras`` (name, id-suffix)."""
    slug = prefix.lower().replace(" ", "-").replace("&", "and")
    base = [
        ("Booster Box", "booster-box"),
        ("Elite Trainer Box", "etb"),
        ("Booster Bundle", "booster-bundle"),
        ("Build & Battle Box", "build-battle-box"),
    ]
    items = base + (extras or [])
    return [
        {
            "id": f"{slug}-{suffix}",
            "name": f"{prefix} {name}",
            "search_query": f"Pokemon {prefix} {name} sealed",
        }
        for name, suffix in items
    ]


SET_CATALOG: dict[str, list[dict]] = {
    # --- Mega Evolution era (2025–2026) — newest first ---------------------
    "Mega Evolution — Chaos Rising": _sealed_products(
        "Mega Evolution — Chaos Rising", "Mega Evolution Chaos Rising"),
    "Mega Evolution — Perfect Order": _sealed_products(
        "Mega Evolution — Perfect Order", "Mega Evolution Perfect Order"),
    "Mega Evolution — Ascended Heroes": _sealed_products(
        "Mega Evolution — Ascended Heroes", "Mega Evolution Ascended Heroes"),
    "Mega Evolution — Phantasmal Flames": _sealed_products(
        "Mega Evolution — Phantasmal Flames", "Mega Evolution Phantasmal Flames"),
    "Mega Evolution (Base Set)": _sealed_products(
        "Mega Evolution (Base Set)", "Mega Evolution"),

    # --- Scarlet & Violet era (2023–2025) ---------------------------------
    "Scarlet & Violet — White Flare": _sealed_products(
        "Scarlet & Violet — White Flare", "White Flare"),
    "Scarlet & Violet — Black Bolt": _sealed_products(
        "Scarlet & Violet — Black Bolt", "Black Bolt"),
    "Scarlet & Violet — Destined Rivals": _sealed_products(
        "Scarlet & Violet — Destined Rivals", "Destined Rivals"),
    "Scarlet & Violet — Journey Together": _sealed_products(
        "Scarlet & Violet — Journey Together", "Journey Together"),
    "Scarlet & Violet — Prismatic Evolutions": _sealed_products(
        "Scarlet & Violet — Prismatic Evolutions", "Prismatic Evolutions",
        [("Super Premium Collection", "spc"), ("Surprise Box", "surprise-box"),
         ("Tech Sticker Collection", "tech-sticker")],
    ),
    "Scarlet & Violet — Surging Sparks": _sealed_products(
        "Scarlet & Violet — Surging Sparks", "Surging Sparks"),
    "Scarlet & Violet — Stellar Crown": _sealed_products(
        "Scarlet & Violet — Stellar Crown", "Stellar Crown"),
    "Scarlet & Violet — Shrouded Fable": _sealed_products(
        "Scarlet & Violet — Shrouded Fable", "Shrouded Fable",
        [("Elite Trainer Box (no Build & Battle)", "etb-only")],
    ),
    "Scarlet & Violet — Twilight Masquerade": _sealed_products(
        "Scarlet & Violet — Twilight Masquerade", "Twilight Masquerade"),
    "Scarlet & Violet — Temporal Forces": _sealed_products(
        "Scarlet & Violet — Temporal Forces", "Temporal Forces"),
    "Scarlet & Violet — Paldean Fates": _sealed_products(
        "Scarlet & Violet — Paldean Fates", "Paldean Fates",
        [("Premium Collection", "premium"), ("Tin", "tin")],
    ),
    "Scarlet & Violet — Paradox Rift": _sealed_products(
        "Scarlet & Violet — Paradox Rift", "Paradox Rift"),
    "Scarlet & Violet — 151": _sealed_products(
        "Scarlet & Violet — 151", "151",
        [("Ultra Premium Collection", "upc"), ("Poster Collection", "poster"),
         ("Binder Collection", "binder")],
    ),
    "Scarlet & Violet — Obsidian Flames": _sealed_products(
        "Scarlet & Violet — Obsidian Flames", "Obsidian Flames"),
    "Scarlet & Violet — Paldea Evolved": _sealed_products(
        "Scarlet & Violet — Paldea Evolved", "Paldea Evolved"),
    "Scarlet & Violet — Base Set": _sealed_products(
        "Scarlet & Violet — Base Set", "Scarlet & Violet Base"),

    # --- Sword & Shield era ------------------------------------------------
    "Crown Zenith": _sealed_products(
        "Crown Zenith", "Crown Zenith",
        [("Pokemon Center Elite Trainer Box", "pc-etb")],
    ),
    "Sword & Shield — Astral Radiance": _sealed_products(
        "Sword & Shield — Astral Radiance", "Astral Radiance",
        [("Trainer Toolkit", "trainer-toolkit")],
    ),
}

# Sentinel set option that reveals free-text inputs for anything not catalogued.
CUSTOM_SET_OPTION = "➕ Other / custom…"

# Offline fallback FX rates (1 USD -> X). Only used if Frankfurter is unreachable.
FALLBACK_FX = {"AUD": 1.52, "NZD": 1.66}

HTTP_TIMEOUT = 8  # seconds


# ===========================================================================
# 1. Database layer
# ===========================================================================

def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults.

    A fresh connection per operation is perfectly fine for a local single-user
    app and side-steps Streamlit's thread/rerun model. Foreign keys are enabled
    so the schema's referential integrity is enforced.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """Create the schema if it does not already exist."""
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


# --- Product helpers -------------------------------------------------------

def upsert_product(
    tcg_id: str,
    name: str,
    set_name: str,
    usd_price: float,
    search_query: str | None = None,
) -> int:
    """Insert a new product or update an existing one (matched on tcg_id).

    ``search_query`` is the text the eBay provider searches with; it defaults to
    the product name. Returns the product's primary-key id.
    """
    search_query = search_query or name
    with closing(get_connection()) as conn, conn:
        cur = conn.execute(
            "SELECT id FROM products WHERE tcg_or_collectr_id = ?", (tcg_id,)
        )
        row = cur.fetchone()
        if row:
            conn.execute(
                """UPDATE products
                       SET name = ?, set_name = ?, usd_market_price = ?,
                           search_query = ?
                     WHERE id = ?""",
                (name, set_name, usd_price, search_query, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            """INSERT INTO products
                   (tcg_or_collectr_id, name, set_name, usd_market_price, search_query)
               VALUES (?, ?, ?, ?, ?)""",
            (tcg_id, name, set_name, usd_price, search_query),
        )
        return cur.lastrowid


def update_product_price(product_id: int, usd_price: float) -> None:
    with closing(get_connection()) as conn, conn:
        conn.execute(
            "UPDATE products SET usd_market_price = ? WHERE id = ?",
            (usd_price, product_id),
        )


def add_purchase_lot(
    product_id: int,
    purchase_price_local: float,
    quantity: int,
    purchase_date: str,
    local_currency: str,
) -> None:
    """Append a brand-new purchase lot to an existing product."""
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """INSERT INTO purchase_lots (
                   product_id, purchase_price_local, initial_quantity,
                   current_sealed_quantity, ripped_quantity,
                   purchase_date, local_currency)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (
                product_id,
                purchase_price_local,
                quantity,
                quantity,
                purchase_date,
                local_currency,
            ),
        )


def rip_one(lot_id: int) -> bool:
    """Atomically 'rip' one sealed unit from a lot.

    Decrements ``current_sealed_quantity`` by 1 and increments
    ``ripped_quantity`` by 1 inside a single transaction. Returns ``False`` (no
    change) if the lot has no sealed stock remaining.
    """
    with closing(get_connection()) as conn, conn:
        row = conn.execute(
            "SELECT current_sealed_quantity FROM purchase_lots WHERE id = ?",
            (lot_id,),
        ).fetchone()
        if not row or row["current_sealed_quantity"] <= 0:
            return False
        conn.execute(
            """UPDATE purchase_lots
                   SET current_sealed_quantity = current_sealed_quantity - 1,
                       ripped_quantity         = ripped_quantity + 1
                 WHERE id = ? AND current_sealed_quantity > 0""",
            (lot_id,),
        )
        return True


def delete_lot(lot_id: int) -> None:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT product_id FROM purchase_lots WHERE id = ?",
            (lot_id,),
        ).fetchone()
    with closing(get_connection()) as conn, conn:
        conn.execute("DELETE FROM purchase_lots WHERE id = ?", (lot_id,))
    if row:
        extractor.untrack_product_if_unused(int(row["product_id"]))


def load_inventory() -> pd.DataFrame:
    """Return one row per purchase lot, joined with its product details."""
    with closing(get_connection()) as conn:
        df = pd.read_sql_query(
            """
            SELECT
                l.id                       AS lot_id,
                p.id                       AS product_id,
                p.tcg_or_collectr_id       AS tcg_id,
                p.name                     AS name,
                p.set_name                 AS set_name,
                p.usd_market_price         AS usd_market_price,
                l.purchase_price_local     AS purchase_price_local,
                l.initial_quantity         AS initial_quantity,
                l.current_sealed_quantity  AS current_sealed_quantity,
                l.ripped_quantity          AS ripped_quantity,
                l.purchase_date            AS purchase_date,
                l.local_currency           AS local_currency
            FROM purchase_lots l
            JOIN products p ON p.id = l.product_id
            ORDER BY p.name, l.purchase_date
            """,
            conn,
        )
    return df


def load_products() -> pd.DataFrame:
    with closing(get_connection()) as conn:
        return pd.read_sql_query(
            "SELECT id, tcg_or_collectr_id, name, set_name, usd_market_price "
            "FROM products ORDER BY name",
            conn,
        )


# ===========================================================================
# 2. Live currency conversion (Frankfurter)
# ===========================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fx_rates() -> tuple[dict[str, float], str]:
    """Fetch live USD -> {AUD, NZD} rates from the Frankfurter API.

    Cached for an hour. Falls back to hard-coded approximate rates if the API
    is unreachable so the app keeps working offline. Returns ``(rates, source)``.
    """
    try:
        resp = requests.get(
            FRANKFURTER_URL,
            params={"from": "USD", "to": ",".join(SUPPORTED_CURRENCIES)},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        rates = {c: float(data["rates"][c]) for c in SUPPORTED_CURRENCIES}
        return rates, f"Frankfurter live ({data.get('date', 'n/a')})"
    except Exception:  # noqa: BLE001 — any network/parse failure -> fallback
        return dict(FALLBACK_FX), "Offline fallback rates"


def usd_to_active(usd_amount: float, active: str, rates: dict[str, float]) -> float:
    """Convert a USD amount into the active display currency."""
    return usd_amount * rates[active]


def local_to_active(
    amount: float, from_currency: str, active: str, rates: dict[str, float]
) -> float:
    """Cross-convert an amount in ``from_currency`` into the active currency.

    Goes via USD: amount_usd = amount / rate[from]; then * rate[active].
    """
    if from_currency == active:
        return amount
    amount_usd = amount / rates[from_currency]
    return amount_usd * rates[active]


# ===========================================================================
# 3. Live pricing
# ===========================================================================
# All price fetching lives in the standalone, pluggable `price_extractor`
# module (eBay Browse API + pokemontcg.io + always-on mock fallback). The app
# calls it through a single path so there is exactly one place prices come from.


def fetch_product_usd_price(product_row: dict) -> tuple[float, str]:
    """Thin wrapper around the extractor for the add-product flow."""
    return extractor.get_usd_price(product_row)


# ===========================================================================
# 4. Core financial calculations
# ===========================================================================

@dataclass
class Portfolio:
    """All headline figures, expressed in the active display currency."""

    total_put_in: float = 0.0
    total_sealed_worth: float = 0.0
    remaining_cost_basis: float = 0.0
    net_profit_loss: float = 0.0
    realized_losses: float = 0.0
    unrealized_losses: float = 0.0


def compute_portfolio(
    df: pd.DataFrame, active: str, rates: dict[str, float], multiplier: float
) -> Portfolio:
    """Compute every headline figure in the active currency.

    Definitions (all converted to ``active`` currency):
      * Total Money Put In   = Σ initial_quantity * purchase_price_local
      * Total Sealed Worth   = Σ current_sealed_qty * usd_price * fx * multiplier
      * Remaining Cost Basis = Σ current_sealed_qty * purchase_price_local
      * Net Profit/Loss      = Total Sealed Worth − Remaining Cost Basis
      * Realized Losses      = Σ ripped_quantity * purchase_price_local
      * Unrealized Losses    = Σ over lots where market < cost of
                               (cost_per_unit − market_per_unit) * sealed_qty
    """
    p = Portfolio()
    if df.empty:
        return p

    for lot in df.itertuples(index=False):
        cur = lot.local_currency

        # Per-unit values in the active currency.
        purchase_unit_active = local_to_active(
            lot.purchase_price_local, cur, active, rates
        )
        market_unit_active = usd_to_active(lot.usd_market_price, active, rates) * multiplier

        p.total_put_in += lot.initial_quantity * purchase_unit_active
        p.total_sealed_worth += lot.current_sealed_quantity * market_unit_active
        p.remaining_cost_basis += lot.current_sealed_quantity * purchase_unit_active
        p.realized_losses += lot.ripped_quantity * purchase_unit_active

        # Unrealized loss only when current market is below original cost.
        if market_unit_active < purchase_unit_active:
            p.unrealized_losses += (
                (purchase_unit_active - market_unit_active)
                * lot.current_sealed_quantity
            )

    p.net_profit_loss = p.total_sealed_worth - p.remaining_cost_basis
    return p


def build_display_table(
    df: pd.DataFrame, active: str, rates: dict[str, float], multiplier: float
) -> pd.DataFrame:
    """Build the main stock-list table, all money columns in active currency."""
    rows = []
    sym = CURRENCY_SYMBOL[active]
    for lot in df.itertuples(index=False):
        purchase_unit = local_to_active(
            lot.purchase_price_local, lot.local_currency, active, rates
        )
        market_unit = usd_to_active(lot.usd_market_price, active, rates) * multiplier
        rows.append(
            {
                "Lot ID": lot.lot_id,
                "Product Name": lot.name,
                "Set": lot.set_name,
                "Lot Currency": lot.local_currency,
                "Sealed Qty": lot.current_sealed_quantity,
                "Ripped": lot.ripped_quantity,
                f"Purchase Price/Unit ({sym})": round(purchase_unit, 2),
                f"Total Cost Basis ({sym})": round(
                    purchase_unit * lot.current_sealed_quantity, 2
                ),
                f"Live Market/Unit ({sym})": round(market_unit, 2),
                f"Total Market Value ({sym})": round(
                    market_unit * lot.current_sealed_quantity, 2
                ),
                "Purchase Date": lot.purchase_date,
            }
        )
    return pd.DataFrame(rows)


# ===========================================================================
# 5. Demo data (optional, for an out-of-the-box experience)
# ===========================================================================

DEMO_PRODUCTS = [
    ("sv3pt5-booster-box", "151 Booster Box", "Scarlet & Violet 151", 320.0),
    ("swsh12pt5-etb", "Crown Zenith Elite Trainer Box", "Crown Zenith", 55.0),
    ("sv1-booster-bundle", "Scarlet & Violet Booster Bundle", "Scarlet & Violet", 28.0),
]


def load_demo_data() -> bool:
    """Seed a few products & lots, but only when the DB is empty so repeated
    clicks never duplicate lots. Returns True if data was seeded."""
    if not load_inventory().empty:
        return False
    today = _dt.date.today()
    seeds = [
        # (tcg_id, name, set, usd, price_local, qty, days_ago, currency)
        (*DEMO_PRODUCTS[0], 540.0, 2, 90, "AUD"),
        (*DEMO_PRODUCTS[0], 575.0, 1, 20, "NZD"),
        (*DEMO_PRODUCTS[1], 95.0, 4, 60, "AUD"),
        (*DEMO_PRODUCTS[2], 49.0, 6, 30, "NZD"),
    ]
    for tcg_id, name, set_name, usd, price_local, qty, days_ago, cur in seeds:
        pid = upsert_product(tcg_id, name, set_name, usd)
        extractor.track_product(pid)
        date = (today - _dt.timedelta(days=days_ago)).isoformat()
        add_purchase_lot(pid, price_local, qty, date, cur)


# ===========================================================================
# 6. Streamlit UI
# ===========================================================================

def money(value: float, active: str) -> str:
    return f"{CURRENCY_SYMBOL[active]}{value:,.2f}"


def render_sidebar() -> tuple[str, float, dict[str, float], str]:
    """Render sidebar controls; return (active_currency, multiplier, rates, src)."""
    st.sidebar.title("⚙️ Settings")

    active = st.sidebar.radio(
        "Active local currency",
        options=list(SUPPORTED_CURRENCIES),
        format_func=lambda c: CURRENCY_LABELS[c],
        horizontal=True,
        help="Switching instantly re-converts every figure on the dashboard.",
    )

    multiplier = st.sidebar.slider(
        "Sealed shipping/import premium",
        min_value=1.00,
        max_value=1.30,
        value=DEFAULT_SEALED_MULTIPLIER,
        step=0.01,
        help="Applied to sealed market value to model Oceania shipping/import costs.",
    )

    if st.sidebar.button("🔄 Refresh FX rates"):
        fetch_fx_rates.clear()

    rates, fx_source = fetch_fx_rates()
    st.sidebar.caption(f"**FX source:** {fx_source}")
    st.sidebar.caption(
        f"1 USD = {rates['AUD']:.4f} AUD · {rates['NZD']:.4f} NZD"
    )

    # --- Price source status (mirrors the Node app's /api/sources) ----------
    st.sidebar.divider()
    st.sidebar.markdown("**💹 Price sources**")
    for src in extractor.provider_status():
        icon = "🟢" if src["usable"] else "⚪️"
        st.sidebar.caption(f"{icon} {src['name']}")
    last = extractor.last_refresh_at()
    st.sidebar.caption(
        f"Last price refresh: {last[:16].replace('T', ' ') if last else 'never'} UTC"
    )
    st.sidebar.caption("eBay activates automatically once keys are in `.env`.")

    st.sidebar.divider()
    if st.sidebar.button("📦 Load demo data"):
        load_demo_data()
        st.rerun()
    if st.sidebar.button("🗑️ Reset all data", type="secondary"):
        with closing(get_connection()) as conn, conn:
            conn.execute("DELETE FROM purchase_lots;")
            conn.execute("DELETE FROM products;")
        st.rerun()

    return active, multiplier, rates, fx_source


def render_metrics(p: Portfolio, active: str) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("💰 Total Money Put In", money(p.total_put_in, active))
    c2.metric("📦 Total Sealed Worth", money(p.total_sealed_worth, active))
    c3.metric(
        "📈 Net Profit / Loss",
        money(p.net_profit_loss, active),
        delta=f"{money(p.net_profit_loss, active)}",
    )

    c4, c5, c6 = st.columns(3)
    c4.metric("🔪 Realized Losses (ripped)", money(p.realized_losses, active))
    c5.metric("📉 Unrealized Losses (market drop)", money(p.unrealized_losses, active))
    c6.metric("🧾 Remaining Cost Basis", money(p.remaining_cost_basis, active))


def render_charts(df: pd.DataFrame, p: Portfolio, active: str, rates, multiplier) -> None:
    st.subheader("📊 Visual Analytics")
    col1, col2 = st.columns(2)

    # --- Losses breakdown ---------------------------------------------------
    with col1:
        st.markdown("**Losses: Realized vs Unrealized**")
        loss_df = pd.DataFrame(
            {
                "Type": ["Realized (ripped)", "Unrealized (market drop)"],
                "Amount": [p.realized_losses, p.unrealized_losses],
            }
        )
        if loss_df["Amount"].sum() > 0:
            fig = px.pie(
                loss_df,
                names="Type",
                values="Amount",
                color="Type",
                color_discrete_map={
                    "Realized (ripped)": "#d62728",
                    "Unrealized (market drop)": "#ff7f0e",
                },
                hole=0.45,
            )
            fig.update_traces(textinfo="label+value")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No losses recorded yet. 🎉")

    # --- Gainers / losers ---------------------------------------------------
    with col2:
        st.markdown("**Value change vs purchase (per lot, sealed stock)**")
        change_rows = []
        for lot in df.itertuples(index=False):
            if lot.current_sealed_quantity <= 0:
                continue
            purchase_unit = local_to_active(
                lot.purchase_price_local, lot.local_currency, active, rates
            )
            market_unit = usd_to_active(lot.usd_market_price, active, rates) * multiplier
            change_rows.append(
                {
                    "Item": f"{lot.name} ({lot.purchase_date})",
                    "Change": round(
                        (market_unit - purchase_unit) * lot.current_sealed_quantity, 2
                    ),
                }
            )
        if change_rows:
            change_df = pd.DataFrame(change_rows).sort_values("Change")
            change_df["Direction"] = change_df["Change"].apply(
                lambda v: "Gain" if v >= 0 else "Loss"
            )
            fig = px.bar(
                change_df,
                x="Change",
                y="Item",
                orientation="h",
                color="Direction",
                color_discrete_map={"Gain": "#2ca02c", "Loss": "#d62728"},
            )
            fig.update_layout(showlegend=False, yaxis_title="", xaxis_title=f"Δ {CURRENCY_SYMBOL[active]}")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Add some sealed stock to see gainers/losers.")


def set_search_query(set_label: str) -> str:
    """Build the eBay search query for a set label (drops the era em-dash)."""
    clean = set_label.replace(" — ", " ").strip()
    return f"Pokemon {clean} sealed"


@st.cache_data(ttl=900, show_spinner=False)
def live_set_listings(set_label: str) -> list[dict]:
    """Live eBay sealed listings for a set, cached 15 min so typing in the filter
    (which reruns the dialog) never re-hits eBay. Empty if eBay is off/unreachable."""
    return extractor.search_sealed_products(set_search_query(set_label), limit=50)


@st.dialog("➕ Log Inventory", width="large")
def inventory_logger_dialog() -> None:
    """Focused modal: pick a set → pick a product from that set (searchable) →
    enter price bought, units bought and purchase date (defaults to today)."""

    # --- Step 1: pick the set ----------------------------------------------
    st.markdown("**1 · Choose a set**")
    set_choice = st.selectbox(
        "Set",
        options=list(SET_CATALOG.keys()) + [CUSTOM_SET_OPTION],
        index=0,
        help="Pick the set first — the product list below is filtered to it.",
    )

    custom = set_choice == CUSTOM_SET_OPTION
    chosen_product: dict | None = None
    set_name = set_choice

    if custom:
        # --- Escape hatch: anything not in the catalog ----------------------
        st.markdown("**2 · Describe the product**")
        c1, c2 = st.columns(2)
        name = c1.text_input("Product name *", key="log_custom_name")
        set_name = c2.text_input("Set name", key="log_custom_set")
        search_query = st.text_input(
            "eBay search query (optional)",
            key="log_custom_sq",
            help="What the eBay provider searches for. Defaults to the product name.",
        )
        if name:
            chosen_product = {
                "id": (name.lower().replace(" ", "-")),
                "name": name,
                "search_query": search_query or name,
            }
    else:
        # --- Step 2: pick a product within the set (searchable) -------------
        st.markdown("**2 · Choose a product**")
        with st.spinner("Searching eBay for sealed listings…"):
            listings = live_set_listings(set_choice)

        if listings:
            source = listings  # live eBay results, cheapest first
            st.caption(
                f"🔴 Live eBay — {len(listings)} sealed listings found. "
                "Prices shown are the current lowest per title."
            )
        else:
            source = SET_CATALOG[set_choice]  # graceful fallback
            st.caption(
                "⚪️ Live eBay search unavailable (no results / keys) — "
                "showing the curated product list."
            )

        search = st.text_input(
            "🔍 Filter products",
            key="log_search",
            placeholder="Type to filter, e.g. 'booster box' or 'etb'",
        )
        products = source
        if search:
            q = search.lower()
            products = [p for p in products if q in p["name"].lower()]
        if not products:
            st.warning("No products match that filter. Clear it or pick another set.")
        else:
            def _label(p: dict) -> str:
                if p.get("usd_price") is not None:
                    return f"{p['name']}  ·  ~US${p['usd_price']:.2f}"
                return p["name"]

            labels = [_label(p) for p in products]
            # Key by set so switching sets starts a fresh selection (never a
            # stale, out-of-options value carried over from the previous set).
            picked = st.selectbox("Product", labels, key=f"log_product_{set_choice}")
            chosen_product = products[labels.index(picked)]
            if chosen_product.get("url"):
                st.caption(f"🔗 [View this listing on eBay]({chosen_product['url']})")

    st.divider()

    # --- Step 3: purchase details ------------------------------------------
    st.markdown("**3 · Purchase details**")
    c1, c2, c3 = st.columns(3)
    price_local = c1.number_input(
        "Price bought / unit", min_value=0.0, step=1.0, key="log_price",
        help="Price you paid per unit, in the lot currency.",
    )
    units = c2.number_input("Units bought", min_value=1, step=1, value=1, key="log_units")
    currency = c3.selectbox("Currency", SUPPORTED_CURRENCIES, key="log_ccy")

    c4, c5 = st.columns(2)
    pdate = c4.date_input("Purchase date", value=_dt.date.today(), key="log_date")
    auto_price = c5.checkbox("Auto-fetch USD market price", value=True, key="log_auto")

    # --- Save ---------------------------------------------------------------
    if st.button("💾 Add to inventory", type="primary", width="stretch"):
        if chosen_product is None:
            st.error("Pick (or describe) a product first.")
            return
        sq = chosen_product["search_query"]
        # For a live eBay listing, store exactly the price shown (the cheapest
        # for this title) — no redundant re-fetch, so the toast matches the pick.
        # For catalog/custom products (no price in hand), optionally auto-fetch.
        listing_usd = chosen_product.get("usd_price")
        if listing_usd is not None:
            final_usd, source = float(listing_usd), "ebay listing"
        elif auto_price:
            final_usd, source = fetch_product_usd_price(
                {
                    "tcg_or_collectr_id": chosen_product["id"],
                    "name": chosen_product["name"],
                    "set_name": set_name,
                    "search_query": sq,
                }
            )
        else:
            final_usd, source = 0.0, "manual"
        pid = upsert_product(chosen_product["id"], chosen_product["name"],
                             set_name, final_usd, sq)
        extractor.track_product(pid)
        add_purchase_lot(pid, price_local, int(units), pdate.isoformat(), currency)
        st.session_state["logger_msg"] = (
            f"Logged {int(units)} × {chosen_product['name']} "
            f"(USD price {final_usd:.2f} via {source})."
        )
        st.rerun()


def render_add_search() -> None:
    """Inventory logger entry point — a single button that opens a focused modal."""
    st.subheader("➕ Inventory Logger")
    st.caption("Pick a set, then a product from that set, enter what you paid — done.")
    if st.button("➕ Log Inventory", type="primary"):
        inventory_logger_dialog()


def render_lot_manager(df: pd.DataFrame, active: str, rates, multiplier) -> None:
    """List every purchase lot with a 'Rip 1' action and live price refresh."""
    st.subheader("🎴 Purchase Lots — Manage & Rip")
    if df.empty:
        st.info("No inventory yet. Add products above or load demo data.")
        return

    for lot in df.itertuples(index=False):
        purchase_unit = local_to_active(
            lot.purchase_price_local, lot.local_currency, active, rates
        )
        market_unit = usd_to_active(lot.usd_market_price, active, rates) * multiplier
        cols = st.columns([3, 1.4, 1.4, 1.6, 1.2, 1.2])
        cols[0].markdown(
            f"**{lot.name}** · {lot.set_name}  \n"
            f"<small>Lot {lot.lot_id} · bought {lot.purchase_date} · {lot.local_currency}</small>",
            unsafe_allow_html=True,
        )
        cols[1].metric("Sealed", lot.current_sealed_quantity)
        cols[2].metric("Ripped", lot.ripped_quantity)
        cols[3].metric("Cost/unit", money(purchase_unit, active))
        cols[4].metric("Mkt/unit", money(market_unit, active))
        with cols[5]:
            disabled = lot.current_sealed_quantity <= 0
            if st.button("🔪 Rip 1", key=f"rip_{lot.lot_id}", disabled=disabled,
                         width="stretch"):
                if rip_one(lot.lot_id):
                    st.toast(f"Ripped 1 × {lot.name}")
                    st.rerun()
            if st.button("🗑️", key=f"del_{lot.lot_id}", width="stretch",
                         help="Delete this lot"):
                delete_lot(lot.lot_id)
                st.rerun()


def maybe_auto_refresh() -> None:
    """Run a price refresh at most once per session, and only if the last
    refresh is older than 7 days (the scheduled-refresh requirement). Runs *before*
    inventory is loaded so the page renders the freshest numbers."""
    if st.session_state.get("auto_refresh_checked"):
        return
    st.session_state["auto_refresh_checked"] = True
    if extractor.is_refresh_due():
        with st.spinner("Auto-refreshing prices (24h schedule)…"):
            summary = extractor.refresh_all_prices(verbose=False)
        if summary:
            st.session_state["auto_refresh_msg"] = (
                f"Auto-refreshed {len(summary)} product price(s)."
            )


def main() -> None:
    st.set_page_config(
        page_title="Pokémon TCG Tracker — Oceania",
        page_icon="🎴",
        layout="wide",
    )
    init_db()
    extractor.ensure_schema()
    maybe_auto_refresh()

    active, multiplier, rates, _ = render_sidebar()

    st.title("🎴 Pokémon TCG Inventory & Portfolio Tracker")
    st.caption(
        f"Oceania edition · all figures shown in **{CURRENCY_LABELS[active]}** · "
        f"sealed premium ×{multiplier:.2f}"
    )

    if msg := st.session_state.pop("auto_refresh_msg", None):
        st.toast(msg)
    if msg := st.session_state.pop("logger_msg", None):
        st.toast(msg, icon="✅")

    df = load_inventory()
    portfolio = compute_portfolio(df, active, rates, multiplier)

    render_metrics(portfolio, active)
    st.divider()

    # Bulk price refresh for all products (single path via the extractor).
    cprice, _ = st.columns([1, 4])
    if cprice.button("💹 Refresh all live prices now"):
        with st.spinner("Fetching live prices…"):
            summary = extractor.refresh_all_prices(verbose=False)
        st.toast(f"Refreshed {len(summary)} product(s).")
        st.rerun()

    # Main stock list table.
    st.subheader("📋 Main Stock List")
    table = build_display_table(df, active, rates, multiplier)
    if table.empty:
        st.info("No inventory yet.")
    else:
        st.dataframe(table, width="stretch", hide_index=True)

    st.divider()
    render_lot_manager(df, active, rates, multiplier)

    st.divider()
    render_charts(df, portfolio, active, rates, multiplier)

    st.divider()
    render_add_search()


if __name__ == "__main__":
    main()
