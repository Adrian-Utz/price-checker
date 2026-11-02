# Running the tests

Run everything from the project root. On Windows use the virtual environment's Python:

```powershell
.\.venv\Scripts\python -m pytest -q
```

On Linux or macOS: `.venv/bin/python -m pytest -q`. In CI, plain `python -m pytest -q`.

## Run one file, one test, or a group

| What | Command |
|---|---|
| One file | `python -m pytest -q tests\test_auth.py` |
| One test | `python -m pytest -q tests\test_auth.py::test_login_logout_roundtrip` |
| By name match | `python -m pytest -q -k "lockout"` |
| Several files | `python -m pytest -q tests\test_auth.py tests\test_users.py` |
| Stop at first failure | add `-x` |
| Show print output | add `-s` |
| Full failure detail | add `-vv` |
| List tests without running | `python -m pytest --collect-only -q` |

Tests that run in both LAN and public mode have the mode in their name, for example
`tests\test_matrix.py::test_admin_pages_open_for_admins[public]`.

## What each file covers

| File | Command | Covers |
|---|---|---|
| `test_main.py` | `python -m pytest -q tests\test_main.py` | Core app: URL parsing, scanning, price history, charts, routes in local mode |
| `test_config.py` | `python -m pytest -q tests\test_config.py` | Mode/host/port/TLS settings, startup refusals, secret file, database migrations |
| `test_users.py` | `python -m pytest -q tests\test_users.py` | Multi-user data model, per-user ownership, 404 on other users' ids, local-data migration |
| `test_auth.py` | `python -m pytest -q tests\test_auth.py` | Login/logout, sessions, lockout, rate limits, registration, invites, admin pages, CLI commands |
| `test_import.py` | `python -m pytest -q tests\test_import.py` | Per-user import and export (JSON, CSV, SQLite) |
| `test_hardening.py` | `python -m pytest -q tests\test_hardening.py` | Disabled routes, security headers, CSP, Host allowlist, CSRF, API budgets, re-check limits |
| `test_serving.py` | `python -m pytest -q tests\test_serving.py` | Server selection, self-signed certificates, LAN URL, gunicorn options, scheduler |
| `test_deploy.py` | `python -m pytest -q tests\test_deploy.py` | Backups, Dockerfile/compose/.dockerignore checks, `.env.example` |
| `test_ui.py` | `python -m pytest -q tests\test_ui.py` | Warning banner, mode badge, quota panel, user menu |
| `test_matrix.py` | `python -m pytest -q tests\test_matrix.py` | Sweeps every route: login required, cross-user isolation, admin-only, local-only routes, TLS config |

`conftest.py` is not a test file. It gives every test its own temporary database and turns CSRF
checks off. Tests that check CSRF turn them back on.

## Notes

- Nothing calls a paid API: tests replace the network calls.
- The Docker build is not part of pytest. It runs in CI (`.github/workflows/ci.yml`, job `docker-smoke`).
  To try it locally: `docker build -t price-checker:ci .`
- The full suite takes about a minute.
