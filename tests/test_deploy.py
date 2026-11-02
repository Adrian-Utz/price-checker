import dataclasses
import re
import sqlite3
from pathlib import Path

import pytest

import main
from utils.database import backup_database, connect
from utils.users import create_user

ROOT = Path(__file__).resolve().parent.parent


def test_backup_is_a_consistent_copy_and_old_ones_are_pruned(isolated_database):
	with connect() as connection:
		create_user(connection, "alice", "correct horse battery")
	first = backup_database(keep=2)
	assert first.parent == isolated_database.parent / "backups"
	copy = sqlite3.connect(first)
	assert copy.execute("SELECT COUNT(*) FROM users WHERE username = 'alice'").fetchone()[0] == 1
	copy.close()
	for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
		(first.parent / f"price_checker-{stamp}.sqlite3").write_bytes(b"")
	(first.parent / "pre-migration-v1.sqlite3").write_bytes(b"")
	backup_database(keep=2)
	scheduled = sorted(first.parent.glob("price_checker-*.sqlite3"))
	assert len(scheduled) == 2
	assert (first.parent / "pre-migration-v1.sqlite3").exists()


def test_backup_cli_command(isolated_database, capsys):
	main.run_cli_command("backup", ["3"])
	assert "Backup written to" in capsys.readouterr().out


def test_public_mode_env_file_never_writes_blank_api_keys(monkeypatch, tmp_path):
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="public"))
	monkeypatch.setattr(main, "ENV_FILE", tmp_path / "data" / ".env")
	monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: None)
	assert main.read_env_file_text() == ""
	main.save_env_file_text(main.update_env_setting(main.read_env_file_text(), "PRICE_CHECKER_API_PROVIDER", "serpapi"))
	assert (tmp_path / "data" / ".env").read_text().strip() == "PRICE_CHECKER_API_PROVIDER=serpapi"


def test_compose_file_keeps_app_private_and_requires_a_domain():
	compose = (ROOT / "docker-compose.yml").read_text()
	app_block = compose.split("  caddy:")[0]
	assert "ports:" not in app_block
	assert "DOMAIN:?" in compose and "PRICE_CHECKER_TRUSTED_PROXIES" in app_block
	assert "reverse_proxy app:5000" in (ROOT / "Caddyfile").read_text()


def test_dockerfile_runs_unprivileged_with_a_healthcheck():
	dockerfile = (ROOT / "Dockerfile").read_text()
	assert re.search(r"^USER app$", dockerfile, re.M)
	assert "HEALTHCHECK" in dockerfile and "/healthz" in dockerfile
	assert "VOLUME /data" in dockerfile and "PRICE_CHECKER_DATA_DIR=/data" in dockerfile
	assert '"wsgi:app"' in dockerfile


def test_dockerignore_keeps_secrets_out_but_ships_what_the_image_needs():
	patterns = (ROOT / ".dockerignore").read_text().split()
	for secret in (".env", "secret_key", "*.sqlite3"):
		assert secret in patterns
	for needed in ("docker", "wsgi.py", "gunicorn.conf.py", "requirements.txt", "utils", "templates", "static"):
		assert needed not in patterns


def test_entrypoint_line_endings_are_normalized_for_linux():
	# Windows checkouts may use CRLF, which would break the #! line inside the container.
	assert "eol=lf" in (ROOT / ".gitattributes").read_text()
	assert "s/\\r$//" in (ROOT / "Dockerfile").read_text()


def test_env_example_has_no_real_secrets_and_lists_required_settings():
	text = (ROOT / ".env.example").read_text()
	assert "DOMAIN=" in text and "PRICE_CHECKER_USER_DAILY_API_CALLS" in text
	assert re.search(r"^SERPAPI_API_KEY=$", text, re.M)


def test_env_example_mode_examples_are_commented_out_and_valid():
	text = (ROOT / ".env.example").read_text()
	assert not re.search(r"^PRICE_CHECKER_(MODE|TLS|ALLOWED_HOSTS|DATA_DIR|TRUSTED_PROXIES)=", text, re.M)
	for mode in ("local", "lan", "public"):
		assert f"# PRICE_CHECKER_MODE={mode}" in text
	# Each uncommented mode block must load without errors.
	from utils.config import load_settings
	for mode, extra in (("lan", {"PRICE_CHECKER_TLS": "selfsigned"}), ("public", {"PRICE_CHECKER_ALLOWED_HOSTS": "prices.example.com"}), ("local", {})):
		assert load_settings({"PRICE_CHECKER_MODE": mode, **extra}).mode == mode


def test_gunicorn_conf_loads_and_uses_one_worker(monkeypatch):
	import importlib.util
	spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", ROOT / "gunicorn.conf.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	assert module.workers == 1 and module.worker_class == "gthread" and callable(module.post_worker_init)
