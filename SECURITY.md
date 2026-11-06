## Security:  

### Local Mode
There shouldn't be any security issues if the program is run locally.  
The only big risk is if you share your API keys with others.(Something you should avoid.)  

### What LAN and Public mode have in common
Both modes require accounts, and both turn on the same protections:

- **Accounts:** Passwords are hashed with scrypt and are 10 to 256 characters long. Five failed logins lock an account for a customizable ammount of minutes (`PRICE_CHECKER_LOGIN_MAX_FAILURES`, `PRICE_CHECKER_LOGIN_LOCKOUT_MINUTES`), and each IP address is rate limited too. Sessions end after 120 minutes idle (`PRICE_CHECKER_SESSION_IDLE_MINUTES`), and "Keep me signed in" lasts 30 days (`PRICE_CHECKER_REMEMBER_DAYS=0` turns it off). Registration is admin-only by default (`PRICE_CHECKER_REGISTRATION`).
- **Roles and lists:** Regular users only see the shared lists an administrator assigned them. Other people's items and stores return 404, never "forbidden", so IDs can't be probed. Regular users can't add, remove or import items. Only administrators manage users, lists, API keys, the API provider and the developer tools.
- **Request protection:** Every POST needs a CSRF token and must come from this site. Sessions are reset at login. Cookies are `HttpOnly` and `SameSite=Lax`. A strict Content-Security-Policy, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and `Cache-Control: no-store` are sent on every page.
- **Uploads:** Imported SQLite files are opened read-only, and ownership columns in any file are ignored. Uploads are capped at 64 MB, and they merge into a list without replacing or deleting anything.
- **API cost limits:** Each user has a daily API call budget (`PRICE_CHECKER_USER_DAILY_API_CALLS`, default 150) and an hourly re-check limit (`PRICE_CHECKER_RECHECKS_PER_HOUR`, default 20). `PRICE_CHECKER_GLOBAL_DAILY_API_CALLS` sets an overall cap. Administrators' calls are counted but never refused.
- **Secrets:** The server won't start without a persistent secret of at least 32 characters. Create one with `python main.py init-secret`, or set `PRICE_CHECKER_SECRET`. It signs sessions, so keep it private and never commit it. Changing it signs everyone out.
- **API keys:** Keys live on the server in `.env` and are shared by every user. They are never sent to the browser, and the `.env` editor exists only in local mode. Keep `.env` out of git and out of backups you share.
- **Backups:** `python main.py backup` copies the database, which holds password hashes and price history. Treat backups like the live data.

### LAN Mode
LAN mode is for a trusted home or office network. It is not meant to be exposed to the internet.

- **Don't port-forward it.** Use Public mode if you want access from outside your network.
- **Encryption:** `PRICE_CHECKER_TLS` has three settings.
  - `off` (default): traffic is plain HTTP, so passwords and watchlists can be read by anyone else on the network. Every page shows a warning. Only use it on a network you trust.
  - `selfsigned`: a certificate is created in the data directory on first start. Traffic is encrypted, but browsers show a warning you have to accept, and a self-signed certificate can't prove the server is who it says it is. Only accept the warning when you reached the address on purpose.
  - `custom`: use your own certificate with `PRICE_CHECKER_TLS_CERT` and `PRICE_CHECKER_TLS_KEY`. The app won't start if the files are missing. Keep the key file private.
  - `Secure` cookies and HSTS are only enabled when the app itself serves TLS. TLS needs `PRICE_CHECKER_SERVER=werkzeug` or `gunicorn`, because Waitress can't serve it.
- **Allowed hosts:** Set `PRICE_CHECKER_ALLOWED_HOSTS` to the server's LAN IP and/or hostname (for example `192.168.1.50,pricechecker.lan`, no scheme or port). Requests with any other `Host` header are refused. If it is blank, nothing is restricted.
- **Firewall:** Allow the app's port only from your LAN, and give the server a fixed IP address so the allowed host doesn't change.
- **Who is on the network:** Anyone on the same network can reach the login page. The account lockout and rate limits slow down guessing, but use strong passwords and keep guests on a separate Wi-Fi network.

### Public Mode
Public mode is for internet hosting. It always runs **behind a reverse proxy that serves HTTPS** (the included Docker setup uses Caddy). The app refuses to start if it is set up unsafely (hopefully, I haven't tried it yet).

- **HTTPS is required.** The proxy terminates TLS and the app trusts it. Cookies are `Secure` and use the `__Host-` prefix, so browsers refuse to send them over plain HTTP. HSTS is sent for a year. If a page arrives without HTTPS, a warning is shown and sign-in is unsafe until the proxy forwards `X-Forwarded-Proto`.
- **Trusted proxies:** `PRICE_CHECKER_TRUSTED_PROXIES` must be at least 1 (the default in public mode). Without it every visitor looks like the proxy, and per-IP rate limits and lockouts stop working. Set it to the real number of proxies in front of the app, and no higher, so visitors can't fake their IP with `X-Forwarded-For`.
- **Allowed hosts:** `PRICE_CHECKER_ALLOWED_HOSTS` is required. Set it to your domain (for example `prices.example.com`) so requests for any other host name are refused.
- **Only the proxy should be reachable.** Don't publish the app's own port. In Docker, only Caddy's ports 80 and 443 should be open to the internet.
- **Registration:** Keep `PRICE_CHECKER_REGISTRATION=admin`. `invite` uses single-use links with an expiry, and each link is shown only once. Avoid `open`, because anyone on the internet could then create accounts that spend your API credits.
- **Spend limits matter more here.** Keep the per-user and global API limits set so a stolen or abused account can't use up your paid API credits.
- **Keep it updated:** Update the host, Docker images and Python dependencies regularly. Back up the data directory (`python main.py backup`, or the included systemd timer) and keep the backups off the server.
- **Secrets:** Keep `.env` and the `secret_key` file out of the repository. Both are gitignored, and the real values must never be committed or put in CI logs.

### Reporting a problem
If you find a security issue, please report it privately to me or opening a public issue.
