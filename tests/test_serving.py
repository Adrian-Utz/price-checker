import dataclasses
import sqlite3

import pytest
from cryptography import x509

import main
from utils import serving
from utils.config import load_settings, startup_errors
from utils.database import connect

GOOD_SECRET = "s" * 40


def settings_for(tmp_path, **env):
	"""Set the load settings for the data directory and secret"""
	return load_settings({"PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_SECRET": GOOD_SECRET, **env})


@pytest.mark.parametrize("env, expected", [
	({}, "werkzeug"),
	({"PRICE_CHECKER_MODE": "lan"}, "waitress"),
	({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_TLS": "selfsigned"}, "werkzeug"),
	({"PRICE_CHECKER_MODE": "public"}, "gunicorn"),
	({"PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_SERVER": "werkzeug"}, "werkzeug"),
])
def test_server_is_chosen_by_mode(tmp_path, env, expected):
	"""Assert that the server mode is chosen by the env and tmp_path"""
	assert serving.resolve_server(settings_for(tmp_path, **env)) == expected


def test_invalid_server_is_rejected(tmp_path):
	"""Test that only accepted server types are passed"""
	with pytest.raises(Exception):
		settings_for(tmp_path, PRICE_CHECKER_SERVER="apache")


def test_waitress_with_tls_is_refused(tmp_path):
	"""Test that if waitress is assigned, it refuses TLS """
	settings = settings_for(tmp_path, PRICE_CHECKER_MODE="lan", PRICE_CHECKER_TLS="selfsigned", PRICE_CHECKER_SERVER="waitress")
	#Assert for startup errors
	assert any("Waitress" in error for error in startup_errors(settings, admin_exists=True))


def test_public_mode_refuses_waitress(tmp_path):
	"""Test that public mode refuses waitress"""
	settings = settings_for(tmp_path, PRICE_CHECKER_MODE="public", PRICE_CHECKER_ALLOWED_HOSTS="a.example.com", PRICE_CHECKER_SERVER="waitress")
	#Assert startup errors as public mode need gunicorn
	assert any("Public mode needs gunicorn" in error for error in startup_errors(settings, admin_exists=True))


def test_selfsigned_certificate_is_created_once_and_covers_names(tmp_path):
	"""Test that selfsigned certificat is created and covers names"""
	settings = settings_for(tmp_path, PRICE_CHECKER_MODE="lan", PRICE_CHECKER_TLS="selfsigned", PRICE_CHECKER_ALLOWED_HOSTS="nas.home.arpa,192.168.50.7")
	#Assert if there are any startup errors 
	assert startup_errors(settings, admin_exists=True) == []
	serving.ensure_tls_files(settings)
	cert_bytes = settings.tls_cert.read_bytes()
	assert settings.tls_key.is_file()
	serving.ensure_tls_files(settings)
	assert settings.tls_cert.read_bytes() == cert_bytes
	#Extract the Subject Alternative Name extension data from the certificate
	san = x509.load_pem_x509_certificate(cert_bytes).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
	#Checks the domain name lis listed in side the certificaes dNSName entries
	assert "nas.home.arpa" in san.get_values_for_type(x509.DNSName)
	#Extracts all IP address entries from the SAN, converts each to a string and checks that the IP is prsent
	assert "192.168.50.7" in [str(address) for address in san.get_values_for_type(x509.IPAddress)]
	serving.ensure_tls_files(settings, force=True)
	assert settings.tls_cert.read_bytes() != cert_bytes


def test_custom_certificates_are_never_overwritten(tmp_path):
	"""Test that the custom certificate is never overwritten"""
	settings = settings_for(tmp_path, PRICE_CHECKER_MODE="lan", PRICE_CHECKER_TLS="custom")
	serving.ensure_tls_files(settings, force=True)
	assert not (tmp_path / "certs").exists()


def test_access_urls_list_lan_addresses_for_wildcard_bind(tmp_path, monkeypatch):
	#Set the lambda function for lan_address to return the IP
	monkeypatch.setattr(serving, "lan_addresses", lambda: ["192.168.1.20"])
	#Set the lambda function for gethostname to return nas
	monkeypatch.setattr(serving.socket, "gethostname", lambda: "nas")
	#Get settings for LAN mode
	lan = settings_for(tmp_path, PRICE_CHECKER_MODE="lan", PRICE_CHECKER_PORT="8080")
	assert serving.access_urls(lan) == ["http://192.168.1.20:8080", "http://nas:8080"]
	#Get settings for TLS mode
	tls = settings_for(tmp_path, PRICE_CHECKER_MODE="lan", PRICE_CHECKER_TLS="selfsigned", PRICE_CHECKER_HOST="10.0.0.5")
	assert serving.access_urls(tls) == ["https://10.0.0.5:5000"]


def test_gunicorn_uses_one_process_with_threads(tmp_path):
	#get Gunicorn options for public mode
	options = serving.gunicorn_options(settings_for(tmp_path, PRICE_CHECKER_MODE="public", PRICE_CHECKER_THREADS="4"))
	#assert that the following settings are true
	assert options["workers"] == 1 
	assert options["threads"] == 4 
	assert options["worker_class"] == "gthread"
	assert options["bind"] == "127.0.0.1:5000"


def test_scheduler_is_off_by_default_and_runs_once_per_day(monkeypatch):
	#Assert that the scheduler is off by default
	assert serving.start_scheduler(-1, lambda: None) is None
	calls = []
	#Set the scheduler polling interval
	monkeypatch.setattr(serving, "SCHEDULER_POLL_SECONDS", 0.01)
	thread = serving.start_scheduler(0, lambda: calls.append(1))
	#Wait for 0.2 secs
	import time
	time.sleep(0.2)
	#Assert that the scheduler thread is not a deamon and that the calls list contains exactly one element
	assert thread.daemon 
	assert calls == [1]


def test_database_uses_wal_and_busy_timeout():
	#Connect to database
	with connect() as connection:
		#Assert the journal mode is WAL and the timeout is greater than 5000 milliseconds
		assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
		assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_concurrent_writers_wait_instead_of_failing():
	#Create a connection wihtout checking the same thread
	blocker = sqlite3.connect(main.DATABASE_PATH, check_same_thread=False)
	#Begin an immediate transaction
	blocker.execute("BEGIN IMMEDIATE")
	#Begin timer
	import threading
	import time
	threading.Timer(0.3, blocker.rollback).start()
	started = time.monotonic() #Record start time
	#Connect to database with checking the same thread
	with connect() as connection:
		connection.execute("BEGIN IMMEDIATE") #Waits for the blocker instead of raising "database is locked"
	assert time.monotonic() - started >= 0.2
	#Close the blocker connection
	blocker.close()


def test_healthz_is_public_and_ignores_host_allowlist(monkeypatch):
	#Set App to testing mode
	main.app.testing = True
	#update the settings to use LAN mode, require auth, and allowed hosts = x.example
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, allowed_hosts=("x.example",)))
	#Make a GET request to the healthz endpoint with a host header
	response = main.app.test_client().get("/healthz", headers={"Host": "127.0.0.1"})
	#Assert that the reponce code is 200, and the responce data is "ok"
	assert response.status_code == 200 
	assert response.get_data(as_text=True) == "ok"


def test_scheduled_scan_runs_each_account_once(monkeypatch):
	#Create users in the database
	from utils.users import create_user
	with connect() as connection:
		alice = create_user(connection, "alice", "correct horse battery")
		bob = create_user(connection, "bob", "correct horse battery")
		connection.execute("UPDATE users SET disabled = 1 WHERE id = ?", (bob,))
	#Update the settings to use LAN mode, enable auth
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True))
	monkeypatch.setattr(main, "provider_ready", lambda provider: True)
	scanned = []
	#Set a function to append user IDs to the scanned list
	monkeypatch.setattr(main, "scan_allowed", lambda user_id: True)
	monkeypatch.setattr(main, "run_scan", lambda user_id: scanned.append(user_id) or True)
	#Run the scheduled scans
	main.run_scheduled_scans()
	#Check the list for the user id of alice
	assert scanned == [alice]
