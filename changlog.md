# v0.0.1beta
- Started with a local Flask price-checker app, but then I moved retailer lookups from fragile direct page scraping to [SerpApi](https://serpapi.com/), with focused Walmart and Home Depot product support. Added SQLite persistence for tracked URLs, observations, errors, and scan state. Added basic JSON-LD/visible-price extraction, caching, request delays, and the one-scan-per-day rule. Added `.env` loading and an in-app editor for configuring `SERPAPI_API_KEY`, request timeouts, browser startup, port, and other runtime settings. Hardened SerpApi response parsing to handle product, product-result, search-result, nested offer, and nested price/value response shapes. Added local SQLite price history, including successful observations, failed checks, timestamps, and a daily scan limit that resets at local midnight. Added a line graph with interactive price/date tooltips and a readable list of all past prices in the history modal. Added JSON and CSV watchlist exports containing current product details and saved observation history. Added automatic browser startup with explicit local HTTP handling and configurable URL scheme support. Added a local tab-unload shutdown signal with a short grace period so the app can exit after the browser session ends. Added [CSRF](https://en.wikipedia.org/wiki/Cross-site_request_forgery) protection for form submissions, persistent configuration support, version display, and regression tests covering parsing, routes, history, exports, and shutdown behavior. Removed the HTML from the main file into its own seperate HTML file.

# [v0.0.2beta](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.2beta)
- Finished Documentation. Changed the fixed CSS themes to Variables. Easier to make Themes. Added 3 themes. Warm, Ocean, and Midnight. Set `table-layout: fixed` so it stays in its container. Added column widths for product, retailer, price, date, and actions. Allowed long URLs and timestamps to wrap with `overlow-wrap: anywhere`. Removed forced single line URL behavior. Added the update checker. It checks one time when the app is launched. Added `requests` to the list of requirements.

# [v0.0.1](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.1)
- No longer a beta release. Fully functional Price Checker. Added the Forest theme. At this point there are 2 light and 2 dark themes. Added the location search. Allowed you to track the same item from different stores. Just put in the URL and click the "Track another store" box. This will allow you to bypass the barrier that checks for duplicates temporarily. Fixed some bugs where the store name wasn't appearing after the scan was run, resulting in it being "location pending" forever. Added [Unwrangle](https://www.unwrangle.com/) to the program. It has more retailer API's available than SerpApi with the downside of only getting 100 free credits instead of 250 each month. Added an API selector at the top to set your prefered API. `Auto` defaults to SerpApi's engines when it is supported (E.G. home-depot, or walmart), and uses Unwrangle for the rest. Allowed users to import their own `.csv` and `.json` files. The program will automatically change them into a fresh SQLite database, following the same backup and replace logic. Added Dev Mode. Dev Mode allows you to bypass the Daily Scan limit, sets the re-check button to always be visible, returns every JSON responce and saves it to a seperate database (`price_checker.dev.sqlite3`), the raw responces are stored in the `api_responces` table. More Dev tools are being made for the future.

# [v0.0.2](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.2)
- Changed the line graph a bit. Prices now run down the left axis, and are snapped to $0.50 intervals at first. Once you have a change in your item's price, the price interval on the chart changes accordingly. Dates are displayed beneath the chart. Added a download button so you can download the chart as an image (png). You will initially see the 12 most recent dots. When more history exists, left/right arrow buttons appear below the graph. If you want to get a photo of all chart history select the box next to the `Download PNG` button. If you just want the current view don't select it.

# [v0.0.3](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.3)
- Fix an error by checking if `history` is a list before iterating it, and that each history entry is a dictionary before accessing observation fields. Put `history-list` into a drop-down element. Added 2 new themes: royal purple and neon. Added user controlled time interval manipulation. Allowing you to set the desired time for the line graph. 

# [v0.0.4](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.4)
- Fixed parts of the chart's popup so it now accepts theme colors. This is mostly just a reupload to allow me to use git.

# [v0.0.5](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.5)
- Moved some of the chart creation logic into it's own file to help with redability. Fixed a Windows specific issue where textarea submissions use [CRLF](https://developer.mozilla.org/en-US/docs/Glossary/CRLF) line endings, and writing them unchanged caused `write_text()` to add another carriage return. This created blank lines between `.env` entries on each save. Added usage bars for both API providers. Added more to the dev mode. Browse the latest 100 saved API responses and inspect their raw JSON. Replay a saved responce offline, without another API request. See the extracted product fields, raw values, parsed values, and JSON paths such as `$.detail.price`. all of this can be accessed. Added `__lazy_modules__` for imports used only after app initialization. Python 3.15+ can defer them until first use, while older version continue loading them normally.

# [v0.0.6](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.6)
- Added a page fade-in to the `stylesheet.css`. Theme switches on the HTML pages now use `document.startViewTransition`. A crossfade between two stapshots, one of the old page into one of the new. Fade is disabled for reduced motion users. Browsers without view transitions fall back to a per-element fade.

# [v0.0.7](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.7)
- Added a new API client, [Apify](https://apify.com/about)! It currently supports Lowes through the [Lowes Product Lookup](https://apify.com/maplerope44/lowes-product-lookup) actor, which looks products up by product ID and ZIP code instead of a link. The product ID is pulled from the Lowes URL, and the ZIP comes from the saved store's ZIP or `APIFY_LOWES_ZIP`. If a store ID is saved it uses that store, otherwise the nearest store. Set `APIFY_API_TOKEN` in your `.env` (and optionally `APIFY_TIMEOUT_SECONDS`) to use it. Added an Apify button to the API selector and an Apify ready/token-needed badge. `Auto` now sends Lowes to Apify when a token is set, and falls back to Unwrangle otherwise. When Lowes hides the single-unit price, the app keeps the price the actor returns and shows it as a bulk price with a "Single-unit price hidden" label instead of discarding it. Added `bulk_price` and `bulk_quantity` columns to the CSV export, and empty bulk cells now import as blank values.

# [v0.0.8](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.8)
- Added a checkbox column to the watchlist. Allows you to select what you want to recheck in your list. Moved the watchlist import/export helpers to `watchlist_io.py` and updated main.py to import and use them. `source_for` was also moved there since database-building uses it. Adjusted export routes to pass in their active database connections. Moved the connection management and schema setup to `database.py`.  Also moved the scan workflow functions to `scanner.py` and made those dependancies explicit since they depend on the database and product fetching code. Moved the provider selection and API-specific behavior together. Environment access, URL parsing, and the SerpApi, Unwrangle, and Apify clients readiness helpers now live in the `providers.py` file. Added saved store removal to the store selection dilogue. Each store now has a remove button with a confirmation prompt. If upgrading from a older version, move your `.sqlite3` files to the utils folder. 

# [v0.0.9](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.9)
- Added a `group_testing` variable. Set it to `True` and the local server doesn't shutdown every time the webpage is closed. Set it to `False` for the original behavior. Fixed the PNG export: the price line relied on a CSS variable that isn’t available when the SVG is rasterized as an image. It now has a fallback color, while the page’s theme color still takes precedence.

# [v0.0.10](https://github.com/Adrian-Utz/price-checker/releases/tag/0.0.10)
- Added some more variables in `main.py` to help with [Live Sharing](https://marketplace.visualstudio.com/items?itemName=MS-vsliveshare.vsliveshare). Added the ability to map Api Providers to products. Allowing the user to map out their entire watch list. Click an item's retailer button and use the new `API for this item` drop down. the row shows an `API <name>` label when an item is pinned. Unpinned items use the global provider at the top of the page. Added a scroll persistence script in `page.html` it will save your position every time you reload the page.

# [v0.1.0](https://github.com/Adrian-Utz/price-checker/releases/tag/0.1.0)
- Perhaps the greatest undertaking of this program. Adding the ability to run the program on a LAN server or on the internet. For this part I seperated it into 10 development phases. Phases 1,2,3 and 5 give a working multi-user LAN mode over HTTP. Phase 4 and 6 are about user input, serving and LAN TLS. Phase 6 also sets up the production server, and phase 7 is all about public mode. Phases 8, 9 and 10 cover UI, tests, and documentation.  

### Phase 1:

**Config**

* utils/config.py reads PRICE_CHECKER_MODE (local, lan or public) and derives host, port, TLS, data directory, auth requirement, shutdown behavior and feature flags.
* Local mode always binds 127.0.0.1
* PRICE_CHECKER_TLS is off, selfsigned or custom, and is only honored in LAN mode. It uses PRICE_CHECKER_TLS_CERT and PRICE_CHECKER_TLS_KEY.
* custom TLS is wired into the server and fails at startup if the files are missing.
* selfsigned is accepted but refuses to start for now; it's planned for Phase 6.
* PRICE_CHECKER_DATA_DIR sets where the database, secret_key file and certs live. Without it, the database stays where it was before (utils/), so the existing data is untouched.
* LAN and public modes turn off tab-close shutdown, browser auto-open, the .env editor and dev tools.
* Public mode adds ProxyFix, with PRICE_CHECKER_TRUSTED_PROXIES defaulting to 1. The session cookie is now HttpOnly, SameSite=Lax, and Secure when HTTPS applies.

**Migrations**

* utils/migrations.py tracks the schema version with PRAGMA user_version. You add a migration by appending to the MIGRATIONS list.
* Version 1 is the old init_db logic, moved over as-is, and version 2 adds a users table.
* Each step runs in its own transaction.
* An existing database is backed up to a backups/ folder next to it before it changes.
* A database from a newer version is refused.
* init_db() in utils/database.py now calls migrate, and there is an admin_exists() helper.

**Startup checks**

* LAN and public modes refuse to start without a persistent secret of at least 32 characters, or without an enabled admin account.
* They also refuse a selfsigned or incomplete custom TLS setup.
* python main.py init-secret creates the secret file and never overwrites an existing one.
* The checks run both from python main.py and when a production server imports main:app.
* Confirmed LAN mode exits with a clear message when no admin exists. Because of that check, LAN and public modes can't start until Phase 3 adds create-admin.

### Phase 2:

**What changed**

* Accounts: a users table (scrypt hashes, role, disabled flag, lockout fields), with helpers in utils/users.py.
* Ownership: products, stores and scans carry a user_id. Stores are unique per user, so store numbers are unique per user.
* Existing data: migration 3 assigns all existing rows to a placeholder local user. Local mode uses that user implicitly.
* Scoping: every query in main.py, the scanner and the export code is scoped by user.
* 404s: product and store routes now return 404 for rows you don't own or that don't exist. That replaces the old redirect with a message for missing rows.
* Limits and scanning: MAX_URLS is per user, and each user has their own scan lock.
* Admin-only settings: API keys, provider and usage counters are server-wide. Only admins see or change them.

### Phase 3:

**What was added**

* Login and sessions: each login starts a fresh session with a new CSRF token. By default a session ends after 120 minutes of inactivity. "Keep me signed in" lasts 30 days and can be turned off with PRICE_CHECKER_REMEMBER_DAYS=0.
* Sign-out everywhere: changing or resetting a password, or disabling an account, signs that user out on every device. The person changing their own password stays signed in.
* Lockout and rate limiting: 5 failed logins lock an account for 15 minutes, and an address is limited to 20 failed logins per 15 minutes. Both limits are configurable. Failures all show the same message, so it doesn't reveal whether an account exists or is locked.
* 429 and Retry-After: an address over its limit gets a 429. The Retry-After header is on by default and can be turned off with PRICE_CHECKER_RETRY_AFTER=0.
* Registration: PRICE_CHECKER_REGISTRATION is admin (default, no sign-up page), invite (single-use links, hashed in the database, expiring) or open (limited to 10 attempts per address per hour).
* Admin page (/admin/users): create users, change roles, disable or enable, reset passwords, and create or revoke invites. An admin can't change their own role or status.
* Account page: users can change their own password, which requires the current one. (subject to change. Might do email password resets, security questions, or both.)
* Password policy: 10 to 256 characters, and the password can't equal the username. There are no complexity rules for now. (Subject to change.)
* CLI: python main.py create-admin [username] and python main.py reset-password [username]. Passwords are prompted without echo, or taken from PRICE_CHECKER_ADMIN_PASSWORD for scripted docker exec use. The first admin created adopts the existing watchlist.
* Schema: migration 4 (schema version now 4) adds the session-invalidation counter, last-login time, an invites table and a login-attempts table.
* Login required: outside local mode every page except sign-in and registration redirects to /login. Local mode is unchanged.
* Test change: the old 401 test now expects a redirect to the login page.

### Phase 4:

**What was changed**

Export

* JSON and CSV export were already limited to the current user in Phase 2, and they still are.
* The raw SQLite export now works only in local mode, because the file holds every user's data and password hashes. It returns 403 in LAN and public mode.

Import

* The old import that replaced the whole database is gone, and /import/db now returns 404. The new POST /import is available in every mode, including local, and accepts .json, .csv or .sqlite3.
* A product you already track (same URL and store) keeps its row and only gains price history it doesn't have, matched by timestamp. New products are added, and nothing is replaced or deleted. Re-importing the same file adds nothing twice.
* The per-user MAX_URLS limit applies. Products over the limit are skipped and counted in the result message.
* Every row is written with your user id, so ownership fields in the file are ignored.
* Uploaded .sqlite3 files are opened read-only, and only known columns are read. Older files without newer columns still import.
* Rows with invalid URLs or timestamps are skipped, and numeric strings (such as from CSV) are cleaned up. A file can hold at most 1,000 products and 20,000 price records per product.
* The page's import dialog is now "Import watchlist" and shows for everyone, with text explaining the merge.
* Because the import goes through MAX_URLS, imported CSV prices are stored as real numbers now. Before, they were stored as strings.

### Phase 5:

**What Was Added**

Lifecycle
* `/shutdown` now returns a 404 outside of local mode,  and the tab-close beacon script is only emitted in local mode.
* The browser auto-open is skippid outside of local mode
* `/settings/env` and the dev-responce routes are blocked outside local mode.(Might change this later to a assignable permission.)

Hardening
* Host allowlist: set it with `PRICE_CHECKER_ALLOWED_HOSTS`. Local mode defaults to the loopback names, which also blocks [DNS rebinding](https://en.wikipedia.org/wiki/DNS_rebinding). LAN only checks if you set it. Public mode refuses to start without it.
* Proxy hops: public mode now also refuses to start with `PRICE_CHECKER_TRUSTED_PROXIES=0`, because every visitor would look like a proxy.
* CSRF: every POST needs the form token or an `X-CSRF-token` header. It also rejects cross-site requests, using Origin and Sec-Fetch-Site. `/shutdown` is exempt from the token but still rejects cross-site requests. A new `CSRF_ENABLED` config flag replaces the old `app.testing` skip, and the test conftest turns it off.
* Headers:
    * CSP with a per-request nonce, plus `nosniff`, `X-Frame-Options`, `Referrer-Policy: same-origin` and a Permissions-Policy.
    * HSTS only over HTTPS with secure cookies.
    * `Cache-Control: no-store` outside local mode.
* Cookies: the session cookie is __Host-session when cookies are secure.
* Templates: inline scripts now carry the nonce, and the inline handlers were replaced with data- attributes and one delegated listener.

Budgets and rate limits
* Defaults outside local mode are 150 API calls per user per day and 20 re-check requests per user per hour. Local mode is unlimited.
* Both are configurable (`PRICE_CHECKER_USER_DAILY_API_CALLS`, `PRICE_CHECKER_GLOBAL_DAILY_API_CALLS` for a server-wide cap, `PRICE_CHECKER_RECHECKS_PER_HOUR`). 0 means unlimited.
* A credit is reserved before each fetch and counts even if the fetch fails.
* A scan that hits the limit stops and is marked `stopped`, so it doesn't use up that day's one completed scan.
* `/recheck-selected` stops at the limit, and `/recheck` reports it.
The home page shows "N of M calls used" when a limit is set.

### Phase 6:

**What was Added**

Serving Choice
* Local: werkzeug is now threaded.
* LAN: waitress or werkzeug when `PRICE_CHECKER_TLS` is on, waitress can't do TLS.
* Public: gunicorn, with one worke process and `PRICE_CHECKER_THREADS` threads (default to 8). This keeps the SQLite writer, per-user scan locks and scheduler in one process.
* `python wsgi.py` and `python main.py` both pick the server this way. If you use Docker `gunicorn -c gunicorn.conf.py wsgi:app`

TLS and startup
* `PRICE_CHECKER_TLS=selfsigned` creates a certificate and key in `DATA_DIR/certs` on the first start and reuses them.
* The certificate covers localhost, the hostname, the detected LAN IPs and any names in `PRICE_CHECKER_ALLOWED_HOSTS`.
* `python main.py make-cert` regenerates it, for example: after the LAN IP changes.
*  Custom certificates are never touched.
*  Staartup prints the LAN URLs, one per detected address, and notes the self-signed certificate.

Optional daily scan
* Set `PRICE_CHECKER_SCHEDULED_SCAN_HOUR` to a UTC hour (0-23) to scan every enabled account once a day. (Off by default)
* Each user still goes through the once-a-day limit, their per-user lock and their API budget.

Also
* Added a public `/healthz` endpoint that checks the database. It skips the host allowlist so Docker healthchecks work.
* Added `waitress`, `cryptography`, and `gunicorn`(not on Windows) in the requirements.

Things to know
* WAL doesn't work well on network filesystems, so keep `DATA_DIR` on a local disk or a Docker volume.
* `/healthz` sets a session cookie which is harmless.