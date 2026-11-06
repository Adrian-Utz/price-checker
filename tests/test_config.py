import sqlite3

import pytest

from utils.config import ConfigError, create_secret_file, load_settings, startup_errors
from utils.migrations import LATEST_VERSION, MigrationError, migrate

GOOD_SECRET = "s" * 40 #A 40-character long string for secret purposes


def test_defaults_are_local_and_loopback_only():
	#Load settings with default values
	settings = load_settings({"PRICE_CHECKER_HOST": "0.0.0.0"})
	#Assert that the mode is local, host is loopback, and other settings are default.
	assert settings.mode == "local"
	assert settings.host == "127.0.0.1"
	assert settings.auth_required is False
	assert settings.auto_shutdown 
	assert settings.open_browser 
	assert settings.allow_env_editor
	#Check for startup errors
	assert startup_errors(settings, admin_exists=False) == []


def test_lan_mode_flags(tmp_path):
	#Load settings with LAN mode, data directory, and port
	settings = load_settings({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_PORT": "8443"})
	#Assert that host is 0.0.0.0 and port is 8443
	assert (settings.host, settings.port) == ("0.0.0.0", 8443)
	#Assert that auth_required, open_browser and auto_shutdown are true
	assert settings.auth_required 
	assert not settings.auto_shutdown 
	assert not settings.open_browser
	assert not settings.allow_env_editor 
	assert not settings.allow_dev_tools
	assert settings.secure_cookies is False


def test_public_mode_binds_loopback_and_trusts_one_proxy(tmp_path):
	#Load settings with public mode, data directory, and trusted proxy
	settings = load_settings({"PRICE_CHECKER_MODE": "public", "PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	#Assert that host is 127.0.0.1 and the trusted proxy is 1
	assert settings.host == "127.0.0.1"
	assert settings.trusted_proxies == 1
	assert settings.secure_cookies is True


def test_data_dir_controls_database_and_secret_locations(tmp_path):
	#Load settings with data directory
	settings = load_settings({"PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	#Assert that database path is the data directory with price_checker.sqlite3
	assert settings.database_path == tmp_path / "price_checker.sqlite3"
	#Create a secret file in the data directory
	create_secret_file(tmp_path)
	#Assert that the secret was loaded correctly
	assert load_settings({"PRICE_CHECKER_DATA_DIR": str(tmp_path)}).secret


def test_secret_file_is_never_overwritten(tmp_path):
	#Creat a secret file and read its own content
	first = create_secret_file(tmp_path).read_text()
	# Assert that the secret is still the same after creation
	assert create_secret_file(tmp_path).read_text() == first


@pytest.mark.parametrize("env", [
	{"PRICE_CHECKER_MODE": "nope"},
	{"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_TLS": "maybe"},
	{"PRICE_CHECKER_PORT": "99999"},
	{"PRICE_CHECKER_DEVELOPER_MODE": "perhaps"},
	{"PRICE_CHECKER_REGISTRATION": "anyone"},
	{"PRICE_CHECKER_LOGIN_MAX_FAILURES": "1"},
])
def test_invalid_values_are_rejected(env):
	#Load settings with invalid enironment variables and assert a ConfigError
	with pytest.raises(ConfigError):
		load_settings(env)


def test_auth_settings_default_to_closed_registration():
	#Load settings with default registration
	settings = load_settings({})
	#Assert that registration is 'admin' and retry_after_header is True
	assert settings.registration == "admin" 
	assert settings.retry_after_header
	#Load settings with 'Invite' registration and assert it's 'invite'
	assert load_settings({"PRICE_CHECKER_REGISTRATION": "Invite"}).registration == "invite"


def test_lan_refuses_without_secret_and_admin(tmp_path):
	#Load settings with LAN mode and data directory and assert errors
	settings = load_settings({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	#Assert  errors containing 'secret' and 'admin'
	errors = startup_errors(settings, admin_exists=False)
	assert any("secret" in error for error in errors)
	assert any("admin" in error for error in errors)


def test_public_starts_with_secret_and_admin(tmp_path):
	#Load settings with public mode , secret, data directory, and allowed hosts
	settings = load_settings({"PRICE_CHECKER_MODE": "public", "PRICE_CHECKER_SECRET": GOOD_SECRET, "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_ALLOWED_HOSTS": "prices.example.com"})
	#Assert no startup errors
	assert startup_errors(settings, admin_exists=True) == []


def test_public_needs_allowed_hosts_and_a_trusted_proxy(tmp_path):
	#Load settings with public mode, secret, data directory, and missing allowed hosts
	env = {"PRICE_CHECKER_MODE": "public", "PRICE_CHECKER_SECRET": GOOD_SECRET, "PRICE_CHECKER_DATA_DIR": str(tmp_path)}
	#Assert errors containing 'ALLOWED_HOSTS'
	assert any("ALLOWED_HOSTS" in error for error in startup_errors(load_settings(env), admin_exists=True))
	#Update environment with allowed hosts and assert errors containing 'TRUSTED_PROXIES'
	env.update(PRICE_CHECKER_ALLOWED_HOSTS="a.example.com", PRICE_CHECKER_TRUSTED_PROXIES="0")
	assert any("TRUSTED_PROXIES" in error for error in startup_errors(load_settings(env), admin_exists=True))


def test_short_secret_is_refused(tmp_path):
	#Load Settings with LAN mode, short secret, data directory, and assert errors
	settings = load_settings({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_SECRET": "short", "PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	#Assert errors contianing 'at least'
	assert any("at least" in error for error in startup_errors(settings, admin_exists=True))


def test_custom_tls_needs_existing_files(tmp_path):
	#Load settings with LAN mode, secret, data directory, and custom TLS
	env = {"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_SECRET": GOOD_SECRET, "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_TLS": "custom"}
	#Assert two errors
	assert len(startup_errors(load_settings(env), admin_exists=True)) == 2
	#Create PEM files for TLS
	cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
	cert.write_text("x")
	key.write_text("x")
	# Update Enviroment with TLS files and assert no errors
	env.update(PRICE_CHECKER_TLS_CERT=str(cert), PRICE_CHECKER_TLS_KEY=str(key))
	settings = load_settings(env)
	assert startup_errors(settings, admin_exists=True) == []
	assert settings.scheme == "https"
	#Assert HTTPS scheme and secure cookies
	assert settings.secure_cookies


def test_migrate_fresh_database_reaches_latest():
	#Connect to a new in-memory SQLite database
	connection = sqlite3.connect(":memory:")
	#Assert migration reaches the latest version
	assert migrate(connection) == LATEST_VERSION
	#Get all table names
	tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
	#Assert table exists
	assert {"products", "observations", "scans", "stores", "users"} <= tables
	#Assert migration reaches the latest version again
	assert migrate(connection) == LATEST_VERSION


def test_migrate_legacy_database_keeps_data_and_backs_up(tmp_path):
	#Create a legacy SQLite database
	path = tmp_path / "legacy.sqlite3"
	connection = sqlite3.connect(path)
	connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT, source TEXT NOT NULL, price REAL, currency TEXT, checked_at TEXT, error TEXT)")
	connection.execute("INSERT INTO products (url, title, source) VALUES ('https://a.test/x', 'Thing', 'a')")
	connection.commit()
	#Migrate the legacy database
	migrate(connection, backup_dir=tmp_path / "backups")
	#Assert data and user_version
	assert connection.execute("SELECT title, api_provider FROM products").fetchone() == ("Thing", None)
	assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
	# Assert backup file exists
	assert (tmp_path / "backups" / "pre-migration-v0.sqlite3").is_file()


def test_migrate_refuses_newer_database():
	#Connect to a new in-memory SQLite database with a newer user_version
	connection = sqlite3.connect(":memory:")
	connection.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
	#Assert migration raises a MigrationError
	with pytest.raises(MigrationError):
		migrate(connection)
