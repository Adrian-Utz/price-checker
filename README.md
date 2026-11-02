# Price Checker

A Flask app for keeping a product watchlist and checking prices once a day. It uses SerpApi, Unwrangle and Apify product lookups, stores history in SQLite, and paces its requests.

It runs three ways:

| Mode | For | Accounts | Server |
|---|---|---|---|
| **local** (default) | Just you, on your own computer | None | Flask / werkzeug |
| **lan** | Your household or office network, over HTTP or HTTPS | Yes | waitress, or werkzeug for TLS |
| **public** | The internet, behind an HTTPS reverse proxy (Docker + Caddy) | Yes | gunicorn |

**Written by:** AJ Utz  
**Written on:** 7/27/2026  
**Last Update on:** 11/2/2026  
**Latest Version:** 0.1.0

## Main Capabilities

1. Track up to 20-100 URLs per user. (Customizable)
2. API lookup:
	- Walmart and Home Depot through SerpApi.
	- Walmart, Home Depot, Lowes, Ace Hardware and Sam's Club through Unwrangle. Lowes store-specific lookups need the store number, ZIP code and state together.
	- Lowes through Apify's Lowes Product Lookup actor (by product ID and ZIP code).
3. Keep current results and price history in SQLite, with a line graph per item.
4. One completed scan per day per user, with randomized request delays and a 24-hour result cache.
5. Export your watchlist and history as JSON or CSV, and import a saved watchlist into your own account (it merges, nothing is replaced).
6. Multiple user accounts in LAN and public modes, with an admin page, invites, lockout and rate limiting.
7. Per-user daily API budgets and hourly re-check limits so one user can't drain a paid API.
8. Developer tools for debugging API responses (local developer mode, or any administrator in LAN and public modes).

## Quick start (local mode)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

On Linux or macOS use `python3 -m venv .venv`, `source .venv/bin/activate`, then the same `pip` and `python main.py` commands. On Debian/Ubuntu you may first need `sudo apt install python3 python3-venv python3-pip`.

The app opens `http://127.0.0.1:5000` in your browser. In local mode there are no accounts, and the server stops shortly after you close the tab.

Your data lives in `price_checker.sqlite3` in the `utils` folder. Set `PRICE_CHECKER_DB` or `PRICE_CHECKER_DATA_DIR` to put it elsewhere. The daily scan limit resets at midnight on the computer's clock, while observation timestamps are stored in UTC.

Optional settings: `PRICE_CHECKER_MAX_URLS` (20-100), `PRICE_CHECKER_CACHE_HOURS`, `PRICE_CHECKER_MIN_DELAY`, `PRICE_CHECKER_LOCATION` (a ZIP or city/state for regional results) and `PRICE_CHECKER_PORT` (or `PORT`).

## LAN mode

For a home or office network. Create the secret and the first admin, then start:

```powershell
$env:PRICE_CHECKER_MODE = "lan"
python main.py init-secret
python main.py create-admin yourname
python main.py
```

LAN and public modes refuse to start without a persistent secret and an admin account. The first admin you create adopts any watchlist you already had.

- It listens on `0.0.0.0:5000` and prints the URL(s) other devices should use.
- HTTP is the default. Set `PRICE_CHECKER_TLS=selfsigned` for HTTPS with an automatically generated certificate (browsers will warn once), or `PRICE_CHECKER_TLS=custom` with `PRICE_CHECKER_TLS_CERT` and `PRICE_CHECKER_TLS_KEY`. Plain HTTP sends passwords unencrypted, so the app shows a warning banner.
- `python main.py make-cert` regenerates the self-signed certificate, for example after your LAN IP changes.

## Public mode (Docker)

For the internet. Docker Compose runs the app behind Caddy, which gets HTTPS certificates automatically. Create the admin **before** the first start:

```bash
cp .env.example .env          # set DOMAIN and your API keys
docker compose run --rm app python main.py create-admin yourname
docker compose up -d
docker compose ps             # the app should show "healthy"
```

There is also a systemd unit for running without Docker. See [docs/deployment.md](docs/deployment.md) for the full guide: systemd, the ufw firewall, router port forwarding, Cloudflare Tunnel, Tailscale, and backing up the SQLite database.

> The Docker, Caddy, Cloudflare Tunnel and Tailscale setups have only been checked by automated tests and CI, not on a live server. Check `docker compose ps` and your first login after deploying.

## Accounts and administration

- Sign in at `/login`. Sessions end after 120 minutes idle, and "Keep me signed in" lasts 30 days (`PRICE_CHECKER_REMEMBER_DAYS=0` turns it off).
- Administrators open **Users** to create accounts, disable them, reset passwords, change roles and make invite links.
- `PRICE_CHECKER_REGISTRATION` is `admin` (default, no sign-up page), `invite` (single-use links) or `open`.
- Passwords are 10 to 256 characters with no complexity rules. Five failed logins lock an account for 15 minutes, and each address is rate limited.
- Command line: `python main.py create-admin [name]`, `python main.py reset-password [name]`, `python main.py backup [keep]`. In Docker, use `docker compose exec app python main.py ...`. For scripts, set `PRICE_CHECKER_ADMIN_PASSWORD` instead of typing the password.
- API keys and the provider are server-wide and admin-managed. Regular users see only their own limits.

Other hosted-mode settings: `PRICE_CHECKER_ALLOWED_HOSTS` (required in public mode), `PRICE_CHECKER_TRUSTED_PROXIES`, `PRICE_CHECKER_USER_DAILY_API_CALLS` (default 150), `PRICE_CHECKER_GLOBAL_DAILY_API_CALLS`, `PRICE_CHECKER_RECHECKS_PER_HOUR` (default 20), `PRICE_CHECKER_THREADS`, and `PRICE_CHECKER_SCHEDULED_SCAN_HOUR`. See [.env.example](.env.example) and [docs/deployment.md](docs/deployment.md). Shutdown-on-tab-close, browser auto-open, `/shutdown` and the `.env` editor exist only in local mode. Administrators get the developer tools in every mode (see below).

## API providers

The buttons at the top of the page set the global provider (admins only in hosted modes). To pin one item to a provider, click its retailer button in the watchlist and choose **API for this item**. Only providers that support that retailer are offered.

Set your keys as environment variables, or in a `.env` file beside `main.py` (in hosted modes, `DATA_DIR/.env`):

```dotenv
SERPAPI_API_KEY=your-key-here
UNWRANGLE_API_KEY=your-key-here
APIFY_API_TOKEN=your-token-here
PRICE_CHECKER_API_PROVIDER=auto
```

`PRICE_CHECKER_API_PROVIDER` is `auto`, `serpapi`, `unwrangle` or `apify`. In `auto`, SerpApi handles retailers it supports, Apify handles Lowes when a token is set, and Unwrangle handles the rest.

**SerpApi** has a free monthly plan. Requests wait up to 90 seconds; raise `SERPAPI_TIMEOUT_SECONDS` on a slow network. Timed-out requests are not retried automatically because SerpApi may already have counted them.

**Unwrangle** has a free trial of 100 credits. (Not renuable)

**Apify** is pay-per-product (about $5 per 1,000 lookups; free accounts are limited). Copy your token from the Apify Console (Settings > API & Integrations). Optional settings:

```dotenv
APIFY_LOWES_ZIP=22902
APIFY_TIMEOUT_SECONDS=180
```

The actor looks products up by product ID (from the Lowes URL) and a 5-digit ZIP, which comes from the saved store or `APIFY_LOWES_ZIP`. When Lowes hides the single-unit price, the returned price is shown as a bulk price.

**Keep your keys private.** Never share the `.env` file. If a key leaks, change it on the provider's dashboard right away.

## Developer mode

In local mode, set `PRICE_CHECKER_DEVELOPER_MODE=1`. In LAN and public modes, every administrator always has it. It disables the daily scan limit, shows Re-check on every product, and saves each raw API response to `price_checker.dev.sqlite3` (`PRICE_CHECKER_DEV_DB` changes the path; the newest 1000 are kept in hosted modes). The **API response lab** lets you browse the latest 100 responses and replay one offline. In hosted modes administrators also skip the per-user API budget and re-check limit (their calls are still counted), non-admins get a 404 for the lab, and the lab shows every user's product URLs. Scheduled scans are not recorded.

## Importing and exporting

Export from the page as JSON or CSV. Import accepts `.json`, `.csv` and `.sqlite3`, and merges into **your** watchlist: products you already track only gain missing price history, new products are added, and the per-user limit still applies.

- **CSV** needs at least a `url` column.
- **JSON** must be an object with a top-level `"products"` list; each product needs a `url`.
- **SQLite** files must hold a `products` table and are opened read-only.
- A file may hold at most 1,000 products and 20,000 price records per product.

Exporting the raw SQLite file is only available in local mode, because that file holds every account's data.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

See [tests/README.md](tests/README.md) for running single files or tests. CI (`.github/workflows/ci.yml`) runs the suite and a Docker build smoke test.

## Files

```
price checker
├── .github/workflows/ci.yml   - Tests and Docker smoke test.
├── api_clients/               - SerpApi, Unwrangle and Apify clients.
├── deploy/                    - systemd service and backup timer.
├── docker/entrypoint.sh       - Creates a secret on first start in Docker.
├── docs/deployment.md         - Docker, systemd, firewall, tunnels, backups.
├── resources/                 - Images for this readme.
├── static/stylesheet.css      - Style for the HTML.
├── templates/                 - Pages: watchlist, login, account, admin, dev lab, shared banner and quota panel.
├── tests/                     - Test suite (see tests/README.md).
├── utils/
│	├── auth.py                - Sessions, lockout, rate limits, invites.
│	├── charting.py            - Chart creation.
│	├── check_for_update.py    - Checks the repo for the latest version.
│	├── config.py              - Mode, host, TLS and feature settings.
│	├── database.py            - Connections, WAL mode, schema setup, backups.
│	├── limits.py              - Per-user and server-wide API budgets.
│	├── migrations.py          - Versioned database migrations.
│	├── providers.py           - Provider selection and API-specific behavior.
│	├── scanner.py             - Scan workflow.
│	├── serving.py             - Server choice, self-signed certificates, scheduler.
│	├── users.py               - User accounts.
│	└── watchlist_io.py        - Import and export.
├── .env.example               - Settings template for Docker.
├── Caddyfile                  - Reverse proxy with automatic HTTPS.
├── changlog.md                - A list of changes to the program.
├── docker-compose.yml         - App plus Caddy.
├── Dockerfile                 - Non-root image with a healthcheck.
├── gunicorn.conf.py           - Production server settings (one worker, threads).
├── main.py                    - Entrance file, routes and CLI commands.
├── requirements.txt           - Requirements to run the price checker.
├── version.py                 - The version number.
├── wsgi.py                    - Production entrypoint.
├── CODE_OF_CONDUCT.md, CONTRIBUTING.md, LICENSE, SECURITY.md
```

## GitDiagram
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="resources/dark_diagram.webp">
  <source media="(prefers-color-scheme: light)" srcset="resources/light_diagram.webp">
  <img alt="Architecture diagram of adrian-utz/price-checker" src="resources/light_diagram.webp">
</picture>

## Responsible use

Only check pages where automated access is permitted by the site's terms and applicable policies. Retailers can change their markup, rate-limit clients, or block automated traffic. Keep the list small, leave the delay enabled, and do not attempt to bypass CAPTCHAs, authentication, robots restrictions, or IP blocks. A browser context does not guarantee that a site will permit automation.

## Features / Bugfixes

### Upcoming / Noted

- Mapping 2 datasheets together.
- Persistence for all store numbers.
- Combine similar items onto a single line graph, then export as an image.
- Search bar to quickly find an item instead of scrolling the whole list.
- Integrate SerpApi's and Unwrangle's Amazon API.
- SerpApi has many other APIs that don't fit this project. Perhaps an offshoot program (social media analytics).
- Change the time zone from UTC to the machine's time zone.
- Local database compare: see which items you and another list share (Walmart, Lowes, Home Depot), with an easy table export. Items must share something such as a UPC, name, product ID or SKU.
- Password resets by email or security questions, and a forced password change after an admin reset.
- Dev mode tools: fixture generator, parser validation panel, API health dashboard, dry-run mode.

### Implemented / Fixed

- Multi-user LAN and public modes: accounts, admin page, invites, lockout, rate limits, per-user data, budgets and quotas.
- LAN TLS (self-signed or custom), production server selection, Docker, Caddy and systemd deployment, backups.
- Versioned database migrations and WAL mode for concurrent users.
- Security hardening: CSP, Host allowlist, CSRF on every POST, secure cookies, proxy support.
- Per-user import and export that merges instead of replacing.
- Dev mode tools: raw API response viewer, price extraction debugger, replay of saved responses, API request log.
- Usage bar showing remaining searches/credits.
- Bulk purchasing line in graph history, and `bulk_price`/`bulk_quantity` CSV columns.
- Line graph per product with user-controlled time interval, and export as an image.
- Watchlist export as JSON or CSV, including the retailer product ID when it can be extracted.
- Flask interface for adding and removing URLs, with SQLite-backed prices, scan observations, errors and daily scan state.
- SerpApi, Unwrangle (Walmart, Home Depot, Lowes, Ace Hardware, Sam's Club) and Apify (Lowes) integrations, with auto/manual provider choice and per-item pinning.
- Resilient parsing for retailer product and offer response shapes. Per-URL failures are recorded and the scan continues.
- Search the same item through multiple stores, and store-specific search.
- Themes, a `.env` editor (local mode), togglable local shutdown, and page scroll persistence.
- On Python 3.15 and newer, selected imports are loaded only when first used.
