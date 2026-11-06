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
	#This test ensures that the backup is a consistent copy and that old backups are pruned.
	with connect() as connection:
		create_user(connection, "alice", "correct horse battery")
	first = backup_database(keep=2)
	#Verify the backup is in the correct directory
	assert first.parent == isolated_database.parent / "backups"
	#Connect to the backup and verify the data
	with sqlite3.connect(first) as copy:
		assert copy.execute("SELECT COUNT(*) FROM users WHERE username = 'alice'").fetchone()[0] == 1
	copy.close() #Close the copy connection
	#Delete some older backups to simulate pruning
	for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
		(first.parent / f"price_checker-{stamp}.sqlite3").write_bytes(b"")
	#Create another backup to simulate a new backup
	(first.parent / "pre-migration-v1.sqlite3").write_bytes(b"")
	backup_database(keep=2) # Take a new backup with keep=2
	#Verify that only the latest two backups remain
	scheduled = sorted(first.parent.glob("price_checker-*.sqlite3"))
	assert len(scheduled) == 2
	assert (first.parent / "pre-migration-v1.sqlite3").exists()


def test_backup_cli_command(isolated_database, capsys):
	#This test checks the backup CLI command and verifies if the output is as expected
	main.run_cli_command("backup", ["3"])
	#Assert that the backup written to the correct location
	assert "Backup written to" in capsys.readouterr().out


def test_public_mode_env_file_never_writes_blank_api_keys(monkeypatch, tmp_path):
	#This test ensures that the environment file never writes blank API keys when in public mode
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="public"))
	monkeypatch.setattr(main, "ENV_FILE", tmp_path / "data" / ".env")
	monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: None)
	#Check if the environment file is empty when in public mode
	assert main.read_env_file_text() == ""
	#Update the environment file with a non-blanck API key
	main.save_env_file_text(main.update_env_setting(main.read_env_file_text(), "PRICE_CHECKER_API_PROVIDER", "serpapi"))
	#Verify that the environment file contains the updated API key
	assert (tmp_path / "data" / ".env").read_text().strip() == "PRICE_CHECKER_API_PROVIDER=serpapi"


def test_compose_file_keeps_app_private_and_requires_a_domain():
	#This test checks if the Docker Compose file keeps the application private and requires a domain
	compose = (ROOT / "docker-compose.yml").read_text()
	#Extract the application block form the Docker Compose file
	app_block = compose.split("  caddy:")[0]
	#Verify that the application block does not have public ports and contains required settings
	assert "ports:" not in app_block
	assert "DOMAIN:?" in compose 
	assert "PRICE_CHECKER_TRUSTED_PROXIES" in app_block
	assert "reverse_proxy app:5000" in (ROOT / "Caddyfile").read_text()


def test_dockerfile_runs_unprivileged_with_a_healthcheck():
	#This test checks if the Dockerfile runs unprivileged and has a healthcheck
	dockerfile = (ROOT / "Dockerfile").read_text()
	#Verify that the Dockerfile runs as the "app" user
	assert re.search(r"^USER app$", dockerfile, re.M)
	#Verify that Dockerfile has a healthcheck, contians the correct healthcheck URL
	assert "HEALTHCHECK" in dockerfile 
	assert "/healthz" in dockerfile
	#Verify that Dockerfile has a volumen definitio and sets the PRICE_CHECKER_DATA_DIR
	assert "VOLUME /data" in dockerfile 
	assert "PRICE_CHECKER_DATA_DIR=/data" in dockerfile
	assert '"wsgi:app"' in dockerfile


def test_dockerignore_keeps_secrets_out_but_ships_what_the_image_needs():
	#This test checks if the .dockerignore file keeps secrets out but ships what the image needs
	patterns = (ROOT / ".dockerignore").read_text().split()
	for secret in (".env", "secret_key", "*.sqlite3"):
		assert secret in patterns
	for needed in ("docker", "wsgi.py", "gunicorn.conf.py", "requirements.txt", "utils", "templates", "static"):
		assert needed not in patterns


def test_entrypoint_line_endings_are_normalized_for_linux():
	#This test checks if the entrypoint line endings are normalized for Linux.
	# Windows checkouts may use CRLF, which would break the #! line inside the container.
	assert "eol=lf" in (ROOT / ".gitattributes").read_text()
	assert "s/\\r$//" in (ROOT / "Dockerfile").read_text()


def test_env_example_has_no_real_secrets_and_lists_required_settings():
	#This test checks if the .env.example file has no real secrets and lists required settings.
	text = (ROOT / ".env.example").read_text()
	#Verify that the .env.example file has the correct domain and daily API calls setting
	assert "DOMAIN=" in text 
	assert "PRICE_CHECKER_USER_DAILY_API_CALLS" in text
	# Verify that the .env.example file contains valid placeholder for the API key
	assert re.search(r"^SERPAPI_API_KEY=$", text, re.M)


def test_env_example_mode_examples_are_commented_out_and_valid():
	#This test checks if the .env.example mode examples are commented out and valid
	text = (ROOT / ".env.example").read_text()
	#verify that the .env.example file does not have uncommented mode settings
	assert not re.search(r"^PRICE_CHECKER_(MODE|TLS|ALLOWED_HOSTS|DATA_DIR|TRUSTED_PROXIES)=", text, re.M)
	#Verify that each uncommented mode block must load without errors
	for mode in ("local", "lan", "public"):
		assert f"# PRICE_CHECKER_MODE={mode}" in text
	# Each uncommented mode block must load without errors.
	from utils.config import load_settings
	for mode, extra in (("lan", {"PRICE_CHECKER_TLS": "selfsigned"}), ("public", {"PRICE_CHECKER_ALLOWED_HOSTS": "prices.example.com"}), ("local", {})):
		assert load_settings({"PRICE_CHECKER_MODE": mode, **extra}).mode == mode


def test_gunicorn_conf_loads_and_uses_one_worker(monkeypatch):
	#This test checks if the GUnicorn config loads and uses one worker
	import importlib.util
	#load the Gunicorn config file
	spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", ROOT / "gunicorn.conf.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	#Verify that the Gunicorn config uses one worker and a thread workerclass
	assert module.workers == 1 
	assert module.worker_class == "gthread" 
	assert callable(module.post_worker_init)
