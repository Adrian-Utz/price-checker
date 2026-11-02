import sqlite3

import pytest

from utils.config import ConfigError, create_secret_file, load_settings, startup_errors
from utils.migrations import LATEST_VERSION, MigrationError, migrate

GOOD_SECRET = "s" * 40


def test_defaults_are_local_and_loopback_only():
	settings = load_settings({"PRICE_CHECKER_HOST": "0.0.0.0"})
	assert settings.mode == "local"
	assert settings.host == "127.0.0.1"
	assert settings.auth_required is False
	assert settings.auto_shutdown and settings.open_browser and settings.allow_env_editor
	assert startup_errors(settings, admin_exists=False) == []


def test_lan_mode_flags(tmp_path):
	settings = load_settings({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_PORT": "8443"})
	assert (settings.host, settings.port) == ("0.0.0.0", 8443)
	assert settings.auth_required and not settings.auto_shutdown and not settings.open_browser
	assert not settings.allow_env_editor and not settings.allow_dev_tools
	assert settings.secure_cookies is False


def test_public_mode_binds_loopback_and_trusts_one_proxy(tmp_path):
	settings = load_settings({"PRICE_CHECKER_MODE": "public", "PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	assert settings.host == "127.0.0.1"
	assert settings.trusted_proxies == 1
	assert settings.secure_cookies is True


def test_data_dir_controls_database_and_secret_locations(tmp_path):
	settings = load_settings({"PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	assert settings.database_path == tmp_path / "price_checker.sqlite3"
	create_secret_file(tmp_path)
	assert load_settings({"PRICE_CHECKER_DATA_DIR": str(tmp_path)}).secret


def test_secret_file_is_never_overwritten(tmp_path):
	first = create_secret_file(tmp_path).read_text()
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
	with pytest.raises(ConfigError):
		load_settings(env)


def test_auth_settings_default_to_closed_registration():
	settings = load_settings({})
	assert settings.registration == "admin" and settings.retry_after_header
	assert load_settings({"PRICE_CHECKER_REGISTRATION": "Invite"}).registration == "invite"


def test_lan_refuses_without_secret_and_admin(tmp_path):
	settings = load_settings({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	errors = startup_errors(settings, admin_exists=False)
	assert any("secret" in error for error in errors)
	assert any("admin" in error for error in errors)


def test_public_starts_with_secret_and_admin(tmp_path):
	settings = load_settings({"PRICE_CHECKER_MODE": "public", "PRICE_CHECKER_SECRET": GOOD_SECRET, "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_ALLOWED_HOSTS": "prices.example.com"})
	assert startup_errors(settings, admin_exists=True) == []


def test_public_needs_allowed_hosts_and_a_trusted_proxy(tmp_path):
	env = {"PRICE_CHECKER_MODE": "public", "PRICE_CHECKER_SECRET": GOOD_SECRET, "PRICE_CHECKER_DATA_DIR": str(tmp_path)}
	assert any("ALLOWED_HOSTS" in error for error in startup_errors(load_settings(env), admin_exists=True))
	env.update(PRICE_CHECKER_ALLOWED_HOSTS="a.example.com", PRICE_CHECKER_TRUSTED_PROXIES="0")
	assert any("TRUSTED_PROXIES" in error for error in startup_errors(load_settings(env), admin_exists=True))


def test_short_secret_is_refused(tmp_path):
	settings = load_settings({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_SECRET": "short", "PRICE_CHECKER_DATA_DIR": str(tmp_path)})
	assert any("at least" in error for error in startup_errors(settings, admin_exists=True))


def test_custom_tls_needs_existing_files(tmp_path):
	env = {"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_SECRET": GOOD_SECRET, "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_TLS": "custom"}
	assert len(startup_errors(load_settings(env), admin_exists=True)) == 2
	cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
	cert.write_text("x")
	key.write_text("x")
	env.update(PRICE_CHECKER_TLS_CERT=str(cert), PRICE_CHECKER_TLS_KEY=str(key))
	settings = load_settings(env)
	assert startup_errors(settings, admin_exists=True) == []
	assert settings.scheme == "https" and settings.secure_cookies


def test_migrate_fresh_database_reaches_latest():
	connection = sqlite3.connect(":memory:")
	assert migrate(connection) == LATEST_VERSION
	tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
	assert {"products", "observations", "scans", "stores", "users"} <= tables
	assert migrate(connection) == LATEST_VERSION


def test_migrate_legacy_database_keeps_data_and_backs_up(tmp_path):
	path = tmp_path / "legacy.sqlite3"
	connection = sqlite3.connect(path)
	connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT, source TEXT NOT NULL, price REAL, currency TEXT, checked_at TEXT, error TEXT)")
	connection.execute("INSERT INTO products (url, title, source) VALUES ('https://a.test/x', 'Thing', 'a')")
	connection.commit()
	migrate(connection, backup_dir=tmp_path / "backups")
	assert connection.execute("SELECT title, api_provider FROM products").fetchone() == ("Thing", None)
	assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
	assert (tmp_path / "backups" / "pre-migration-v0.sqlite3").is_file()


def test_migrate_refuses_newer_database():
	connection = sqlite3.connect(":memory:")
	connection.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
	with pytest.raises(MigrationError):
		migrate(connection)
