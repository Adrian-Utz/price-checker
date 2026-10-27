from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

"""
This file will read the mode and derive the host, port auth requirement, shutdown behavior, and feature flags.

Modes:
	local  - 127.0.0.1 only, no accounts, tab-close shutdown (the original behavior).
	lan    - reachable on the local network, accounts required, optional TLS.
	public - internet hosting behind a reverse proxy, accounts required.

Last Update: 10/20/2026
Written on: 10/20/2026
Written by: AJ Utz
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_DATABASE_PATH = Path(__file__).resolve().parent / "price_checker.sqlite3"
MODES = ("local", "lan", "public")
TLS_MODES = ("off", "selfsigned", "custom")
REGISTRATION_MODES = ("admin", "invite", "open")
LOCAL_HOST = "127.0.0.1"
LOCAL_HOSTNAMES = ("localhost", "127.0.0.1", "[::1]")
SECRET_FILE_NAME = "secret_key"
MIN_SECRET_LENGTH = 32
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


class ConfigError(ValueError):
	"""Raised when the environment describes an invalid configuration."""


@dataclass(frozen=True)
class Settings:
	mode: str
	host: str
	port: int
	tls: str
	tls_cert: Path | None
	tls_key: Path | None
	data_dir: Path
	database_path: Path
	dev_database_path: Path
	secret: str | None
	trusted_proxies: int
	auth_required: bool
	auto_shutdown: bool
	open_browser: bool
	allow_env_editor: bool
	allow_dev_tools: bool
	registration: str = "admin"
	session_idle_minutes: int = 120
	remember_days: int = 30
	login_max_failures: int = 5
	login_lockout_minutes: int = 15
	login_ip_limit: int = 20
	retry_after_header: bool = True
	allowed_hosts: tuple[str, ...] = ()
	user_daily_api_calls: int = 0
	global_daily_api_calls: int = 0
	rechecks_per_hour: int = 0

	@property
	def is_local(self) -> bool:
		return self.mode == "local"

	@property
	def secure_cookies(self) -> bool:
		# Public mode sits behind an HTTPS proxy; LAN is secure only when this app serves TLS itself.
		return self.mode == "public" or self.tls != "off"

	@property
	def scheme(self) -> str:
		return "https" if self.tls != "off" else "http"


def load_env_file() -> None:
	load_dotenv(PROJECT_ROOT / ".env", override=True)


def _flag(env: Mapping[str, str], key: str, default: bool) -> bool:
	"""Check if the specified env variable is true, false or none"""
	raw = env.get(key)
	if raw is None or not raw.strip():
		return default

	value = raw.strip().lower()
	if value in TRUE_VALUES:
		return True
	if value in FALSE_VALUES:
		return False

	raise ConfigError(f"{key} must be one of 1/0, true/false, yes/no, on/off.")


def _integer(env: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
	"""Check and return the specified integer from the env file"""
	raw = env.get(key)
	if raw is None or not raw.strip():
		return default

	try:
		value = int(raw)
	except ValueError:
		raise ConfigError(f"{key} must be a whole number.") from None

	if not low <= value <= high:
		raise ConfigError(f"{key} must be between {low} and {high}.")
	return value


def read_secret(data_dir: Path, env: Mapping[str, str] | None = None) -> str | None:
	"""Return the persistent secret from PRICE_CHECKER_SECRET or the data directory's secret file, if any."""
	env = os.environ if env is None else env
	configured = env.get("PRICE_CHECKER_SECRET", "").strip()

	if configured:
		return configured

	secret_file = data_dir / SECRET_FILE_NAME
	if secret_file.is_file():
		return secret_file.read_text(encoding="utf-8").strip() or None
	return None


def create_secret_file(data_dir: Path) -> Path:
	"""Create the secret file once. An existing secret is never overwritten, so sessions stay valid."""
	data_dir.mkdir(parents=True, exist_ok=True)
	secret_file = data_dir / SECRET_FILE_NAME

	try:
		with open(secret_file, "x", encoding="utf-8") as handle:
			handle.write(secrets.token_hex(32))
	except FileExistsError:
		return secret_file

	try:
		secret_file.chmod(0o600)
	except OSError:
		pass

	return secret_file


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
	"""## Loads the selected modes from the env file.
	**PRICE_CHECKER_MODE** = local, lan, public  
	**PRICE_CHECKER_DATA_DIR** = Directory for the DB, Secrets and certs for Docker  
	**PRICE_CHECKER_HOST** = LAN mode uses default 0.0.0.0 if not specified, Ignored in local mode  
	**PRICE_CHECKER_TLS** = off, selfsigned or custom. Only honored in LAN mode, and uses PRICE_CHECKER_TLS_CERT and PRICE_CHECKER_TLS_KEY
	                    selfsigned is accepted but doesn't start for now.
                        custom TLS is wired into the server and fails at startup if the files are missing.  
	**PRICE_CHECKER_TRUSTED_PROXIES** = Tells the middleware exaclty how many hops back to trust,
	                    stripping out any fake headers injected. defaults to 1
	"""
	env = os.environ if env is None else env
	mode = env.get("PRICE_CHECKER_MODE", "local").strip().lower() or "local"

	if mode not in MODES:
		raise ConfigError(f"PRICE_CHECKER_MODE must be one of: {', '.join(MODES)}.")

	is_local = mode == "local"
	configured_data_dir = env.get("PRICE_CHECKER_DATA_DIR", "").strip()
	data_dir = Path(configured_data_dir).expanduser() if configured_data_dir else PROJECT_ROOT

	# Local mode ignores PRICE_CHECKER_HOST so a leftover LAN value can never expose the app.
	if is_local:
		host = LOCAL_HOST
	else:
		default_host = "0.0.0.0" if mode == "lan" else LOCAL_HOST
		host = env.get("PRICE_CHECKER_HOST", "").strip() or default_host

	tls = "off" if mode != "lan" else env.get("PRICE_CHECKER_TLS", "off").strip().lower() or "off"

	if tls not in TLS_MODES:
		raise ConfigError(f"PRICE_CHECKER_TLS must be one of: {', '.join(TLS_MODES)}.")

	registration = env.get("PRICE_CHECKER_REGISTRATION", "admin").strip().lower() or "admin"
	if registration not in REGISTRATION_MODES:
		raise ConfigError(f"PRICE_CHECKER_REGISTRATION must be one of: {', '.join(REGISTRATION_MODES)}.")

	allowed_hosts = tuple(
		host.strip().lower() for host in env.get("PRICE_CHECKER_ALLOWED_HOSTS", "").split(",") if host.strip()
	)
	if is_local and not allowed_hosts:
		allowed_hosts = LOCAL_HOSTNAMES #Rejecting other Host headers blocks DNS-rebinding attacks on the local server
	for entry in allowed_hosts:
		if not re.fullmatch(r"(\*\.)?[a-z0-9._\[\]:-]+", entry):
			raise ConfigError(f"PRICE_CHECKER_ALLOWED_HOSTS has an invalid entry: {entry!r}.")

	cert = env.get("PRICE_CHECKER_TLS_CERT", "").strip()
	key = env.get("PRICE_CHECKER_TLS_KEY", "").strip()
	database = env.get("PRICE_CHECKER_DB", "").strip()
	
	if database:
		database_path = Path(database).expanduser()
	elif configured_data_dir:
		database_path = data_dir / "price_checker.sqlite3"
	else:
		database_path = LEGACY_DATABASE_PATH

	dev_database = env.get("PRICE_CHECKER_DEV_DB", "").strip()

	if dev_database:
		dev_database_path = Path(dev_database).expanduser()
	elif configured_data_dir:
		dev_database_path = data_dir / "price_checker.dev.sqlite3"
	else:
		dev_database_path = LEGACY_DATABASE_PATH.with_name("price_checker.dev.sqlite3")

	developer_mode = _flag(env, "PRICE_CHECKER_DEVELOPER_MODE", False)
	
	return Settings(
		mode=mode,
		host=host,
		port=_integer(env, "PRICE_CHECKER_PORT", _integer(env, "PORT", 5000, 1, 65535), 1, 65535),
		tls=tls,
		tls_cert=Path(cert).expanduser() if cert else None,
		tls_key=Path(key).expanduser() if key else None,
		data_dir=data_dir,
		database_path=database_path,
		dev_database_path=dev_database_path,
		secret=read_secret(data_dir, env),
		trusted_proxies=_integer(env, "PRICE_CHECKER_TRUSTED_PROXIES", 1 if mode == "public" else 0, 0, 10),
		auth_required=not is_local,
		auto_shutdown=is_local,
		open_browser=is_local and _flag(env, "PRICE_CHECKER_OPEN_BROWSER", True),
		allow_env_editor=is_local,
		allow_dev_tools=is_local and developer_mode,
		registration=registration,
		session_idle_minutes=_integer(env, "PRICE_CHECKER_SESSION_IDLE_MINUTES", 120, 5, 10080),
		remember_days=_integer(env, "PRICE_CHECKER_REMEMBER_DAYS", 30, 0, 365),
		login_max_failures=_integer(env, "PRICE_CHECKER_LOGIN_MAX_FAILURES", 5, 3, 50),
		login_lockout_minutes=_integer(env, "PRICE_CHECKER_LOGIN_LOCKOUT_MINUTES", 15, 1, 1440),
		login_ip_limit=_integer(env, "PRICE_CHECKER_LOGIN_IP_LIMIT", 20, 5, 1000),
		retry_after_header=_flag(env, "PRICE_CHECKER_RETRY_AFTER", True),
		allowed_hosts=allowed_hosts,
		# One API call is one credit. Local mode has no limits by default; shared servers do.
		user_daily_api_calls=_integer(env, "PRICE_CHECKER_USER_DAILY_API_CALLS", 0 if is_local else 150, 0, 100000),
		global_daily_api_calls=_integer(env, "PRICE_CHECKER_GLOBAL_DAILY_API_CALLS", 0, 0, 1000000),
		rechecks_per_hour=_integer(env, "PRICE_CHECKER_RECHECKS_PER_HOUR", 0 if is_local else 20, 0, 10000),
	)


def startup_errors(settings: Settings, admin_exists: bool) -> list[str]:
	"""Return every reason this configuration must not start. Local mode has none."""
	if settings.is_local:
		return []

	errors: list[str] = []

	if not settings.secret:
		errors.append(
			f"No persistent secret. Set PRICE_CHECKER_SECRET or create {settings.data_dir / SECRET_FILE_NAME} "
			"(run `python main.py init-secret`)."
		)
	elif len(settings.secret) < MIN_SECRET_LENGTH:
		errors.append(f"The secret must be at least {MIN_SECRET_LENGTH} characters.")

	if not admin_exists:
		errors.append("No admin account exists. Create one with `python main.py create-admin`.")

	if settings.tls == "selfsigned":
		errors.append("PRICE_CHECKER_TLS=selfsigned is not implemented yet; use off or custom.")

	if settings.mode == "public":
		if not settings.allowed_hosts:
			errors.append("Public mode needs PRICE_CHECKER_ALLOWED_HOSTS (comma-separated public host names, e.g. prices.example.com).")
		if settings.trusted_proxies < 1:
			errors.append("Public mode runs behind a reverse proxy: PRICE_CHECKER_TRUSTED_PROXIES must be at least 1, or every visitor looks like the proxy and rate limits break.")

	if settings.tls == "custom":
		if not settings.tls_cert or not settings.tls_cert.is_file():
			errors.append("PRICE_CHECKER_TLS=custom needs PRICE_CHECKER_TLS_CERT to point to an existing file.")
		if not settings.tls_key or not settings.tls_key.is_file():
			errors.append("PRICE_CHECKER_TLS=custom needs PRICE_CHECKER_TLS_KEY to point to an existing file.")

	return errors
