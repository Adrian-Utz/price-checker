from datetime import datetime, timedelta, timezone
import csv
import io
import json

import pytest

from main import cache_is_fresh, ensure_success_status, local_calendar_date, normalize_url, parse_price, price_chart, product_query
from api_clients.serpapi_client import SerpApiClient, SerpApiError, product_bulk_price, product_price, product_reference
from api_clients.unwrangle_client import product_bulk_price as unwrangle_bulk_price
from api_clients.unwrangle_client import UnwrangleClient


def setup_module():
    from main import app

    app.testing = True


def test_normalize_url_removes_fragment():
    assert normalize_url(" https://example.com/item#reviews ") == "https://example.com/item"


def test_normalize_url_rejects_non_http_urls():
    with pytest.raises(ValueError):
        normalize_url("example.com/item")


def test_parse_price_uses_lowest_visible_dollar_amount():
    assert parse_price("Was $39.99, now $24.50") == 24.50


def test_parse_price_reads_screen_reader_price_text():
    markup_text = "\\n        $129.99\\n        "
    assert parse_price(markup_text) == 129.99


def test_http_error_is_reported_before_extraction():
    with pytest.raises(RuntimeError, match="HTTP 403"):
        ensure_success_status(403)


def test_product_query_uses_product_slug_without_numeric_id():
    url = "https://www.lowes.com/pd/Dowan-Office-Chair-Black/7975119"
    assert product_query(url) == "Dowan Office Chair Black"


def test_cache_is_fresh_respects_cache_window():
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    assert cache_is_fresh(fresh)
    assert not cache_is_fresh(stale)
    assert not cache_is_fresh(None)


def test_local_calendar_date_converts_timestamp_to_computer_timezone():
    timestamp = "2026-08-21T00:30:00+00:00"
    assert local_calendar_date(timestamp) == datetime.fromisoformat(timestamp).astimezone().date()


def test_walmart_reference_extracts_product_id():
    reference = product_reference("https://www.walmart.com/ip/Example-Product/123456789")
    assert reference.retailer == "walmart"
    assert reference.product_id == "123456789"


def test_home_depot_reference_extracts_numeric_product_id():
    reference = product_reference("https://www.homedepot.com/p/Example-Drill/987654321")
    assert reference.retailer == "home_depot"
    assert reference.product_id == "987654321"


def test_walmart_provider_reads_product_result(monkeypatch):
    client = SerpApiClient("test-key")
    calls = []

    def fake_request(engine, **params):
        calls.append((engine, params))
        return {"product_result": {"title": "Example Product", "price": 12.99}}

    monkeypatch.setattr(client, "request", fake_request)
    result = client.product("https://www.walmart.com/ip/Example-Product/123456789", store_id="3081")
    assert result == {"title": "Example Product", "price": 12.99, "currency": "USD"}
    assert calls == [("walmart_product", {"product_id": "123456789", "q": "Example Product", "store_id": "3081"})]


def test_walmart_provider_reads_product_result_without_location(monkeypatch):
    client = SerpApiClient("test-key")
    monkeypatch.setattr(client, "request", lambda engine, **params: {
        "product_result": {"title": "Example Product", "price": 12.99}
    })
    result = client.product("https://www.walmart.com/ip/Example-Product/123456789")
    assert result == {"title": "Example Product", "price": 12.99, "currency": "USD"}


def test_home_depot_provider_resolves_search_result(monkeypatch):
    client = SerpApiClient("test-key")
    calls = []

    def fake_request(engine, **params):
        calls.append((engine, params))
        if engine == "home_depot":
            return {"products": [{"product_id": "987654321"}]}
        return {"products": [{"title": "Example Drill", "price": 49.99}]}

    monkeypatch.setattr(client, "request", fake_request)
    result = client.product("https://www.homedepot.com/p/Example-Drill", store_id="1950")
    assert result["price"] == 49.99
    assert [call[0] for call in calls] == ["home_depot", "home_depot_product"]
    assert calls == [
        ("home_depot", {"q": "Example Drill", "country": "us", "store_id": "1950"}),
        ("home_depot_product", {"product_id": "987654321", "q": "Example Drill", "country": "us", "store_id": "1950"}),
    ]


def test_store_name_is_read_from_provider_response(monkeypatch):
    client = SerpApiClient("test-key")
    monkeypatch.setattr(client, "request", lambda engine, **params: {
        "search_information": {"store_name": "Charlottesville"},
        "products": [{"title": "Example Drill", "price": 49.99}],
    })
    result = client.product("https://www.homedepot.com/p/Example-Drill/987654321")
    assert result["store_name"] == "Charlottesville"


def test_store_location_is_read_from_provider_response(monkeypatch):
    client = SerpApiClient("test-key")
    monkeypatch.setattr(client, "request", lambda engine, **params: {
        "search_information": {
            "store_name": "Charlottesville",
            "location": {"city": "Charlottesville", "province": "VA", "postal_code": "22901"},
        },
        "products": [{"title": "Example Drill", "price": 49.99}],
    })
    result = client.product("https://www.homedepot.com/p/Example-Drill/987654321")
    assert result["store_location"] == "Charlottesville, VA, 22901"


def test_home_depot_provider_reads_nested_current_offer_price(monkeypatch):
    client = SerpApiClient("test-key")
    monkeypatch.setattr(client, "request", lambda engine, **params: {
        "product_result": {
            "title": "Mirror Clip",
            "price_was": 19.99,
            "offers": [{"seller": "The Home Depot", "price": "$12.99"}],
        }
    })
    result = client.product("https://www.homedepot.com/p/Mirror-Clip/301942175")
    assert result["price"] == 12.99


def test_product_price_ignores_previous_price_when_current_price_missing():
    assert product_price({"price_was": 19.99, "original_price": "$24.99"}) is None


def test_bulk_price_skips_invalid_tier_above_regular_price():
    offer = {
        "bulk_price": 50,
        "bulk_quantity": 77,
        "tiers": [{"description": "Buy 50 or more $19.15", "bulk_price": "$19.15"}],
    }
    assert product_bulk_price(offer, regular_price=21.28) == (19.15, 50)


def test_bulk_price_reads_provider_specific_quantity_parameters():
    assert product_bulk_price({"bulk_price": 19.15, "bulk_price_quantity": 50}) == (19.15, 50)
    assert unwrangle_bulk_price({"bulk_price": "$19.15", "bulk_price_threshold": 50}) == (19.15, 50)


def test_lowes_provider_reads_numeric_price(monkeypatch):
    client = UnwrangleClient("test-key")
    monkeypatch.setattr(client, "request", lambda platform, **params: {
        "detail": {
            "name": "Example Lowes Product",
            "price": 12.99,
            "currency": "USD",
        }
    })
    result = client.product("https://www.lowes.com/pd/Example-Product/123456")
    assert result["price"] == 12.99


def test_lowes_provider_reads_plain_numeric_price_string(monkeypatch):
    client = UnwrangleClient("test-key")
    monkeypatch.setattr(client, "request", lambda platform, **params: {
        "detail": {"name": "Example Lowes Product", "price": "2399", "currency": "USD"}
    })
    result = client.product("https://www.lowes.com/pd/Example-Product/123456")
    assert result["price"] == 2399.0


def test_lowes_provider_reads_nested_or_list_price(monkeypatch):
    client = UnwrangleClient("test-key")
    responses = iter([
        {"detail": {"name": "Example Lowes Product", "price": {"value": "2399"}}},
        {"detail": {"name": "Example Lowes Product", "price": ["2399"]}},
    ])
    monkeypatch.setattr(client, "request", lambda platform, **params: next(responses))
    url = "https://www.lowes.com/pd/Example-Product/123456"
    assert client.product(url)["price"] == 2399.0
    assert client.product(url)["price"] == 2399.0


def test_lowes_provider_falls_back_to_list_price(monkeypatch):
    client = UnwrangleClient("test-key")
    monkeypatch.setattr(client, "request", lambda platform, **params: {
        "detail": {"name": "Example Lowes Product", "price": None, "list_price": 2399, "currency": "USD"}
    })
    result = client.product("https://www.lowes.com/pd/Example-Product/123456")
    assert result["price"] == 2399.0


def test_unwrangle_request_records_response_history_for_dev_mode(monkeypatch):
    import api_clients.unwrangle_client as unwrangle_client

    client = UnwrangleClient("test-key")
    payload = {"detail": {"name": "Example Lowes Product", "price": 12.99}}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(unwrangle_client, "urlopen", lambda endpoint, timeout: FakeResponse())
    result = client.request("lowes_detail", url="https://www.lowes.com/pd/Example-Product/123456")

    assert result == payload
    assert client.response_history == [payload]


def test_unwrangle_price_parser_reads_trailing_currency():
    from api_clients.unwrangle_client import parse_price

    assert parse_price("2399 USD") == 2399.0


def test_home_depot_provider_uses_priced_products_when_product_result_has_no_price(monkeypatch):
    client = SerpApiClient("test-key")
    monkeypatch.setattr(client, "request", lambda engine, **params: {
        "product_result": {"title": "Metadata only"},
        "products": [{"title": "OOK Mirror Clip", "price": 8.98}],
    })
    result = client.product("https://www.homedepot.com/p/OOK-Mirror-Clip/301942175")
    assert result["title"] == "OOK Mirror Clip"
    assert result["price"] == 8.98


def test_home_depot_provider_reads_product_results_envelope(monkeypatch):
    client = SerpApiClient("test-key")
    monkeypatch.setattr(client, "request", lambda engine, **params: {
        "product_results": [{"title": "OOK Mirror Clip", "offers": [{"price": {"value": 8.98}}]}]
    })
    result = client.product("https://www.homedepot.com/p/OOK-Mirror-Clip/301942175")
    assert result["price"] == 8.98


def test_serpapi_client_requires_key(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with pytest.raises(SerpApiError, match="SERPAPI_API_KEY"):
        SerpApiClient("")


def test_serpapi_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("SERPAPI_TIMEOUT_SECONDS", "120")
    client = SerpApiClient("test-key")
    assert client.timeout_seconds == 120


def test_serpapi_account_usage_reads_remaining_searches(monkeypatch):
    import api_clients.serpapi_client as serpapi_client

    client = SerpApiClient("test+key")
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "total_searches_left": 41,
                "plan_searches_left": 40,
                "searches_per_month": 250,
                "api_key": "must-not-be-returned",
            }).encode("utf-8")

    def fake_urlopen(endpoint, timeout):
        calls.append((endpoint, timeout))
        return FakeResponse()

    monkeypatch.setattr(serpapi_client, "urlopen", fake_urlopen)

    assert client.account_usage() == {"remaining": 41, "monthly_limit": 250, "monthly_used": None}
    assert calls == [("https://serpapi.com/account.json?api_key=test%2Bkey", 5)]


def test_fetch_product_caches_unwrangle_remaining_credits(monkeypatch):
    import main

    monkeypatch.setenv("PRICE_CHECKER_API_PROVIDER", "unwrangle")
    monkeypatch.setenv("UNWRANGLE_API_KEY", "test-key")
    monkeypatch.setattr(main, "DEVELOPER_MODE", False)
    monkeypatch.setitem(main.api_usage, "unwrangle_remaining", None)

    def fake_product(self, url, store_id=None, zip_state=None, zipcode=None):
        self.response_history.append({"remaining_credits": "37.5"})
        return {"price": 12.99, "currency": "USD"}

    monkeypatch.setattr(UnwrangleClient, "product", fake_product)

    assert main.fetch_product("https://www.lowes.com/pd/example/123456")["price"] == 12.99
    assert main.api_usage_summary()["unwrangle_remaining"] == 37.5


def test_home_page_displays_provider_usage(monkeypatch):
    import main

    monkeypatch.setattr(main, "serpapi_ready", lambda: True)
    monkeypatch.setattr(main, "unwrangle_ready", lambda: True)
    monkeypatch.setitem(main.api_usage, "serpapi_remaining", 41)
    monkeypatch.setitem(main.api_usage, "unwrangle_remaining", 37.5)

    response = main.app.test_client().get("/")

    assert response.status_code == 200
    assert b"41 searches left" in response.data
    assert b"37.5 credits left" in response.data


def test_home_page_explains_missing_api_setup(monkeypatch):
    from main import app

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"API key needed" in response.data
    assert b"SERPAPI_API_KEY" in response.data


def test_settings_form_persists_store_ids(monkeypatch, tmp_path):
    from main import app, ENV_FILE, read_env_file_text

    env_file = tmp_path / ".env"
    env_file.write_text("SERPAPI_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.setattr("main.ENV_FILE", env_file)
    response = app.test_client().post("/settings/env", data={
        "csrf_token": "test",
        "env_content": read_env_file_text(),
    })
    assert response.status_code == 302
    saved = env_file.read_text(encoding="utf-8")
    assert "SERPAPI_API_KEY=test-key" in saved


def test_saving_env_file_keeps_only_one_trailing_newline(monkeypatch, tmp_path):
    import main

    env_file = tmp_path / ".env"
    monkeypatch.setattr(main, "ENV_FILE", env_file)
    monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: None)
    content = "SERPAPI_API_KEY=test-key\r\nUNWRANGLE_API_KEY=another-key\r\n"

    main.save_env_file_text(content)
    main.save_env_file_text(content)

    assert env_file.read_text(encoding="utf-8") == "SERPAPI_API_KEY=test-key\nUNWRANGLE_API_KEY=another-key\n"


def test_watchlist_store_selection_is_saved_per_product(monkeypatch, tmp_path):
    from main import app, connect, init_db

    monkeypatch.setattr("main.DATABASE_PATH", tmp_path / "store-selection.sqlite3")
    init_db()
    with connect() as connection:
        product_id = connection.execute(
            "INSERT INTO products (user_id, url, source) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?)",
            ("https://www.walmart.com/ip/example/123456", "Walmart"),
        ).lastrowid

    response = app.test_client().post("/stores/add", data={
        "retailer": "Walmart",
        "store_id": "4638",
        "name": "Charlottesville",
        "location": "Charlottesville, VA",
    })
    assert response.status_code == 302
    with connect() as connection:
        assert connection.execute("SELECT name, location FROM stores WHERE store_id = ?", ("4638",)).fetchone()[:2] == ("Charlottesville", "Charlottesville, VA")

    response = app.test_client().post(f"/store/{product_id}", data={"store_id": "4638"})
    assert response.status_code == 302
    with connect() as connection:
        assert connection.execute("SELECT store_id FROM products WHERE id = ?", (product_id,)).fetchone()[0] == "4638"

    response = app.test_client().get("/")
    assert b"Store 4638" in response.data


def test_store_can_be_saved_with_only_store_number(monkeypatch, tmp_path):
    from main import app, connect, init_db

    monkeypatch.setattr("main.DATABASE_PATH", tmp_path / "number-only-store.sqlite3")
    init_db()
    response = app.test_client().post("/stores/add", data={"retailer": "Walmart", "store_id": "4638"})
    assert response.status_code == 302
    with connect() as connection:
        store = connection.execute("SELECT name, location FROM stores WHERE store_id = '4638'").fetchone()
    assert tuple(store) == ("Store 4638", "Location pending")


def test_duplicate_url_requires_explicit_override(monkeypatch, tmp_path):
    from main import app, connect, init_db

    monkeypatch.setattr("main.DATABASE_PATH", tmp_path / "duplicate.sqlite3")
    init_db()
    url = "https://www.walmart.com/ip/example/123456"
    client = app.test_client()
    client.post("/add", data={"url": url})
    response = client.post("/add", data={"url": url})
    assert b"already tracked" in client.get(response.location).data

    client.post("/add", data={"url": url, "allow_duplicate": "on"})
    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM products WHERE url = ?", (url,)).fetchone()[0] == 2


def test_developer_mode_scan_refetches_products_with_a_fresh_cached_price(monkeypatch, tmp_path):
    from main import connect, init_db, run_scan

    monkeypatch.setattr("main.DATABASE_PATH", tmp_path / "dev-mode-scan.sqlite3")
    monkeypatch.setattr("main.DEVELOPER_MODE", True)
    monkeypatch.setattr("main.MIN_DELAY_SECONDS", 0.0)
    init_db()
    fresh_checked_at = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        local_id = connection.execute("SELECT id FROM users WHERE username = 'local'").fetchone()[0]
        connection.execute(
            "INSERT INTO products (user_id, url, source, price, checked_at) VALUES (?, ?, ?, ?, ?)",
            (local_id, "https://www.walmart.com/ip/example/123456", "Walmart", 19.99, fresh_checked_at),
        )

    calls = []
    monkeypatch.setattr("main.fetch_product", lambda url, **kwargs: (calls.append(url), {"price": 19.99, "currency": "USD"})[1])
    run_scan(local_id)

    assert calls == ["https://www.walmart.com/ip/example/123456"]


def test_dev_response_viewer_and_replay_are_offline(monkeypatch, tmp_path):
    from main import app, init_dev_db, record_dev_api_responses

    dev_database = tmp_path / "response-viewer.sqlite3"
    monkeypatch.setattr("main.DEV_DATABASE_PATH", dev_database)
    monkeypatch.setattr("main.DEVELOPER_MODE", True)
    init_dev_db()
    payload = {
        "detail": {
            "name": "Test Lumber",
            "price": 21.28,
            "currency": "USD",
            "bulk_price": 19.15,
            "bulk_price_threshold": 50,
        }
    }
    record_dev_api_responses("https://www.lowes.com/pd/Test/123", "UnwrangleClient", [payload])
    app.testing = True
    client = app.test_client()

    response = client.get("/dev/responses?response_id=1")
    assert response.status_code == 200
    assert b"Raw JSON response" in response.data
    assert b"Test Lumber" in response.data

    replay = client.post("/dev/responses/1/replay")
    assert replay.status_code == 200
    assert b"Extraction debugger" in replay.data
    assert b"19.15" in replay.data
    assert b"bulk_price_threshold" in replay.data


def test_dev_response_viewer_is_hidden_when_developer_mode_is_disabled(monkeypatch):
    from main import app

    monkeypatch.setattr("main.DEVELOPER_MODE", False)
    app.testing = True
    assert app.test_client().get("/dev/responses").status_code == 404


def test_stylesheet_is_served_from_flask_static_route():
    from main import app

    response = app.test_client().get("/static/stylesheet.css")
    assert response.status_code == 200
    assert b"font-family" in response.data


def test_home_page_displays_version_number():
    from main import VERSION_NUMBER, app

    response = app.test_client().get("/")
    assert response.status_code == 200
    assert f"v{VERSION_NUMBER}".encode() in response.data


def test_dotenv_dependency_is_available():
    from dotenv import load_dotenv

    assert callable(load_dotenv)


def test_open_app_browser_can_be_disabled(monkeypatch):
    from main import open_app_browser

    opened = []
    monkeypatch.setenv("PRICE_CHECKER_OPEN_BROWSER", "0")
    monkeypatch.setattr("main.webbrowser.open", lambda *args, **kwargs: opened.append(args))
    open_app_browser("127.0.0.1", 5000)
    assert opened == []


def test_app_url_uses_http_for_localhost(monkeypatch):
    from main import app_url

    monkeypatch.delenv("PRICE_CHECKER_URL_SCHEME", raising=False)
    assert app_url("127.0.0.1", 5000) == "http://127.0.0.1:5000"


def test_app_url_respects_configured_scheme(monkeypatch):
    from main import app_url

    monkeypatch.setenv("PRICE_CHECKER_URL_SCHEME", "https")
    assert app_url("127.0.0.1", 5000) == "https://127.0.0.1:5000"


def test_price_chart_contains_price_and_date_tooltips():
    from main import app

    with app.app_context():
        chart = price_chart([
            {"price": 12.5, "observed_at": "2026-08-20T12:30:00+00:00"},
            {"price": 10.0, "observed_at": "2026-08-21T12:30:00+00:00"},
        ])
    assert chart is not None
    assert "$12.50 on 2026-08-20 12:30 UTC" in str(chart)
    assert "$10.00 on 2026-08-21 12:30 UTC" in str(chart)


def test_price_chart_main_line_has_fallback_color_for_png_export():
    chart = price_chart([
        {"price": 12.5, "observed_at": "2026-08-20T12:30:00+00:00"},
        {"price": 10.0, "observed_at": "2026-08-21T12:30:00+00:00"},
    ])

    assert chart is not None
    assert 'stroke="var(--lg-main-line, #d06b35)"' in str(chart)


def test_price_chart_shows_bulk_price_as_a_second_series():
    from main import app

    with app.app_context():
        chart = price_chart([
            {"price": 12.5, "bulk_price": 10.0, "observed_at": "2026-08-20T12:30:00+00:00"},
            {"price": 12.0, "bulk_price": 9.5, "observed_at": "2026-08-21T12:30:00+00:00"},
        ])

    assert chart is not None
    assert 'class="bulk-price-line"' in str(chart)
    assert "Bulk price $10.00 on 2026-08-20 12:30 UTC" in str(chart)
    assert "Bulk price</text>" in str(chart)


def test_refresh_product_records_bulk_price_history(monkeypatch, tmp_path):
    from main import connect, init_db, refresh_product

    monkeypatch.setattr("main.DATABASE_PATH", tmp_path / "bulk-history.sqlite3")
    init_db()
    with connect() as connection:
        product_id = connection.execute(
            "INSERT INTO products (user_id, url, source) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?)",
            ("https://www.walmart.com/ip/example/123456", "Walmart"),
        ).lastrowid
        product = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()

    monkeypatch.setattr("main.fetch_product", lambda url, **kwargs: {
        "price": 12.5, "bulk_price": 10.0, "bulk_quantity": 3, "currency": "USD",
    })
    refresh_product(product)

    with connect() as connection:
        observation = connection.execute(
            "SELECT price, bulk_price FROM observations WHERE product_id = ?", (product_id,),
        ).fetchone()
    assert tuple(observation) == (12.5, 10.0)


def test_price_chart_places_fifty_cent_price_ticks_on_left_and_dates_below():
    from main import app

    with app.app_context():
        chart = price_chart([
            {"price": 10.10, "observed_at": "2026-08-20T12:30:00+00:00"},
            {"price": 11.90, "observed_at": "2026-08-21T12:30:00+00:00"},
        ])

    assert chart is not None
    assert 'text-anchor="end">$10.00</text>' in str(chart)
    assert 'text-anchor="end">$12.00</text>' in str(chart)
    assert 'y="176" fill="#6c756b" font-size="9" text-anchor="start">2026-08-20</text>' in str(chart)
    assert 'text-anchor="end">2026-08-21</text>' in str(chart)


def test_price_chart_uses_larger_familiar_ticks_for_large_prices():
    from main import app

    with app.app_context():
        chart = price_chart([
            {"price": 800, "observed_at": "2026-08-20T12:30:00+00:00"},
            {"price": 900, "observed_at": "2026-08-21T12:30:00+00:00"},
        ])

    assert chart is not None
    assert 'text-anchor="end">$825.00</text>' in str(chart)
    assert 'text-anchor="end">$850.00</text>' in str(chart)
    assert 'text-anchor="end">$875.00</text>' in str(chart)


def test_chart_window_limits_dense_date_range_to_notable_changes(monkeypatch, tmp_path):
    from main import app, connect, init_db

    db_path = tmp_path / "chart-window.sqlite3"
    monkeypatch.setattr("main.DATABASE_PATH", db_path)
    init_db()
    with connect() as connection:
        product_id = connection.execute(
            "INSERT INTO products (user_id, url, source) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?)",
            ("https://www.walmart.com/ip/example/123456", "Walmart"),
        ).lastrowid
        for index in range(13):
            connection.execute(
                "INSERT INTO observations (product_id, price, status, observed_at) VALUES (?, ?, ?, ?)",
                (product_id, 10 + index, "success", f"2026-08-{index + 1:02d}T12:30:00+00:00"),
            )

    response = app.test_client().get(f"/chart/{product_id}?start=2026-08-01&end=2026-08-31")
    assert response.status_code == 200
    assert response.data.count(b"<circle ") == 12
    assert b"$22.00 on 2026-08-13 12:30 UTC" in response.data

    short_response = app.test_client().get(f"/chart/{product_id}?start=2026-08-06&end=2026-08-13")
    assert short_response.status_code == 200
    assert short_response.data.count(b"<circle ") == 8

    reversed_response = app.test_client().get(f"/chart/{product_id}?start=2026-08-13&end=2026-08-06")
    assert reversed_response.status_code == 400

    full_response = app.test_client().get(f"/chart/{product_id}?all=1")
    assert full_response.status_code == 200
    assert full_response.data.count(b"<circle ") == 13
    assert b'viewBox="0 0 468 180"' in full_response.data


def test_chart_window_collapses_unchanged_prices(monkeypatch, tmp_path):
    from main import app, connect, init_db

    db_path = tmp_path / "flat-chart-window.sqlite3"
    monkeypatch.setattr("main.DATABASE_PATH", db_path)
    init_db()
    with connect() as connection:
        product_id = connection.execute(
            "INSERT INTO products (user_id, url, source) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?)",
            ("https://www.walmart.com/ip/example/123456", "Walmart"),
        ).lastrowid
        for index in range(14):
            connection.execute(
                "INSERT INTO observations (product_id, price, status, observed_at) VALUES (?, ?, ?, ?)",
                (product_id, 10, "success", f"2026-08-{index + 1:02d}T12:30:00+00:00"),
            )

    response = app.test_client().get(f"/chart/{product_id}?start=2026-08-01&end=2026-08-31")
    assert response.status_code == 200
    assert response.data.count(b"<circle ") == 2


def test_price_chart_is_empty_without_successful_prices():
    assert price_chart([]) is None


def test_watchlist_collapses_price_history_by_default(monkeypatch):
    from main import app, connect

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with connect() as connection:
        connection.execute(
            "INSERT INTO products (user_id, url, source) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?)",
            ("https://www.walmart.com/ip/example/123456", "Walmart"),
        )
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"<dialog class=\"history-modal\"" in response.data
    assert b"View history" in response.data or b"No history yet" in response.data


def test_home_page_sends_shutdown_beacon_on_tab_close(monkeypatch):
    from main import app

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"navigator.sendBeacon" in response.data
    assert b"/shutdown" in response.data


def test_shutdown_endpoint_schedules_local_server_shutdown(monkeypatch):
    from main import app

    scheduled = []
    monkeypatch.setattr("main.schedule_shutdown", lambda: scheduled.append(True))
    response = app.test_client().post("/shutdown")
    assert response.status_code == 204
    assert scheduled == [True]


def test_shutdown_endpoint_rejects_non_local_request(monkeypatch):
    from main import app

    monkeypatch.setattr("main.schedule_shutdown", lambda: pytest.fail("remote shutdown must not be scheduled"))
    response = app.test_client().post("/shutdown", environ_base={"REMOTE_ADDR": "192.0.2.10"})
    assert response.status_code == 403


def test_history_modal_lists_past_prices(monkeypatch, tmp_path):
    from main import app, connect, init_db

    db_path = tmp_path / "history.sqlite3"
    monkeypatch.setattr("main.DATABASE_PATH", db_path)
    init_db()
    with connect() as connection:
        product_id = connection.execute(
            "INSERT INTO products (user_id, url, source, title) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?, ?)",
            ("https://www.walmart.com/ip/example/123456", "Walmart", "Example Item"),
        ).lastrowid
        connection.execute(
            "INSERT INTO observations (product_id, price, currency, title, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (product_id, 12.50, "USD", "Example Item", "success", None, "2026-08-20T12:30:00+00:00"),
        )
        connection.execute(
            "INSERT INTO observations (product_id, price, currency, title, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (product_id, 10.00, "USD", "Example Item", "success", None, "2026-08-21T12:30:00+00:00"),
        )

    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b'class="history-list"' in response.data
    assert b"Download PNG" in response.data
    assert b"Entire history" in response.data
    assert b"?all=1" in response.data
    assert b"exportChartImage" in response.data
    assert b"const exportScale = 4" in response.data
    assert b"$10.00 on 2026-08-21 12:30 UTC" in response.data
    assert b"$12.50 on 2026-08-20 12:30 UTC" in response.data


def test_json_export_includes_watchlist_and_history(monkeypatch, tmp_path):
    from main import app, connect, init_db

    db_path = tmp_path / "json-export.sqlite3"
    monkeypatch.setattr("main.DATABASE_PATH", db_path)
    init_db()
    with connect() as connection:
        product_id = connection.execute(
            "INSERT INTO products (user_id, url, source, title, price, currency, checked_at) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?, ?, ?, ?, ?)",
            ("https://www.walmart.com/ip/example/123456", "Walmart", "Example Item", 10.00, "USD", "2026-08-21T12:30:00+00:00"),
        ).lastrowid
        connection.execute(
            "INSERT INTO observations (product_id, price, currency, status, observed_at) VALUES (?, ?, ?, ?, ?)",
            (product_id, 12.50, "USD", "success", "2026-08-20T12:30:00+00:00"),
        )

    response = app.test_client().get("/export/json")
    payload = json.loads(response.data)
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert "attachment" in response.headers["Content-Disposition"]
    assert payload["products"][0]["id"] == product_id
    assert payload["products"][0]["product_id"] == "123456"
    assert payload["products"][0]["price"] == 10.00
    assert payload["products"][0]["history"][0]["price"] == 12.50


def test_csv_export_includes_watchlist_and_history(monkeypatch, tmp_path):
    from main import app, connect, init_db

    db_path = tmp_path / "csv-export.sqlite3"
    monkeypatch.setattr("main.DATABASE_PATH", db_path)
    init_db()
    with connect() as connection:
        product_id = connection.execute(
            "INSERT INTO products (user_id, url, source, title, price) VALUES ((SELECT id FROM users WHERE username = 'local'), ?, ?, ?, ?)",
            ("https://www.homedepot.com/p/example/987654", "Home Depot", "Example Drill", 49.99),
        ).lastrowid
        connection.execute(
            "INSERT INTO observations (product_id, price, currency, status, observed_at) VALUES (?, ?, ?, ?, ?)",
            (product_id, 54.99, "USD", "success", "2026-08-20T12:30:00+00:00"),
        )

    response = app.test_client().get("/export/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert rows[0]["id"] == str(product_id)
    assert rows[0]["product_id"] == "987654"
    assert rows[0]["title"] == "Example Drill"
    assert json.loads(rows[0]["history"])[0]["price"] == 54.99


def test_home_page_has_env_editor_button():
    from main import app

    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"Edit .env" in response.data


def test_save_env_file_updates_env(monkeypatch, tmp_path):
    from main import app

    env_file = tmp_path / ".env"
    monkeypatch.setattr("main.ENV_FILE", env_file)
    response = app.test_client().post(
        "/settings/env",
        data={"env_content": "SERPAPI_API_KEY=test-key"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert env_file.read_text(encoding="utf-8") == "SERPAPI_API_KEY=test-key\n"
    assert b".env updated successfully." in response.data


def test_api_usage_meter_shows_counts_on_hover_and_fills_by_usage(monkeypatch):
    import main

    monkeypatch.setattr(main.app, "testing", True)
    monkeypatch.setattr(main, "serpapi_ready", lambda: True)
    monkeypatch.setattr(main, "unwrangle_ready", lambda: True)
    for key, value in {
        "serpapi_remaining": 43,
        "serpapi_monthly_limit": 250,
        "serpapi_monthly_used": 75,
        "unwrangle_remaining": 65.5,
        "unwrangle_starting_credits": 100,
    }.items():
        monkeypatch.setitem(main.api_usage, key, value)

    response = main.app.test_client().get("/")

    assert response.status_code == 200
    assert b'<meter class="api-usage-track serpapi-usage"' in response.data
    assert b'<meter class="api-usage-track unwrangle-usage"' in response.data
    assert b'value="30.0"' in response.data
    assert b'value="34.5"' in response.data
    assert b"SerpApi: 43 searches left; 75 of 250 monthly searches used" in response.data
    assert b"Unwrangle: 65.5 credits left" in response.data
    assert b">43 searches left<" not in response.data
    assert b">65.5 credits left<" not in response.data


def test_unwrangle_usage_baseline_moves_up_when_balance_increases(monkeypatch):
    import main

    monkeypatch.setattr(main.app, "testing", True)
    monkeypatch.setitem(main.api_usage, "unwrangle_starting_credits", 80)
    monkeypatch.setitem(main.api_usage, "unwrangle_remaining", 45)

    main.update_unwrangle_usage([{"remaining_credits": 110}])

    assert main.api_usage["unwrangle_starting_credits"] == 110
    assert main.api_usage_summary()["unwrangle_usage_percent"] == 0

    main.update_unwrangle_usage([{"remaining_credits": 99}])
    assert main.api_usage_summary()["unwrangle_usage_percent"] == pytest.approx(10)


def test_serpapi_account_usage_includes_monthly_usage(monkeypatch):
    import api_clients.serpapi_client as serpapi_client

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "total_searches_left": 43,
                "searches_per_month": 250,
                "this_month_usage": 75,
            }).encode("utf-8")

    monkeypatch.setattr(serpapi_client, "urlopen", lambda *args, **kwargs: FakeResponse())

    assert serpapi_client.SerpApiClient("test-key").account_usage() == {
        "remaining": 43,
        "monthly_limit": 250,
        "monthly_used": 75,
    }
