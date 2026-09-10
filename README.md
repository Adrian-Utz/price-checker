# Price Checker

A local web app for maintaining a product watchlist and checking prices once per day. It uses SerpApi Walmart and Home Depot product lookups, SQLite history, conservative request pacing, and cached results.

**Written by:** AJ Utz  
**Written on:** 7/27/2026  
**Last Update on:** 9/10/2026  
**Latest Version:** 0.0.1

## Main Capabilities
1. Allow the user to input up to 20-100 URLs. (Customizable)
2. Look up Walmart and Home Depot products through SerpApi engines. 
3. Look up Walmart, Home Depot, Lowes, Ace Hardware, and Sam's Club items with Unwrangle's engines. 
4. Keep current results and historical observations in SQLite.
5. Restrict the user to one completed scan per local calendar day, resetting at local midnight.
6. Space requests with a minimum delay and randomized jitter.
7. Cache successful or failed URL checks for 24 hours by default. (Resets at midnight)
8. Export the watchlist and its saved price history as JSON or CSV.
9. Shut down the local server after the app tab is closed.
10. Import a saved watchlist file to check it.
11. Dev Mode.

## Setting up a Virtual Environment

Setting up a virtual environment using Windows Powershell. (Not required but recommended)
1. In the directory you put the program: 
```python
python -m venv .venv
```
2. Then: 
```python
.venv\Scripts\Activate.ps1
```
3. Install dependancies: 
```python
python -m pip install -r requiements.txt
```
4. Run the file: 
```python
python main.py
```

## Run locally

1. Create and activate a virtual environment. (Not required but recommended.)
2. Install dependencies: 
```python
python -m pip install -r requirements.txt
```
3. Start the app: 
```python 
python main.py
```
4. The app automatically opens `http://127.0.0.1:5000` in your default browser.

The database is created as `price_checker.sqlite3` beside `main.py`. Set `PRICE_CHECKER_DB` to choose another location. Optional settings are `PRICE_CHECKER_MAX_URLS` (20-100), `PRICE_CHECKER_CACHE_HOURS`, `PRICE_CHECKER_MIN_DELAY`, `PRICE_CHECKER_LOCATION` (a ZIP code or city/state used for regional retailer results), and `PORT`. The daily scan limit resets at midnight according to the computer's local timezone; observation timestamps remain stored in UTC.

For local API troubleshooting, set `PRICE_CHECKER_DEVELOPER_MODE=1`. Developer mode disables the daily scan limit, shows Re-check for every product, and records each JSON API response in a separate `price_checker.dev.sqlite3` database. Set `PRICE_CHECKER_DEV_DB` to choose another developer database path. Leave developer mode unset or set it to `0` for normal behavior; regular users can only Re-check products whose previous request failed.

SerpApi requests wait up to 90 seconds by default. Set `SERPAPI_TIMEOUT_SECONDS` if your network is slower, for example `$env:SERPAPI_TIMEOUT_SECONDS = "120"`. The app does not automatically retry a timed-out request because SerpApi may already count it as a search.

When the browser tab unloads, the app sends a local shutdown signal and exits after a short grace period. This also applies to a refresh or navigation if no new app request arrives during that grace period.

### Supported Api's

#### SerpApi
**Free Monthly Plan**  
Create a SerpApi account and set your API key in environment variables. Or, for a quick setup, create a file named `.env` beside `main.py` with this content:

```dotenv
SERPAPI_API_KEY=your-key-here
UNWRANGLE_API_KEY=your-key-here

PRICE_CHECKER_API_PROVIDER=serpapi-or-unwrangle
```

#### Unwrangle
**Free Trial**  
Create a Unwrangle account and set you API key in evironment variables. Or, for a quick setup, create a file named `.env` beside `main.py` with this content:

```dotenv
SERPAPI_API_KEY=your-key-here
UNWRANGLE_API_KEY=your-key-here

PRICE_CHECKER_API_PROVIDER=serpapi-or-unwrangle
```

**Notice**  
Keep the `.env` file private do not share your personal API key. If you think your key has been leaked, quickly go to your dashboard and change it.

## Responsible use

Only check pages where automated access is permitted by the site's terms and applicable policies. Retailers can change their markup, rate-limit clients, or block automated traffic. Keep the list small, leave the delay enabled, and do not attempt to bypass CAPTCHAs, authentication, robots restrictions, or IP blocks. A browser context does not guarantee that a site will permit automation.

Price extraction through SerpApi uses structured product fields. Walmart and Home Depot are supported; Lowe's and generic sites are not supported by the current providers. Unwrangle does provide more retailer options, but they don't provide a free version.

## Features

### Upcoming

- Discontinued: Checks if the page or product is discontined, then alerts user.
- Local Database compair: If you have your own database of products, you can see which items you and walmart, lowes, home-depot share. Easy table export function for comparision. (If you don't map the items by something they share, you won't get any hits. E.G. UPC, Name, Product ID or SKU)
- Dev mode tools:
	- Raw API response viewer
	- Price extraction debugger
	- Replay saved API responses
	- Price sanity checks
	- Provider comparison
	- API request log
	- Fixture generator
	- Parser validation panel
	- API health dashboard
	- Dry-run mode

### Implemented

- Graph view: Shows a line graph of the product over time.
- Watchlist export: Download current products and saved observations as JSON or CSV.
- Local Flask interface for adding and removing URLs.
- SQLite-backed current prices, scan observations, errors, and daily scan state.
- SerpApi product lookups with configurable timeouts.
- Resilient parsing for retailer product and offer response shapes.
- Per-URL failures are recorded while the rest of the scan continues.
- Search the same item through multiple stores.
- Different themes for your enjoyment.
- Unwrangle API integration with more retailers.
- Ability to toggle between auto, SerpApi, and Unwrangle.
