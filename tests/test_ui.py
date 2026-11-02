import dataclasses

import main
from test_hardening import PASSWORD, add_product, lan, sign_in  # noqa: F401
from utils.database import connect


def page(client, path="/", **headers):
	return client.get(path, headers=headers).get_data(as_text=True)


def test_local_mode_shows_no_banner_badge_or_quota():
	body = page(main.app.test_client())
	assert "warning-notice" not in body
	assert "mode-badge" not in body
	assert "Your limits" not in body


def test_lan_http_warns_on_every_page_including_login(lan):
	assert "not encrypted" in page(sign_in())
	assert "not encrypted" in page(main.app.test_client(), "/login")
	assert "LAN mode" in page(sign_in())


def test_lan_tls_has_no_plaintext_warning(lan):
	lan(tls="manual")
	assert "not encrypted" not in page(sign_in())


def test_selfsigned_certificate_gets_an_explanation(lan):
	lan(tls="selfsigned")
	assert "self-signed certificate" in page(sign_in())


def test_public_mode_warns_only_when_not_https(lan, monkeypatch):
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="public", allowed_hosts=("a.example.com",), trusted_proxies=1))
	client = main.app.test_client()
	client.post("/login", data={"username": "alice", "password": PASSWORD}, base_url="https://a.example.com")
	assert "not delivered over HTTPS" not in client.get("/", base_url="https://a.example.com").get_data(as_text=True)
	assert "not delivered over HTTPS" in page(main.app.test_client(), "/login", Host="a.example.com")


def test_quota_panel_shows_all_limits(lan):
	lan(user_daily_api_calls=7, rechecks_per_hour=5)
	add_product(lan.alice)
	body = page(sign_in())
	assert "Your limits" in body
	assert f"1 of {main.MAX_URLS}" in body
	assert "0 of 7" in body
	assert "0 of 5" in body


def test_quota_hides_unlimited_rows(lan):
	lan(user_daily_api_calls=0, rechecks_per_hour=0)
	body = page(sign_in())
	assert "Products tracked" in body
	assert "API calls today" not in body
	assert "Re-checks this hour" not in body


def test_quota_counts_rechecks_and_api_calls(lan, monkeypatch):
	lan(rechecks_per_hour=3)
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: None)
	product_id = add_product(lan.alice)
	with connect() as connection:
		connection.execute("UPDATE products SET error = 'boom' WHERE id = ?", (product_id,))
	client = sign_in()
	client.post(f"/recheck/{product_id}")
	assert "1 of 3" in page(client)


def test_quota_is_per_user(lan):
	lan(user_daily_api_calls=7)
	with connect() as connection:
		connection.execute("INSERT INTO api_spend (user_id, day, calls) VALUES (?, date('now'), 4)", (lan.admin,))
	assert "0 of 7" in page(sign_in())
	assert "4 of 7" not in page(sign_in("boss")) #Administrators have no API budget


def test_global_api_usage_is_admin_only(lan):
	assert "API usage" not in page(sign_in())


def test_user_menu_links_depend_on_role(lan):
	member = page(sign_in())
	admin = page(sign_in("boss"))
	assert "Signed in as <strong>alice</strong>" in member
	assert "/admin/users" not in member
	assert "/admin/users" in admin
	assert "/account" in member and "Sign out" in member
