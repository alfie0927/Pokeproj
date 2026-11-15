# Roadmap

Things deliberately left out so far, and when to pick them up.

## Card reading (OCR)
- **Test on real phone photos.** OCR was only checked on clean TCGdex renders (3/3 correct, 3–16 s). Run 10–20 real
  pairs (English, Japanese, a slab, a reverse holo, glare/angle) and note misreads.
- **Thread count.** RapidOCR is pinned to 4 threads (`OCR_THREADS` in secrets overrides it). Its default (all cores)
  was 5× slower on this i7-12700H. Re-tune if the PC changes.
- **Model choice.** Using mobile PP-OCRv5 (English + Japanese in one model). If small print misreads, try the
  `small` model (slower), or a larger bottom-strip upscale in `vision._ocr`.
- **Local vision model (Ollama) as a third backend** if OCR misreads often, or for a rough condition guess:
  `qwen3-vl:4b` (3.3 GB), same `CardRead` JSON schema via `/api/chat` `images` + `format`. ~1–3 min/card on CPU.
- **Variant detection** (reverse holo / 1st edition). OCR can't see foil; currently always "normal" — set it on the draft.
- **Photo pairing check.** OCR doesn't verify image 2 is the back of the same card.
- **Image-hash tie-breaker** when number + set code match several cards (rare).

## Prices
- **Check sold-price inference against reality** once eBay keys are in: compare a week of inferred sales with eBay's
  Price Guide for a few cards; watch the `gone` count per card (ended items eBay stopped returning).
- **Best Offer sales** are now flagged and dropped once a card has 3 clean sales. If many cards only ever sell via
  Best Offer, compare their flagged sales with Terapeak and consider a discount factor.
- **Condition multipliers** (LP 0.8 / MP 0.6 / HP 0.4) are fixed guesses. Replace with real LP/MP data once
  there are enough tracked sales.
- **Marketplace Insights** is wired in but unapproved: apply for access; `ebay.sold_search` field names
  (`lastSoldPrice`, `lastSoldDate`, `buyingOptions`) are from eBay's docs and need a check on the first real response.
- **Budget**: the scanner + daily refresh share 4,500 Browse calls/day. If "stopped at the daily budget" shows most
  days, raise eBay's limit (Application Growth Check) or lower scanner-seen cards to every other day.
- **Sealed EV hit cards** still fall back to Cardmarket until each card has eBay data; they're followed at the
  lowest priority, so EV accuracy improves as the budget reaches them.
- **MP/HP multipliers** follow the learned LP ratio (× 0.75 / × 0.5); learn them separately if titles ever say MP/HP
  reliably enough to bucket.

## Sealed EV
- **Pull rates are estimates** (`data/pull_rates.toml`). Check the era defaults and special-set overrides against a
  current pull-rate source; the ranking is only as good as these numbers.
- **Trend signal needs history**: "Hold / invest" only appears after 7+ days of daily refreshes. Schedule
  `refresh_daily.py` so it builds while the app is closed.
- Japanese sealed (boxes of 20/30 packs) only via `data/sets.toml` entries with their own rates.
- Sets with few UK listings (old XY/SM boxes) are flagged "thin data" — consider a manual price in `sets.toml`.

## Deal finder
- **Runs only while the app is running.** For 24/7 without the dashboard open, run `deals.scan()` from a
  scheduled script (every 5 min); add a notification (email / Windows toast) when a deal lands.
- **Condition is title + eBay condition only**: a "NM" title on a scuffed card still reads as NM. Grading from the
  photo needs the Claude backend (or an Ollama vision model) — see Card reading.
- **One broad query ("pokemon", 200 newest)**: busy evenings can post more than 200 listings between scans. Add a
  second page (`offset=200`) or per-era queries if the "new listings" count sits at 200 every scan.
- Auctions ending soon with no bids are a second source of deals (needs an "ending soonest" scan + bid reminder).

## Reseller tools (next)
- **Sealed deals**: Booster Boxes / ETBs listed below their recent sold average, in the Deal finder.
- **Grading arbitrage**: raw cards whose PSA 10 value minus grading cost and a gem-rate guess beats the raw price.
- **Phone alerts**: push deals to your phone (needs a free service such as ntfy.sh or a Telegram bot).
- **Typo/misspelled listings**: searches for common misspellings (e.g. "Charzard") that other buyers miss.
- **Sealed query spam**: a few listings name other sets to game search (e.g. "Silver Tempest ETB 151"); excluded
  per product in `data/sets.toml` for now. A general rule (reject titles naming two different sets) would be cleaner.
- **Auction scan cost**: 2 pages of "ending soonest" every 10 minutes; first-time market lookups for new cards
  happen only while more than 1,000 Browse calls are left today.

## Listing
- Trainer/Galarian Gallery cards have no TCGdex image: drafts show your photo only.
- Best Offer and auctions; per-draft strategy switching.
- HEIC photos from iPhone (`pillow-heif`) if an upload fails.

## Going public later
- Web frontend over `core/`, accounts, per-user eBay OAuth, Postgres, eBay account-deletion webhook,
  per-user API costs. Re-check every data source's terms for commercial use.
