# Price Checker

A local web app for maintaining a product watchlist and checking prices once per day. It uses SerpApi Walmart and Home Depot product lookups, SQLite history, conservative request pacing, and cached results.

**Written by:** AJ Utz  
**Written on:** 7/27/2026  
**Last Update on:** 9/23/2026  
**Latest Version:** 0.0.3

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
12. Creates a line graph for viewing an item's history.

## Setting up a Virtual Environment

Setting up a virtual environment using Windows Powershell. (Not required but recommended)
1. In the directory you put the program: 
```cmd
python -m venv .venv
```
2. Then: 
```cmd
.venv\Scripts\Activate.ps1
```
3. Install dependancies: 
```cmd
python -m pip install -r requiements.txt
```
4. Run the file: 
```cmd
python main.py
```

## Run on Linux

1. Open terminal:
```terminal
sudo apt update
sudo apt install python3 python3-venv python3-pip git
```
2. Download files, extract them, and move to the price-checker-main directory
3. Create a virtual environment, download depencies, and create an .env
```terminal
python3 -m venv .venv 
source .venv/bin/activate
```
```terminal
python -m pip install -r requiements.txt
```
```terminal
python main.py
```

## Run Locally

1. Create and activate a virtual environment. (Not required but recommended.)
2. Install dependencies: 
```cmd
python -m pip install -r requirements.txt
```
3. Start the app: 
```cmd 
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
PRICE_CHECKER_DEVELOPER_MODE='0'
PRICE_CHECKER_API_PROVIDER=serpapi-or-unwrangle
```

#### Unwrangle
**Free Trial**  
Create a Unwrangle account and set your API key in evironment variables. Or, for a quick setup, create a file named `.env` beside `main.py` with this content:

```dotenv
SERPAPI_API_KEY=your-key-here
UNWRANGLE_API_KEY=your-key-here
PRICE_CHECKER_DEVELOPER_MODE='0'
PRICE_CHECKER_API_PROVIDER=serpapi-or-unwrangle
```

**Notice**  
Keep the `.env` file private do not share your personal API key. If you think your key has been leaked, quickly go to your dashboard and change it.

## Uploading your database  
The price checker accepts user made databases from the following filetypes: .json, .csv, .sqlite3, .sqlite, and .db.  
**CSV upload**: The imported file requires a `url` and `history` column. If either is missing, it should reject the file.  
**JSON upload**: The JSON must be an object with a top-level `"products"` list. Each product row should still include a `url` value, because the app skips rows without one during import.  
**SQLite upload**: The file must be a valid SQLite database and contain a `products` table. There is no stricter per-column requirement check for the database upload itself.  

## Responsible use

Only check pages where automated access is permitted by the site's terms and applicable policies. Retailers can change their markup, rate-limit clients, or block automated traffic. Keep the list small, leave the delay enabled, and do not attempt to bypass CAPTCHAs, authentication, robots restrictions, or IP blocks. A browser context does not guarantee that a site will permit automation.

Price extraction through SerpApi uses structured product fields. Walmart and Home Depot are supported; Lowe's and generic sites are not supported by the current providers. Unwrangle does provide more retailer options, but they don't provide a free version.

## Features/Bugfixes

### Upcoming

- Saving the env file on the front end adds too many new lines. 
- Mapping 2 datasheets together.
- Bulk purchasing line in graph history.
- Persistance to all store numbers. 
- Combine similar items onto a single line graph, then export as an image.
- Search bar function to quickly show the requested item. (Instead of scrolling the entire database.)
- Integrate SerpApi's and Unwrangle's Amazon API into the program
- SerpApi has a bunch of other API's that don't fit with the current project. Perhaps we can make an offshoot of this program. (Social Media anylitics)
- Change the time zone from UTC to the machine's time zone
- Usage bar in the top right to tell the user how many more searches/credits they have left
- Discontinued: Checks if the page or product is discontined, then alerts user.
- Local Database compare: If you have your own database of products, you can see which items you and walmart, lowes, home-depot share. Easy table export function for comparision. (If you don't map the items by something they share, you won't get any hits. E.G. UPC, Name, Product ID or SKU)
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

- Add time interval customization for the bar graph.(User controlled)
- Export line graph as image
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
- Line Graph
- JSON and CSV data export
- Store specific search. Works with SerpApi, and a few of Unwrangle's API's
- .env file editor from front end
- Unwrangle API integration (Walmart, Home Depot, Lowes, Ace Hardware, Sams Club)
- Import/export the raw SQLite database file to switch between watchlists