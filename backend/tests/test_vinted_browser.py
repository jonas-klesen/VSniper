from __future__ import annotations

from types import SimpleNamespace

import pytest
from selenium.webdriver.chrome.options import Options

from vsniper.integrations.vinted.browser import (
    BrowserListingChallengeError,
    BrowserListingUnconfiguredError,
    VintedBrowserClient,
)


def _settings(
    *,
    proxy_url: str = "http://proxy.example:8080",
    profile_dir: str = "/tmp/vsniper-browser-test",
) -> SimpleNamespace:
    return SimpleNamespace(
        vinted_browser_proxy_url=proxy_url,
        vinted_browser_webdriver_url="http://browser:4444/wd/hub",
        vinted_browser_timeout_seconds=1,
        vinted_browser_profile_dir=profile_dir,
    )


class _FakeDriver:
    def __init__(self, *, title: str, html: str) -> None:
        self.title = title
        self.page_source = html
        self.visited: list[str] = []
        self.cdp_calls: list[tuple[str, dict]] = []

    def execute_cdp_cmd(self, command: str, params: dict) -> None:
        self.cdp_calls.append((command, params))

    def get(self, url: str) -> None:
        self.visited.append(url)


def test_browser_fetch_attempts_listing_without_proxy(monkeypatch) -> None:
    html = '<meta property="og:title" content="Wide cargos | Vinted">'
    driver = _FakeDriver(title="Wide cargos | Vinted", html=html)
    client = VintedBrowserClient(_settings(proxy_url=""))
    monkeypatch.setattr(client, "_driver_or_create", lambda: driver)

    result = client.fetch_html("https://www.vinted.de/items/123-item", cookie_header="")

    assert result == html
    assert driver.visited == ["https://www.vinted.de/items/123-item"]


def test_empty_proxy_configuration_adds_no_chromium_proxy_argument() -> None:
    client = VintedBrowserClient(_settings(proxy_url=""))
    options = Options()

    client._configure_proxy(options)

    assert all(not argument.startswith("--proxy-server=") for argument in options.arguments)


def test_invalid_non_empty_proxy_configuration_is_rejected() -> None:
    client = VintedBrowserClient(_settings(proxy_url="proxy.example"))

    with pytest.raises(BrowserListingUnconfiguredError, match="http, https, or socks5"):
        client._configure_proxy(Options())


def test_browser_fetch_syncs_cookie_and_returns_listing_html(monkeypatch) -> None:
    html = '<meta property="og:title" content="Wide cargos | Vinted">'
    driver = _FakeDriver(title="Wide cargos | Vinted", html=html)
    client = VintedBrowserClient(_settings())
    monkeypatch.setattr(client, "_driver_or_create", lambda: driver)

    result = client.fetch_html(
        "https://www.vinted.de/items/123-item",
        cookie_header="access_token_web=token; datadome=clearance",
    )

    assert result == html
    assert driver.visited == ["https://www.vinted.de/items/123-item"]
    assert driver.cdp_calls[0][0] == "Network.setCookies"
    assert [cookie["name"] for cookie in driver.cdp_calls[0][1]["cookies"]] == [
        "access_token_web",
        "datadome",
    ]


def test_browser_fetch_leaves_challenge_session_open(monkeypatch) -> None:
    driver = _FakeDriver(
        title="Just a moment...",
        html="<html><script>window._cf_chl_opt = {}</script></html>",
    )
    client = VintedBrowserClient(_settings())
    client._driver = driver  # type: ignore[assignment]
    monkeypatch.setattr(client, "_driver_or_create", lambda: driver)

    with pytest.raises(BrowserListingChallengeError, match="Open the Vinted browser"):
        client.fetch_html("https://www.vinted.de/items/123-item", cookie_header="")

    assert client._driver is driver


def test_authenticated_proxy_secret_is_written_to_profile_not_capabilities(tmp_path) -> None:
    client = VintedBrowserClient(
        _settings(
            proxy_url="http://proxy-user:proxy-password@proxy.example:8080",
            profile_dir=str(tmp_path),
        )
    )
    options = Options()

    client._configure_proxy(options)

    assert all("proxy-user" not in argument and "proxy-password" not in argument for argument in options.arguments)
    assert "--load-extension=/home/seluser/.config/chromium/proxy-extension" in options.arguments
    background = (tmp_path / "proxy-extension" / "background.js").read_text()
    assert "proxy-user" in background
    assert "proxy-password" in background
