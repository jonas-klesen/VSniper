from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options

from vsniper.core.config import Settings


_logger = logging.getLogger(__name__)


class BrowserListingError(RuntimeError):
    """Base error for the optional browser-backed listing transport."""


class BrowserListingUnconfiguredError(BrowserListingError):
    pass


class BrowserListingChallengeError(BrowserListingError):
    pass


class BrowserListingUnavailableError(BrowserListingError):
    pass


class VintedBrowserClient:
    """Own one persistent visible Chromium session for one-off listing imports.

    The browser lives in the private Selenium sidecar. Its profile is persisted by
    Docker, and the same live session is exposed through noVNC so an operator can
    complete a Cloudflare challenge without copying credentials into another browser.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._driver: webdriver.Remote | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            driver, self._driver = self._driver, None
            if driver is not None:
                try:
                    driver.quit()
                except WebDriverException:
                    _logger.debug("Vinted browser session was already closed.", exc_info=True)

    def fetch_html(self, url: str, *, cookie_header: str) -> str:
        with self._lock:
            for attempt in range(2):
                try:
                    driver = self._driver_or_create()
                    self._sync_vinted_cookies(driver, cookie_header)
                    driver.get(url)
                    html = self._wait_for_listing_or_challenge(driver)
                    if self._looks_like_challenge(driver.title, html):
                        raise BrowserListingChallengeError(
                            "Vinted requires browser verification. Open the Vinted browser, "
                            "complete the challenge, then retry the import."
                        )
                    return html
                except BrowserListingChallengeError:
                    raise
                except (InvalidSessionIdException, WebDriverException) as exc:
                    self._discard_driver()
                    if attempt == 0:
                        continue
                    raise BrowserListingUnavailableError(
                        "The Vinted listing browser is unavailable. Check the browser service "
                        "and, if configured, the proxy, then retry."
                    ) from exc

        raise BrowserListingUnavailableError("The Vinted listing browser is unavailable.")

    def _driver_or_create(self) -> webdriver.Remote:
        if self._driver is not None:
            return self._driver

        options = Options()
        options.add_argument("--window-size=1440,1000")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--user-data-dir=/home/seluser/.config/chromium")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        self._configure_proxy(options)

        try:
            driver = webdriver.Remote(
                command_executor=self.settings.vinted_browser_webdriver_url,
                options=options,
            )
            driver.set_page_load_timeout(self.settings.vinted_browser_timeout_seconds)
        except WebDriverException as exc:
            raise BrowserListingUnavailableError(
                "The Vinted listing browser could not start. Check its service and, if configured, "
                "the proxy."
            ) from exc

        self._driver = driver
        return driver

    def _configure_proxy(self, options: Options) -> None:
        proxy_url = self.settings.vinted_browser_proxy_url.strip()
        if not proxy_url:
            return

        parsed = urlparse(proxy_url)
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or parsed.port is None:
            raise BrowserListingUnconfiguredError(
                "VINTED_BROWSER_PROXY_URL must be an http, https, or socks5 URL including a port."
            )

        proxy_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if parsed.username is None and parsed.password is None:
            options.add_argument(f"--proxy-server={proxy_server}")
            return
        if parsed.username is None or parsed.password is None:
            raise BrowserListingUnconfiguredError(
                "VINTED_BROWSER_PROXY_URL must include both a username and password, or neither."
            )
        if parsed.scheme == "socks5":
            raise BrowserListingUnconfiguredError(
                "Authenticated SOCKS5 proxies are not supported by Chromium; use an authenticated HTTP proxy."
            )

        extension_path = self._write_proxy_auth_extension(
            scheme=parsed.scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=unquote(parsed.username),
            password=unquote(parsed.password),
        )
        options.add_argument(f"--load-extension={extension_path}")

    def _write_proxy_auth_extension(
        self,
        *,
        scheme: str,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> str:
        manifest = {
            "manifest_version": 3,
            "name": "VSniper proxy configuration",
            "version": "1.0.0",
            "permissions": ["proxy", "storage", "webRequest", "webRequestAuthProvider"],
            "host_permissions": ["<all_urls>"],
            "background": {"service_worker": "background.js"},
        }
        background = f"""
const config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{
      scheme: {json.dumps(scheme)},
      host: {json.dumps(host)},
      port: {port}
    }},
    bypassList: ["localhost", "127.0.0.1"]
  }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}});
chrome.webRequest.onAuthRequired.addListener(
  (_details, callback) => callback({{
    authCredentials: {{
      username: {json.dumps(username)},
      password: {json.dumps(password)}
    }}
  }}),
  {{urls: ["<all_urls>"]}},
  ["asyncBlocking"]
);
"""
        host_root = Path(self.settings.vinted_browser_profile_dir)
        extension_dir = host_root / "proxy-extension"
        extension_dir.mkdir(parents=True, exist_ok=True)
        (extension_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (extension_dir / "background.js").write_text(background, encoding="utf-8")
        return "/home/seluser/.config/chromium/proxy-extension"

    @staticmethod
    def _sync_vinted_cookies(driver: webdriver.Remote, cookie_header: str) -> None:
        cookies = []
        for part in cookie_header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name and value:
                cookies.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": ".vinted.de",
                        "path": "/",
                        "secure": True,
                    }
                )
        if cookies:
            driver.execute_cdp_cmd("Network.setCookies", {"cookies": cookies})

    def _wait_for_listing_or_challenge(self, driver: webdriver.Remote) -> str:
        timeout = max(1, self.settings.vinted_browser_timeout_seconds)
        deadline = time.monotonic() + timeout
        html = driver.page_source
        while time.monotonic() < deadline:
            html = driver.page_source
            if "property=\"og:title\"" in html or "property='og:title'" in html:
                return html
            if self._looks_like_challenge(driver.title, html):
                return html
            time.sleep(0.5)
        raise TimeoutException("Timed out waiting for Vinted listing metadata.")

    @staticmethod
    def _looks_like_challenge(title: str, html: str) -> bool:
        sample = f"{title}\n{html[:50_000]}".lower()
        return any(
            marker in sample
            for marker in (
                "just a moment",
                "cf-chl-",
                "challenge-platform",
                "_cf_chl_opt",
                "cf-turnstile",
            )
        )

    def _discard_driver(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                pass
