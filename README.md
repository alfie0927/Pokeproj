# PokéProj

Pokémon card price tracker (eBay UK), sealed EV ranking and an AI eBay listing assistant.

## Where prices come from (eBay only, free)
1. **Real sold prices (Marketplace Insights)**: used automatically once eBay approves your keyset for this
   restricted API (Settings → "Test Marketplace Insights access"). Source `insights`.
2. **Sold prices, inferred**: the app follows cards (watched, drafted, viewed, and every rare+ the Deal finder
   sees) and tracks their UK listings, fixed price and auctions. When a followed listing vanishes or an auction
   ends, it asks eBay (Browse `getItem`): auction with bids → sold at the final bid; fixed price ended/out of stock
   with ≥1 sold → sold at list price (flagged when Best Offer was on, since the accepted offer is hidden); ended
   with stock left → seller ended it (ignored). Source `ebay`.
3. **Your own orders**: exact prices of cards you sold through this app. Source `own`.
4. **Today's UK asks, low quartile**: the cheaper end of current UK listings (the asks that actually sell), after
   removing accessories/fakes/foreign copies (stickers, binder inserts, metal cards, keyrings, Spanish…), listings
   with another set's number (`215/163` for `215/203`) and anything under 40 % of the median.

Market value = manual override → sold median (≥3 sales in 90 days: the 30-day median when there are 3+ that
recent, else the latest 10; Best-Offer sales dropped once 3 clean sales exist) → asks low quartile (≥3 listings) →
"thin" (1–2 sales/listings, shown but never used for deals) → none. **Cardmarket is never used for prices**: it's
shown on the card view as an EU reference, and only fills in sealed-EV hit cards that have no eBay data yet
(labelled). Played (LP) prices use a multiplier learned from cards with both raw and played sales once 10 cards
qualify (Settings until then). Every value says its basis, e.g. `sold ×7 (30 d, ebay)` or `asks ×22 (low quartile)`.

**Keep it running daily** (Windows Task Scheduler, 3 am):
```
schtasks /create /sc daily /st 03:00 /tn PokeProjRefresh /tr "D:\pokeproj\.venv\Scripts\python.exe D:\pokeproj\refresh_daily.py"
```
The daily job refreshes followed cards highest priority first (watched/draft/deal → viewed → scanner-seen),
2 Browse calls per card + 1 per ended listing, and stops at the daily budget (Settings, default 4,500 of eBay's
5,000). Watched/draft/deal cards are also re-checked every 6 hours while the app is running. Cards unseen for 30
days stop being followed. The deal scanner starts a pass every minute (titles first, 4 at a time; values 6 cards at
a time, 1 Browse call for a card it has never priced; listing photos last). With fewer than 1,000 of today's calls
left it slows to every 3 minutes and stops pricing new cards; at 0 it pauses until the next day.

**Speed notes**: eBay and TCGdex calls share kept-open connections (`ebay.session()`), listing checks run 6 at a
time (`ebay.get_items`), the database runs in WAL mode with one connection per thread, and price history / risers
and fallers read the small `ask_daily` table (one row per card, day and condition) instead of every listing row.

## How cards are read from photos (free by default)
`VISION_BACKEND = "ocr"` (default): RapidOCR runs offline on your CPU and reads the printed collector number
(`199/165`), set code (`MEW`, `SV2a`) and, for slabs, the PSA/BGS/CGC label. The TCGdex catalog turns number +
set code into the exact card; when several match you pick from a dropdown. **Condition is yours to set** on each
draft (defaults to NM). Tips: photograph straight-on, in focus, with the bottom corner readable.
`VISION_BACKEND = "claude"` + `ANTHROPIC_API_KEY`: Claude identifies and grades condition (~3–6p per card).

## Setup
1. `python -m venv .venv` then `.venv\Scripts\pip install streamlit anthropic rapidocr onnxruntime`
2. Copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` and fill it in.
   eBay: free developer account at developer.ebay.com → create Production and Sandbox keysets → User Tokens
   (OAuth, scopes `sell.inventory`, `sell.account`, `sell.fulfillment`). Production keys also need the marketplace
   account-deletion step (choose the exemption: this app stores no eBay user data).
3. Run: `.venv\Scripts\streamlit run app.py --server.address 0.0.0.0`
   Phone on home Wi-Fi: `http://<PC-IP>:8501` (allow Python through Windows Firewall).
4. In **Settings**: postcode → "Create / check inventory location", "Load my policies from eBay", your eBay
   username, postage. Then "Show required item specifics" to check nothing required is missing.
   Press "Run daily refresh now" once to seed tracking, then schedule it (above).
5. Test posting with `EBAY_ENV = "sandbox"` first, then switch to `"production"`.

## Pages
- **List cards** — upload front/back photos in any order → cards identified and priced → **review grid** (set
  condition, price, title, tick Accept, Save) → **Post** with a progress bar and cancel. Header shows your eBay
  listing allowance; posting is blocked with a reason when it's 0. **Live on eBay** syncs real prices from eBay
  (incl. Seller Hub edits), shows market now and a suggested price per listing, and lets you push price changes.
- **Card prices** — one search box (`charizard 199`, `sv08 238`, `199/165`, `Kingdra TG03`, `リザードン`), era filter,
  market overview (tracked cards, risers/fallers, watchlist with trend lines, recently viewed), card view with value
  by condition, history, live UK listings with photos, Watch, and **List this card →** (skips identification).
- **Sealed EV**: a ranking of every English Booster Box / Elite Trainer Box / Booster Bundle back to XY. Each row:
  stock product photo, **UK price** = median of recent eBay UK sales (`sold ×N`, 3+ sales) or a UK estimate from the
  cheaper current listings (`est.`) until then, **EV if opened** (estimated pull rates × UK card values), **profit
  if opened**, EV ÷ price and a verdict. "EV x % eBay-priced" says how much of the EV uses eBay card prices (the
  rest is a labelled Cardmarket stand-in until those cards have eBay data). Sealed sales are confirmed from listings
  that end sold and from units sold on multi-quantity listings. Photos come from TCGCSV (free daily mirror of
  TCGplayer's catalogue). Pull rates: `data/pull_rates.toml`; custom/Japanese sets: `data/sets.toml`.
- **Deal finder** (Buy): two feeds, filled by a background scanner while the app runs.
  *Buy now*: the newest UK listings, card read from the title (or OCR of the photo), condition from eBay's
  condition + title, compared with market value. Listed when at or under 70 % of market (modern, XY onwards) /
  50 % (vintage), rare+, worth ≥ £5, with a solid eBay value. Each shows **profit and ROI** if resold at market
  (minus your postage cost and selling fee %), how fast the card sells (`6 sold / 30 d`), when it was listed and
  where. Sort by newest / £ profit / ROI / % of market; min-profit filter. **Beep + Windows notification** when a
  deal lands (click opens the listing). *Auctions ending soon*: UK auctions ending within 2 h whose bid + postage
  is under the line, with a suggested **max bid**. The app never buys or bids. **Bought** adds the card to Inventory.
- **Inventory** (Buy): what you bought, cost, value now, unrealised and realised profit; **List this →** opens the
  List page with the card chosen; when it sells through the app, the sale price is filled in.
- **Settings** — pricing defaults, postage, eBay policies, item location, selling limits, daily refresh.

## Checks
`.venv\Scripts\python tests\test_core.py`
