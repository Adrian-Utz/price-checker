# Price Checker

A local web app for maintaining a product watchlist and checking prices once per day. It uses SerpApi Walmart and Home Depot product lookups, SQLite history, conservative request pacing, and cached results.

## Main Capabilities
1. Allow the user to input up to 20-100 URLs.
2. Look up Walmart and Home Depot products through their SerpApi engines.
3. Keep current results and historical observations in SQLite.
4. Restrict the user to one completed scan per local calendar day, resetting at local midnight.
5. Space requests with a minimum delay and randomized jitter.
6. Cache successful or failed URL checks for 24 hours by default.(Resets at midnight)
7. Export the watchlist and its saved price history as JSON or CSV.
8. Shut down the local server after the app tab is closed.

## Run locally

1. Create and activate a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`
3. Start the app: `python main.py`
4. The app automatically opens `http://127.0.0.1:5000` in your default browser.

The database is created as `price_checker.sqlite3` beside `main.py`. Set `PRICE_CHECKER_DB` to choose another location. Optional settings are `PRICE_CHECKER_MAX_URLS` (20-100), `PRICE_CHECKER_CACHE_HOURS`, `PRICE_CHECKER_MIN_DELAY`, and `PORT`. The daily scan limit resets at midnight according to the computer's local timezone; observation timestamps remain stored in UTC.

When the browser tab unloads, the app sends a local shutdown signal and exits after a short grace period. This also applies to a refresh or navigation if no new app request arrives during that grace period.

SerpApi requests wait up to 90 seconds by default. Set `SERPAPI_TIMEOUT_SECONDS` if your network is slower, for example `$env:SERPAPI_TIMEOUT_SECONDS = "120"`. The app does not automatically retry a timed-out request because SerpApi may already count it as a search.

### SerpApi mode

Create a SerpApi account and keep the API key out of source files. For a one-time setup, create a file named `.env` beside `main.py` with this content:

```dotenv
SERPAPI_API_KEY=your-key-here
```

The app loads `.env` automatically every time it starts, so after this one-time setup you can simply run `python main.py`. Keep `.env` private. The app uses SerpApi's `walmart_product` and `home_depot_product` engines and never opens Chromium. Walmart URLs should contain a product ID such as `/ip/product-name/123456789`. Home Depot URLs can contain a numeric ID such as `/p/product-name/987654321`; if they do not, the app uses the `home_depot` search engine to resolve one before requesting product details. SerpApi usage limits and pricing apply. The returned result should be reviewed for exact model, size, color, store, and location.

## Responsible use

Only check pages where automated access is permitted by the site's terms and applicable policies. Retailers can change their markup, rate-limit clients, or block automated traffic. Keep the list small, leave the delay enabled, and do not attempt to bypass CAPTCHAs, authentication, robots restrictions, or IP blocks. A browser context does not guarantee that a site will permit automation.

Price extraction through SerpApi uses structured product fields. Walmart and Home Depot are supported; Lowe's and generic sites are not supported by the current providers. Discontinued-product detection is not implemented.

## Features

### Upcoming

- Discontinued: Checks if the page or product is discontined, then alerts user.

### Implemented

- Graph view: Shows a line graph of the product over time.
- Watchlist export: Download current products and saved observations as JSON or CSV.
- Local Flask interface for adding and removing URLs.
- SQLite-backed current prices, scan observations, errors, and daily scan state.
- SerpApi product lookups with configurable timeouts.
- Resilient parsing for retailer product and offer response shapes.
- Per-URL failures are recorded while the rest of the scan continues.
