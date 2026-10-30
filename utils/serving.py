from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from utils.config import LOCAL_HOSTNAMES, Settings

"""
Chooses and starts the web server for the selected mode, creates the self-signed LAN certificate,
works out the LAN addresses to print, and runs the optional daily scheduled scan.

	local  -> werkzeug (single user, tab-close shutdown needs a server object we can stop)
	lan    -> waitress, or werkzeug when this app serves TLS itself (waitress has no TLS)
	public -> gunicorn, one worker process with several threads, behind a reverse proxy

Last Update: 10/28/2026
Written on: 10/28/2026
Written by: AJ Utz
"""

CERT_VALID_DAYS = 825
SCHEDULER_POLL_SECONDS = 60


def resolve_server(settings: Settings) -> str:
	"""Turn PRICE_CHECKER_SERVER=auto into a concrete server name."""
	if settings.server != "auto":
		return settings.server
	if settings.mode == "public":
		return "gunicorn"
	if settings.mode == "lan" and settings.tls == "off":
		return "waitress"
	return "werkzeug"


def lan_addresses() -> list[str]:
	"""Best-effort list of this machine's private IPv4 addresses, with the default-route address first."""
	found: list[str] = []
	try:
		# Create a UDP socket and connect it to a well-known address to obtain the default route
		with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
			probe.connect(("192.0.2.1", 9)) #A UDP connect only picks a route; nothing is sent
			found.append(probe.getsockname()[0])
	except OSError:
		#IF the connection fails, skip
		pass
	try:
		#use socket.getaddrinfo to get all IPv4 addressess associated with the local host
		for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
			found.append(info[4][0])
	except OSError:
		#skip if an error occurs
		pass
	#Use a set to remove duplicate addresses and filter out private and loopback addresses
	unique = [address for address in dict.fromkeys(found) if not address.startswith("127.") and address != "0.0.0.0"]
	return unique


def access_urls(settings: Settings) -> list[str]:
	"""URLs to print at startup. A wildcard bind lists every LAN address."""
	#define the netloc part of the URL(host:port)
	netloc_port = f":{settings.port}"
	#Determine the list of hosts based on the settings
	if settings.host in {"0.0.0.0", "::"}:
		#if host is 0.0.0.0 or :: (IPv6 wildcard), use all LAN addresses
		hosts = lan_addresses() or ["localhost"]
		# If mode is lan, add the hostname to the list
		if settings.mode == "lan":
			hosts += [socket.gethostname()]
	else:
		#Otherwise use the specified host
		hosts = [settings.host]
	#Create a list of URLs by combining the scheme, hosts, and netloc
	return [f"{settings.scheme}://{host}{netloc_port}" for host in dict.fromkeys(hosts)]


def _certificate_names(settings: Settings) -> tuple[list[str], list[str]]:
	names = {"localhost", socket.gethostname()}
	addresses = {"127.0.0.1", *lan_addresses()}
	for entry in settings.allowed_hosts:
		if entry in LOCAL_HOSTNAMES or entry.startswith("*."):
			continue
		bare = entry.split(":")[0] if not entry.startswith("[") else entry.strip("[]")
		try:
			addresses.add(str(ipaddress.ip_address(bare)))
		except ValueError:
			names.add(bare)
	return sorted(names), sorted(addresses)


def create_selfsigned_certificate(settings: Settings) -> tuple[Path, Path]:
	"""Write a self-signed certificate and private key covering this host's names and LAN addresses."""
	from cryptography import x509
	from cryptography.hazmat.primitives import hashes, serialization
	from cryptography.hazmat.primitives.asymmetric import ec
	from cryptography.x509.oid import NameOID

	assert settings.tls_cert and settings.tls_key
	#Get the list of names and addresses to include in the certificate
	names, addresses = _certificate_names(settings)
	#Generate a private key using ECDSA with SECP256R1 curve
	key = ec.generate_private_key(ec.SECP256R1())
	#define the certificate subjects and now's date and time
	subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Price Checker")])
	now = dt.datetime.now(dt.timezone.utc)
	#create a list of DNS and IP SAN extensions
	alternatives = [x509.DNSName(name) for name in names] + [x509.IPAddress(ipaddress.ip_address(item)) for item in addresses]
	#Create a self-signed certificate
	certificate = (
		x509.CertificateBuilder()
		.subject_name(subject)
		.issuer_name(subject)
		.public_key(key.public_key())
		.serial_number(x509.random_serial_number())
		.not_valid_before(now - dt.timedelta(minutes=5))
		.not_valid_after(now + dt.timedelta(days=CERT_VALID_DAYS))
		.add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
		.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
		.sign(key, hashes.SHA256())
	)
	#Create the dir for the certificate and the key if they don't exist
	settings.tls_cert.parent.mkdir(parents=True, exist_ok=True)
	settings.tls_key.parent.mkdir(parents=True, exist_ok=True)
	#Write the private key to the file with no excryption
	settings.tls_key.write_bytes(key.private_bytes(
		serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
	))
	# Try to set the file permissions to 600(read/write only by the owner)
	try:
		os.chmod(settings.tls_key, 0o600)
	except OSError:
		pass
	#Write the certificate to the file in PEM format
	settings.tls_cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
	# Return the paths of the certificate and key files
	return settings.tls_cert, settings.tls_key


def ensure_tls_files(settings: Settings, *, force: bool = False) -> None:
	"""Create the self-signed certificate on first start (or when forced). Custom certificates are never touched."""
	if settings.tls != "selfsigned":
		return
	if force or not (settings.tls_cert and settings.tls_cert.is_file() and settings.tls_key and settings.tls_key.is_file()):
		create_selfsigned_certificate(settings)


def gunicorn_options(settings: Settings, on_worker_ready: Callable[[], None] | None = None) -> dict:
	"""One worker process: SQLite writers, per-user scan locks and the scheduler all live in one process.
	Concurrency comes from threads."""
	options = {
		"bind": f"{settings.host}:{settings.port}",
		"workers": 1,
		"threads": settings.threads,
		"worker_class": "gthread",
		"timeout": 120,
		"graceful_timeout": 30,
		"accesslog": "-",
		"errorlog": "-",
	}
	if os.path.isdir("/dev/shm"):
		options["worker_tmp_dir"] = "/dev/shm" #Avoids heartbeat stalls on container filesystems
	if settings.tls != "off" and settings.tls_cert and settings.tls_key:
		options["certfile"], options["keyfile"] = str(settings.tls_cert), str(settings.tls_key)
	if on_worker_ready is not None:
		options["post_worker_init"] = lambda worker: on_worker_ready()
	return options


def start_scheduler(hour: int, run_all: Callable[[], None]) -> threading.Thread | None:
	"""Run run_all once per UTC day, at or after the given hour. Returns None when scheduling is off."""
	if hour < 0:
		return None

	def loop() -> None:
		last_run: dt.date | None = None
		while True:
			now = dt.datetime.now(dt.timezone.utc)
			if now.hour >= hour and last_run != now.date():
				last_run = now.date()
				try:
					run_all()
				except Exception as error: #A failed run must not stop tomorrow's
					print(f"Scheduled scan failed: {error}")
			time.sleep(SCHEDULER_POLL_SECONDS)

	thread = threading.Thread(target=loop, name="scheduled-scans", daemon=True)
	thread.start()
	return thread


def serve(app, settings: Settings, *, on_ready: Callable[[], None] | None = None, on_server: Callable[[object], None] | None = None) -> None:
	"""Start the right server for the mode and block until it stops.

	on_ready runs once the app is about to accept requests (background tasks); on_server receives the werkzeug
	server object so tab-close shutdown can stop it.
	"""
	ensure_tls_files(settings)
	name = resolve_server(settings)
	urls = ", ".join(access_urls(settings))
	print(f"Price Checker running in {settings.mode} mode ({name}) at {urls}")
	if settings.tls == "selfsigned":
		print(f"Self-signed certificate: {settings.tls_cert} - browsers will warn once until you trust it.")
	if settings.mode == "lan" and settings.allowed_hosts == () and settings.host in {"0.0.0.0", "::"}:
		print("Tip: set PRICE_CHECKER_ALLOWED_HOSTS to the names/addresses people will use to restrict which Host headers are accepted.")
	# Choose and start the appropriate server based on the settings
	if name == "gunicorn":
		from gunicorn.app.base import BaseApplication

		class Gunicorn(BaseApplication):
			def load_config(self) -> None:
				for key, value in gunicorn_options(settings, on_ready).items():
					self.cfg.set(key, value)

			def load(self):
				return app

		Gunicorn().run()
		return

	if on_ready is not None:
		on_ready()
	if name == "waitress":
		from waitress import serve as waitress_serve

		waitress_serve(app, host=settings.host, port=settings.port, threads=settings.threads, ident=None)
		return

	from werkzeug.serving import make_server
	#Use Werkzeug's built-in server for other cases
	ssl_context = (str(settings.tls_cert), str(settings.tls_key)) if settings.tls != "off" else None
	server = make_server(settings.host, settings.port, app, threaded=True, ssl_context=ssl_context)
	if on_server is not None:
		on_server(server)
	try:
		# Block the main thread to keep the server running until it stops
		server.serve_forever()
	finally:
		#ensure the server is properly closed
		server.server_close()
