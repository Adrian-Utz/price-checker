"""
Production entry point.

	python wsgi.py
	picks the server for PRICE_CHECKER_MODE (same as `python main.py`)
	gunicorn -c gunicorn.conf.py wsgi:app   what the Docker image runs

Importing this module refuses to start LAN/public mode without a secret and an admin account.
"""

from main import app, serve, SETTINGS, register_server, start_background_tasks, enforce_startup_requirements

if __name__ == "__main__":
	enforce_startup_requirements()
	serve(app, SETTINGS, on_ready=start_background_tasks, on_server=register_server)
