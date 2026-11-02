# Deploying Price Checker

Price Checker has three modes (`PRICE_CHECKER_MODE`):

| Mode | For | Accounts | Server | HTTPS |
|------|-----|----------|--------|-------|
| `local` | one person, this computer | none | werkzeug | no |
| `lan` | a home/office network | yes | waitress (werkzeug when TLS is on) | optional (`PRICE_CHECKER_TLS=off/selfsigned/custom`) |
| `public` | the internet | yes | gunicorn behind a reverse proxy | by the proxy |

`lan` and `public` refuse to start until a secret exists and an admin account has been created.

---

## 1. Public server with Docker (recommended)

Needs a Linux host with Docker, a domain name whose DNS `A`/`AAAA` record points at the host, and ports 80 and 443 reachable from the internet (Caddy needs 80 to get the certificate).

```bash
cp .env.example .env
nano .env                  # set DOMAIN and your API key(s)

docker compose build
# Create the first admin BEFORE starting: the app refuses to start without one.
docker compose run --rm app python main.py create-admin yourname
docker compose up -d
```

Then open `https://your-domain`. Caddy obtains and renews the certificate automatically.

- **More admins / password resets**
  - `docker compose exec app python main.py create-admin <name>`
  - `docker compose exec app python main.py reset-password <name>`
- **Logs:** `docker compose logs -f app`
- **Health:** `docker compose ps` (the app reports `healthy` once `/healthz` answers).
- **Update:** `git pull && docker compose build && docker compose up -d`. A database backup is written to `/data/backups` automatically before any schema migration.
- **Data:** everything lives in the `price_data` volume (mounted at `/data`): the database, the session secret (`secret_key`), backups, and the provider choice. Deleting this volume deletes all accounts and watchlists. Do not delete `secret_key` unless you want everyone signed out.
- **Bind mount instead of a named volume:** the container runs as UID 10001, so run `sudo chown 10001:10001 ./data` first.
- **API keys** come from `.env`. In public mode the `.env` editor is disabled; admins can still pick the provider on the home page (stored in `/data/.env`).
- `PRICE_CHECKER_SERVER=auto` picks gunicorn for public mode. Don't force waitress there: it discards the proxy's `X-Forwarded-*` headers, and the app refuses to start that way.
- **Do not publish port 5000.** Only Caddy should reach the app: it supplies the client address the login rate limits depend on, and the app trusts exactly one proxy hop (`PRICE_CHECKER_TRUSTED_PROXIES=1`).

### Limits and the scheduler

`PRICE_CHECKER_USER_DAILY_API_CALLS` (default 150) and `PRICE_CHECKER_GLOBAL_DAILY_API_CALLS` (default unlimited) cap paid API use. `PRICE_CHECKER_SCHEDULED_SCAN_HOUR=4` scans every account daily at 04:00 UTC. The image runs one gunicorn worker with several threads on purpose, so the scheduler and scan locks work. Don't raise the worker count.

---

## 2. Public server without Docker (systemd)

```bash
sudo useradd --system --home /opt/price-checker --shell /usr/sbin/nologin pricechecker
sudo git clone <repo-url> /opt/price-checker
cd /opt/price-checker
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chown -R pricechecker: /opt/price-checker

sudo cp .env.example /etc/price-checker.env
sudo nano /etc/price-checker.env          # uncomment the "Without Docker" lines and set API keys
sudo chmod 640 /etc/price-checker.env
sudo chgrp pricechecker /etc/price-checker.env

sudo cp deploy/price-checker.service deploy/price-checker-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
```

Create the secret and first admin as the service user, with the same environment as the service:

```bash
sudo mkdir -p /var/lib/price-checker && sudo chown pricechecker: /var/lib/price-checker
sudo -u pricechecker bash -c 'set -a; . /etc/price-checker.env; set +a
  export PRICE_CHECKER_DATA_DIR=/var/lib/price-checker
  cd /opt/price-checker
  .venv/bin/python main.py init-secret
  .venv/bin/python main.py create-admin yourname'

sudo systemctl enable --now price-checker price-checker-backup.timer
```

The app listens on `127.0.0.1:5000`. Put a reverse proxy in front for HTTPS, for example Caddy installed on the host:

```
prices.example.com {
	reverse_proxy 127.0.0.1:5000
}
```

(nginx works too if it sets `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host`, and replaces rather than appends client-supplied values.)

---

## 3. LAN mode

For a server only your household/office uses:

```bash
PRICE_CHECKER_MODE=lan
PRICE_CHECKER_DATA_DIR=/var/lib/price-checker
PRICE_CHECKER_TLS=selfsigned          # or off, or custom with PRICE_CHECKER_TLS_CERT / _KEY
PRICE_CHECKER_ALLOWED_HOSTS=192.168.1.20,nas.home.arpa   # optional but recommended
```

`python main.py init-secret`, `python main.py create-admin <name>`, then `python main.py`. Startup prints the URLs to use.

- `selfsigned` creates a certificate in `DATA_DIR/certs` that covers the hostname and the LAN addresses found at first start. Browsers warn once until you trust it. If the server's IP changes, run `python main.py make-cert` and restart.
- `off` serves plain HTTP: fine on a trusted home network, but passwords are visible to anyone on it.
- To run LAN mode under systemd, use `deploy/price-checker.service` but change the `ExecStart` to `/opt/price-checker/.venv/bin/python main.py`.

### Firewall (ufw)

Allow only your LAN to reach the port (adjust the subnet and port):

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow from 192.168.1.0/24 to any port 5000 proto tcp
sudo ufw enable
```

For the public setup, allow 80 and 443 only:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
```

> **Docker bypasses ufw for published ports.** Docker adds its own iptables rules, so a port in `ports:` is open to the world even if ufw denies it. That is why the compose file publishes only Caddy's 80/443 and keeps the app on the internal network. Never add `ports: - "5000:5000"` to the app.

---

## 4. Reaching it from the internet

### Router port forwarding (public mode)

1. Give the server a fixed address (DHCP reservation in the router).
2. Forward TCP 80 and 443 (and UDP 443 for HTTP/3) to that address.
3. Point your domain at your home IP. If the IP changes, use a dynamic-DNS service.
4. Test from outside your network (mobile data).

This does not work if your ISP puts you behind carrier-grade NAT (your router's WAN address is in `100.64.0.0/10` or differs from what a "what is my IP" site shows). Use a tunnel instead. Forwarding exposes the server directly to the internet, so keep registration on `admin` or `invite` and the API budgets on.

### Cloudflare Tunnel (no open ports)

Run public mode without Caddy and let `cloudflared` connect out to Cloudflare:

1. In Cloudflare Zero Trust create a tunnel, add a public hostname `prices.example.com` with service `http://app:5000`, and copy the tunnel token into `.env` as `TUNNEL_TOKEN=...`.
2. Add this service to `docker-compose.yml`, remove the `caddy` service, and keep the app unpublished:

```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: ${TUNNEL_TOKEN}
    depends_on:
      app:
        condition: service_healthy
```

Cloudflare terminates HTTPS and forwards `X-Forwarded-For` / `X-Forwarded-Proto`, so `PRICE_CHECKER_TRUSTED_PROXIES=1` stays correct. After the first login, check that the failed-login rate limit counts individual visitors rather than one address.

### Tailscale (private, for you and people you invite)

- **Simplest:** run LAN mode and bind it to the server's Tailscale address: `PRICE_CHECKER_MODE=lan`, `PRICE_CHECKER_HOST=100.x.y.z`, `PRICE_CHECKER_ALLOWED_HOSTS=<machine>.<tailnet>.ts.net,100.x.y.z`. Traffic between tailnet devices is already encrypted. Add `PRICE_CHECKER_TLS=selfsigned` if you also want browser-level HTTPS.
- **With a trusted HTTPS certificate:** run LAN mode on `127.0.0.1` and publish it with `tailscale serve --bg 5000`, then open the `https://<machine>.<tailnet>.ts.net` address. Add that name to `PRICE_CHECKER_ALLOWED_HOSTS`.

Nothing is exposed to the public internet in either case.

---

## 5. Backups

The database runs in WAL mode, so **don't copy the `.sqlite3` file while the app is running**: recent writes may sit in the `-wal` file. Use the built-in backup, which is safe at any time:

```bash
docker compose exec app python main.py backup          # keeps the newest 14
# without Docker (enabled in section 2):
sudo systemctl status price-checker-backup.timer          # daily at ~03:30
```

Backups are written to `DATA_DIR/backups/price_checker-<timestamp>.sqlite3` (the app also leaves a `pre-migration-v*.sqlite3` copy before schema upgrades). A backup in the same volume doesn't protect against a lost disk, so copy it elsewhere, for example:

```bash
docker run --rm -v price-checker_price_data:/data -v "$PWD":/out alpine \
  sh -c 'cp /data/backups/$(ls -t /data/backups | head -1) /out/'
```

(The volume is named `<project folder>_price_data`; `docker volume ls` shows it.)

Also keep a copy of `secret_key` (it signs sessions) and your `.env`.

**Restore:** stop the app, replace `price_checker.sqlite3` with the backup, delete any `price_checker.sqlite3-wal` and `-shm` files beside it, and start the app again.
