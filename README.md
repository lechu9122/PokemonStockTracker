# 🎴 Pokémon TCG Inventory & Portfolio Tracker — Oceania Edition

A **local-only**, single-file Streamlit app for tracking sealed Pokémon TCG
product inventory and its live portfolio value in **AUD 🇦🇺** and **NZD 🇳🇿**.

All data is stored locally in `pokemon_oceania_tracker.db` (SQLite). Nothing is
sent to any cloud database.

## Features

- **Lot-based accounting** — the same product bought at different times/prices is
  stored as separate *purchase lots*, so cost bases are never wrongly averaged.
- **Per-lot currency** — each lot remembers whether it was bought in AUD or NZD.
- **Live currency toggle** — switch the active display currency in the sidebar;
  every figure instantly re-converts. Rates come from the free, no-signup
  [Frankfurter API](https://api.frankfurter.app) (USD→AUD/NZD), with an offline
  fallback so the app always works.
- **Live pricing (pluggable providers)** — prices come from a standalone
  `price_extractor.py` with three providers tried in priority order:
  **eBay Browse API** (the **lowest** current active-listing price for each
  tracked sealed product) → **pokemontcg.io** (USD prices for single cards) →
  an always-on **mock** fallback. Every listing is normalised to USD *before*
  the minimum is taken, so the dashboard's `usd_market_price × FX` model is
  preserved and mixed-currency results stay correct.
- **Resilient + zero-config** — the app is fully functional with no API keys
  (mock covers everything), and if any live API is down, rate-limited, or
  broken, the extractor logs it and falls through to the next provider — it
  never crashes the dashboard.
- **Weekly auto-refresh for tracked products** — on load, if tracked prices are
  older than 7 days the app refreshes them automatically (tracked via
  `last_price_refresh` in `app_meta`). Only products in your inventory cache are
  scanned. You can also run `python price_extractor.py` from cron for a true
  background refresh, or hit **💹 Refresh all live prices now** in the UI.
- **Guided Inventory Logger** — a single **➕ Log Inventory** button opens a
  focused modal that walks you through it: **pick a set first**, then pick a
  product, then enter **price bought**, **units bought** and a **purchase date
  that defaults to today**. When eBay keys are present, choosing a set **live-
  searches eBay for all sealed listings of that set** (cheapest first, auctions
  excluded) so you can browse the real products on offer; a 🔍 box filters them.
  With no keys / no results it falls back to a curated per-set catalog. An
  **➕ Other / custom…** option covers anything not listed. Re-logging the same
  product appends a new purchase lot (it never overwrites an existing one).
- **"Rip 1 Pack/Box"** — one atomic SQLite transaction decrements sealed stock and
  increments ripped count, instantly updating the dashboard.
- **Visual analytics** — a donut of Realized vs Unrealized losses, and a bar chart
  of per-lot value change vs purchase price.

## Financial metrics (shown in the active currency)

| Metric | Definition |
| --- | --- |
| Total Money Put In | Σ `initial_quantity × purchase_price_local` |
| Total Sealed Worth | Σ `current_sealed_qty × usd_price × fx × premium` |
| Net Profit / Loss | Total Sealed Worth − remaining sealed cost basis |
| Realized Losses | Σ `ripped_quantity × purchase_price_local` |
| Unrealized Losses | Σ over lots where market < cost: `(cost − market) × sealed_qty` |

The **sealed premium** (default `×1.05`) models standard Oceania shipping/import
costs on sealed goods and is adjustable in the sidebar.

## Run it

### Quick start (one command)

The launcher creates the virtualenv, installs dependencies the first time, and
starts the app — no manual setup needed:

```bash
./run.sh                 # macOS / Linux
```

```bat
run.bat                  :: Windows
```

It opens on <http://localhost:8501> by default. To use a different port:

```bash
PORT=8600 ./run.sh       # macOS / Linux   (Windows: set PORT=8600 && run.bat)
```

Press **Ctrl+C** to stop the app.

### Manual setup (if you prefer)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Then open the URL Streamlit prints (default <http://localhost:8501>).
Click **Load demo data** in the sidebar to populate sample inventory.

## Turning on live eBay prices (optional)

Everything works without keys. To pull real eBay asking prices:

```bash
cp .env.example .env          # then paste your keys into .env
```

- Create a free app at <https://developer.ebay.com/> → **Application keys**, and
  put the **production** App ID / Cert ID into `EBAY_CLIENT_ID` /
  `EBAY_CLIENT_SECRET`. The eBay provider activates automatically on restart
  (the sidebar **Price sources** panel turns its dot 🟢).
- `EBAY_MARKETPLACE_ID` defaults to `EBAY_AU` (eBay has no NZ marketplace).
- `EBAY_ENV` selects `production` (default) or `sandbox`. **Sandbox keys (App ID
  contains `-SBX-`) authenticate but return almost no real listings**, so the
  scanner falls back to mock — use **production** keys for real prices.
- eBay's free Browse API returns the **lowest current asking price** from active
  **fixed-price ("Buy It Now") listings only** — auctions are excluded so live,
  incomplete bid amounts can't drag the price down. It is not sold comps (those
  need the partner-gated Marketplace Insights API) and excludes shipping — treat
  it as a trend signal. The lowest listing is
  the noisiest eBay signal (single packs / accessories / mismatched titles can
  drag it down), so tune each product's search query for best results.

### Background refresh via cron (weekly scheduler)

```bash
# refresh every Monday at 6am
0 6 * * 1  cd /path/to/PokemonStockTracker && ./.venv/bin/python price_extractor.py
```

## Schema

```
products(id PK, tcg_or_collectr_id UNIQUE, name, set_name, usd_market_price)
purchase_lots(id PK, product_id FK, purchase_price_local, initial_quantity,
              current_sealed_quantity, ripped_quantity, purchase_date,
              local_currency CHECK IN ('AUD','NZD'))
```
