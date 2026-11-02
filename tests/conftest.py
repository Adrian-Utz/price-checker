import os
import tempfile
from pathlib import Path

import dotenv
import pytest

# Must run before main/utils.database are imported so tests never touch the real database or .env mode.
dotenv.load_dotenv = lambda *args, **kwargs: False #A developer's real .env (override=True) would otherwise beat the settings below
_TEST_DIR = Path(tempfile.mkdtemp(prefix="price-checker-tests-"))
os.environ["PRICE_CHECKER_MODE"] = "local"
os.environ["PRICE_CHECKER_DATA_DIR"] = str(_TEST_DIR)
os.environ["PRICE_CHECKER_DB"] = str(_TEST_DIR / "import.sqlite3")
os.environ["PRICE_CHECKER_DEV_DB"] = str(_TEST_DIR / "import.dev.sqlite3")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
	"""Give every test its own empty, migrated database."""
	import main
	import utils.database as database

	path = tmp_path / "test.sqlite3"
	monkeypatch.setattr(database, "DATABASE_PATH", path)
	monkeypatch.setattr(main, "DATABASE_PATH", path)
	database.init_db()
	# Most tests POST without tokens; test_hardening turns CSRF back on.
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", False)
	yield path
