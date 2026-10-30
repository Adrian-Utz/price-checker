"""Gunicorn settings for `gunicorn -c gunicorn.conf.py wsgi:app`, taken from the same PRICE_CHECKER_* variables."""

from utils.config import load_env_file, load_settings
from utils.serving import ensure_tls_files, gunicorn_options

load_env_file()
_settings = load_settings()
ensure_tls_files(_settings)


def _start_background_tasks() -> None:
	from main import start_background_tasks
	start_background_tasks()


globals().update(gunicorn_options(_settings, _start_background_tasks))
