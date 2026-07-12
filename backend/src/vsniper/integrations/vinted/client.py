from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx

from vsniper.core.config import get_settings
from vsniper.domain.contracts import CandidateFeature, ClothingItem, SearchRecord, SessionHealth
from vsniper.integrations.vinted.category_aliases import CATEGORY_ALIAS_IDS


SESSION_HEALTH_TTL = timedelta(minutes=15)
FILTER_OPTION_CACHE_TTL = timedelta(hours=6)
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)
VINTED_CATALOG_PER_PAGE = 96
VINTED_CURRENCY = "EUR"
VINTED_WEB_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_logger = logging.getLogger(__name__)
FilterOptionCacheKey = tuple[str, str, str, str] | tuple[str, str, str, str, str]
_ITEM_URL_RE = re.compile(r"^/items/(?P<item_id>\d+)(?:[-/].*)?$")

FILTER_ID_PARAM_BY_FIELD = {
    "brand": "brand_ids",
    "category": "catalog_ids",
    "catalog": "catalog_ids",
    "colour": "color_ids",
    "color": "color_ids",
    "condition": "status_ids",
    "status": "status_ids",
    "material": "material_ids",
    "size": "size_ids",
}

COLOR_ALIAS_IDS = {
    "black": ["1"],
    "schwarz": ["1"],
    "grey": ["3"],
    "gray": ["3"],
    "grau": ["3"],
    "white": ["12"],
    "weiß": ["12"],
    "weiss": ["12"],
    "cream": ["20"],
    "creme": ["20"],
    "beige": ["4"],
    "brown": ["2"],
    "braun": ["2"],
    "blue": ["9"],
    "blau": ["9"],
    "navy": ["27"],
    "green": ["10"],
    "grün": ["10"],
    "gruen": ["10"],
    "khaki": ["16"],
    "red": ["7"],
    "rot": ["7"],
    "yellow": ["8"],
    "gelb": ["8"],
}

STATUS_ALIAS_IDS = {
    "new with tags": ["6"],
    "neu mit etikett": ["6"],
    "new": ["1"],
    "neu": ["1"],
    "very good": ["2"],
    "sehr gut": ["2"],
    "good": ["3"],
    "gut": ["3"],
    "satisfactory": ["4"],
    "zufriedenstellend": ["4"],
}

MATERIAL_ALIAS_IDS = {
    "wool": ["46"],
    "wolle": ["46"],
    "leather": ["43"],
    "leder": ["43"],
    "suede": ["298"],
    "wildleder": ["298"],
    "cotton": ["44"],
    "baumwolle": ["44"],
    "cashmere": ["47"],
    "kaschmir": ["47"],
    "alpaca": ["122"],
    "alpaka": ["122"],
}


class _ListingPageMetadataParser(HTMLParser):
    """Collect the stable metadata Vinted renders in every listing page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.canonical_url: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value for name, value in attrs if value is not None}
        if tag == "meta":
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if key and content and key not in self.metadata:
                self.metadata[key.lower()] = content
        elif tag == "link" and attributes.get("rel", "").lower() == "canonical":
            self.canonical_url = attributes.get("href")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()


class VintedClientError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class VintedConfigurationError(VintedClientError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class VintedSessionError(VintedClientError):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None) -> None:
        super().__init__(message, retryable=retryable, status_code=status_code)


class VintedSearchError(VintedClientError):
    pass


class VintedListingUrlError(VintedClientError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class VintedClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        on_tokens_refreshed: Callable[[str, str], None] | None = None,
    ) -> None:
        self.settings = get_settings()
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout = timeout
        self._client = client
        self._session_health_cache: dict[str, SessionHealth] = {}
        self._filter_option_cache: dict[FilterOptionCacheKey, tuple[datetime, list[dict[str, Any]]]] = {}
        self._cookie_override: str | None = None
        self._refresh_token: str | None = None
        self._on_tokens_refreshed = on_tokens_refreshed
        self._generated_anon_id = str(uuid4())
        self._last_token_refresh_error: str | None = None
        # Parallel scan threads share one client; serialize refreshes so rotating refresh
        # tokens are not clobbered by two simultaneous refreshes (which kills the session).
        self._token_refresh_lock = threading.Lock()
        # Disabled on throwaway validation probes: refreshing rotates (and upstream-invalidates)
        # the refresh token inside the very cookie being validated, which would break it on save.
        self._allow_token_refresh = True

    def set_cookie(self, value: str) -> None:
        new_cookie = value.strip() or None
        if new_cookie != self._cookie_override:
            self._generated_anon_id = str(uuid4())
        self._cookie_override = new_cookie
        self._session_health_cache.clear()
        self._last_token_refresh_error = None
        if self._cookie_override:
            parsed = self._extract_named_token(self._cookie_override, "refresh_token_web")
            # Always realign the refresh token with the new cookie. If the new cookie has no
            # refresh_token_web, clear the old one so a later refresh can't persist a stale
            # token over the fresh credentials.
            self._refresh_token = parsed or None

    def set_refresh_token(self, value: str) -> None:
        self._refresh_token = value.strip() or None
        self._last_token_refresh_error = None

    def sync_persisted_credentials(self, cookie: str, refresh_token: str) -> None:
        """Re-apply DB-persisted credentials onto the long-lived client.

        Runs under the token-refresh lock and skips when nothing changed, so a scan
        thread can't clobber a refresh_token another thread just rotated (and persisted)
        with a stale pre-rotation snapshot. The DB stays the source of truth, so even if a
        rare interleaving slips a stale value through, the next scan re-syncs from it.
        """
        new_cookie = cookie.strip() or None
        new_refresh = refresh_token.strip() or None
        with self._token_refresh_lock:
            if new_cookie == self._cookie_override and new_refresh == self._refresh_token:
                return
            self.set_cookie(cookie)
            self.set_refresh_token(refresh_token)

    def validate_cookie(self, cookie: str, *, region: str | None = None) -> SessionHealth:
        """Validate an arbitrary cookie against upstream on a fully isolated throwaway client.

        Probing on a separate instance (rather than snapshot/restore of the live client)
        guarantees the probe can never (a) mutate the shared client's cookie/refresh token while
        a scan thread is mid-request, or (b) persist rotated tokens to the DB — the throwaway has
        no ``on_tokens_refreshed`` callback. Token refresh is also disabled on the probe: a
        refresh would rotate and upstream-invalidate the refresh token inside the very cookie the
        user is about to save. The HTTP transport (``self._client``) is shared so tests and any
        connection reuse still apply; only the mutable credential state is isolated.
        """
        probe = VintedClient(base_url=self.base_url, timeout=self.timeout, client=self._client)
        probe._allow_token_refresh = False
        probe.set_cookie(cookie)
        return probe.get_session_health(region=region, force=True)

    def _effective_cookie(self) -> str:
        if self._cookie_override is not None:
            return self._cookie_override
        return self.settings.vinted_cookie

    def fetch_user_sizes(self, *, region: str | None = None) -> list[str]:
        text = self._fetch_personalization_sizes_text(region=region)
        return self._parse_user_selected_sizes(text)

    def fetch_user_sizes_grouped(self, *, region: str | None = None) -> list[dict[str, str]]:
        """Each selected size title enriched with the Vinted size-group description it
        belongs to (e.g. "Schuhe Männer", "Hosen für Herren"). Vinted's personalization page
        lists a user's sizes across every category in one flat list; the group description
        is what lets a caller apply a size only to the matching clothing item instead of the
        same size list to every search regardless of category.
        """
        text = self._fetch_personalization_sizes_text(region=region)
        group_by_size_id = self._parse_size_group_descriptions(text)
        grouped: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for size_id, title in self._parse_user_selected_sizes_with_ids(text):
            description = group_by_size_id.get(size_id)
            if description is None:
                continue
            key = (title, description)
            if key in seen:
                continue
            seen.add(key)
            grouped.append({"title": title, "group_description": description})
        return grouped

    def _fetch_personalization_sizes_text(self, *, region: str | None = None) -> str:
        # Vinted's REST API does not expose size preferences. They are only
        # available in the Next.js RSC payload of the personalization page.
        region = (region or self.settings.vinted_region).strip().lower()
        self._maybe_refresh_token()

        url = f"{self._base_url_for_region(region)}/member/personalization/sizes/mens"
        headers = self._request_headers(region=region)
        headers["Accept"] = "text/x-component"
        headers["RSC"] = "1"
        cookie_header = self._cookie_header_value_or_raise()
        self._attach_cookie_headers(headers, cookie_header)

        try:
            if self._client is not None:
                response = self._client.request("GET", url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    response = client.request("GET", url, headers=headers)
        except httpx.TimeoutException as exc:
            raise VintedSearchError("Vinted request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise VintedSearchError(f"Vinted request failed: {exc}", retryable=True) from exc

        if response.status_code in {401, 403}:
            raise VintedSessionError(
                "Vinted rejected the configured cookie/session.",
                retryable=False,
                status_code=response.status_code,
            )
        if not response.is_success:
            raise VintedSearchError(
                f"Failed to fetch size preferences: HTTP {response.status_code}",
                retryable=False,
                status_code=response.status_code,
            )
        return response.text

    @staticmethod
    def _parse_user_selected_sizes(text: str) -> list[str]:
        sizes: list[str] = []
        for _, title in VintedClient._parse_user_selected_sizes_with_ids(text):
            if title not in sizes:
                sizes.append(title)
        return sizes

    @staticmethod
    def _parse_user_selected_sizes_with_ids(text: str) -> list[tuple[int, str]]:
        sizes: list[tuple[int, str]] = []
        search_key = '"userSelectedSizes":'
        pos = 0
        while True:
            idx = text.find(search_key, pos)
            if idx == -1:
                break
            bracket_start = text.find("[", idx + len(search_key))
            if bracket_start == -1:
                break
            depth = 0
            bracket_end = bracket_start
            for i in range(bracket_start, len(text)):
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        bracket_end = i
                        break
            try:
                arr = json.loads(text[bracket_start : bracket_end + 1])
            except (json.JSONDecodeError, ValueError):
                pos = bracket_start + 1
                continue
            for s in arr:
                if isinstance(s, dict):
                    size_id = s.get("id")
                    title = s.get("title")
                    if isinstance(size_id, int) and title and isinstance(title, str):
                        sizes.append((size_id, title))
            pos = bracket_end + 1
        return sizes

    # Matches each entry of the RSC payload's top-level `sizeGroups` listing, e.g.
    # {"id":38,"caption_code":10,"caption":"Größe","description":"Schuhe Männer","size_ids":[...]}.
    # `description` is the human-readable category (shoes, trousers, shirts, ...) that a
    # selected size id belongs to; it is what we need to apply sizes per clothing item.
    _SIZE_GROUP_RE = re.compile(
        r'\{"id":\d+,"caption_code":\d+,"caption":"[^"]*","description":"([^"]*)","size_ids":\[([^\]]*)\]'
    )

    @classmethod
    def _parse_size_group_descriptions(cls, text: str) -> dict[int, str]:
        group_by_size_id: dict[int, str] = {}
        for match in cls._SIZE_GROUP_RE.finditer(text):
            description, size_ids_blob = match.groups()
            for raw_id in size_ids_blob.split(","):
                raw_id = raw_id.strip()
                if raw_id.isdigit():
                    group_by_size_id.setdefault(int(raw_id), description)
        return group_by_size_id

    def get_session_health(self, *, region: str | None = None, force: bool = False) -> SessionHealth:
        region = (region or self.settings.vinted_region).strip().lower()
        now = datetime.now(UTC)
        cached = self._session_health_cache.get(region)
        if not force and cached is not None and cached.last_validated_at is not None:
            if cached.last_validated_at >= now - SESSION_HEALTH_TTL:
                return cached

        cookie_header = self._cookie_header_value()
        if cookie_header is None:
            health = SessionHealth(
                region=region,
                status="missing",
                last_validated_at=None,
                detail="Vinted credentials are missing from the environment. Add a valid cookie to enable live scans.",
            )
            self._session_health_cache[region] = health
            return health

        cookie_expiry = self._extract_cookie_expiry()
        if cookie_expiry is not None and cookie_expiry <= now:
            health = SessionHealth(
                region=region,
                status="warning",
                last_validated_at=now,
                detail=f"The configured Vinted cookie appears to be expired locally (exp {cookie_expiry.isoformat()}).",
            )
            self._session_health_cache[region] = health
            return health

        transient = False
        try:
            payload = self._request("GET", region=region, path="/api/v2/users/current", auth_required=True)
        except VintedClientError as exc:
            transient = exc.retryable
            detail = str(exc)
            if cookie_expiry is not None:
                detail = f"{detail} Cookie exp={cookie_expiry.isoformat()}."
            cookie_quality_detail = self._cookie_quality_detail(cookie_header)
            if cookie_quality_detail:
                detail = f"{detail} {cookie_quality_detail}"
            health = SessionHealth(region=region, status="warning", last_validated_at=now, detail=detail)
        else:
            user = payload.get("user") if isinstance(payload, dict) else None
            if not isinstance(user, dict):
                user = payload if isinstance(payload, dict) else {}
            username = self._first_non_empty(user.get("login"), user.get("username"), user.get("id"))
            detail = f"Vinted session validated against upstream for region '{region}'."
            if username:
                detail += f" Authenticated as {username}."
            if cookie_expiry is not None:
                detail += f" Cookie exp={cookie_expiry.isoformat()}."
            cookie_quality_detail = self._cookie_quality_detail(cookie_header)
            if cookie_quality_detail:
                detail += f" {cookie_quality_detail}"
            health = SessionHealth(region=region, status="healthy", last_validated_at=now, detail=detail)

        # Don't poison the cache for the full TTL on a transient blip (timeout / 5xx / 429):
        # leave the previous (or no) cached value so the next scan re-validates immediately.
        if not transient:
            self._session_health_cache[region] = health
        return health

    def run_search(self, search: SearchRecord, *, validate_session: bool = True) -> list[dict[str, Any]]:
        if validate_session:
            health = self.get_session_health(region=search.region)
            if health.status != "healthy":
                raise VintedSessionError(health.detail, retryable=False)

        payload = self._request(
            "GET",
            region=search.region,
            path="/api/v2/catalog/items",
            params=self._build_search_params(search),
        )
        items = self._extract_items(payload)
        normalized: list[dict[str, Any]] = []
        for item in items:
            try:
                normalized.append(self._normalise_item(item, search))
            except ValueError:
                continue
        return normalized

    def fetch_item_by_url(self, url: str, *, clothing_item: ClothingItem, region: str = "de") -> dict[str, Any]:
        item_id = self._item_id_from_url(url)
        item = self._fetch_listing_page(
            path=self._listing_path_from_url(url),
            item_id=item_id,
            region=region,
        )

        search = SearchRecord(
            id=f"offer-{item_id}",
            name="Offer by URL",
            enabled=True,
            clothing_item=clothing_item,
            query="",
            region=region,
            filters=[],
        )
        return self._normalise_item(item, search)

    def _fetch_listing_page(self, *, path: str, item_id: str, region: str) -> dict[str, Any]:
        """Load an item from Vinted's current server-rendered listing page.

        The old ``/api/v2/items/<id>`` endpoint now serves Vinted's HTML 404 page,
        even for active listings. The public item page is the supported source for
        this one-off import flow and includes Open Graph metadata before client-side
        JavaScript executes.
        """
        url = f"{self._base_url_for_region(region)}{path}"
        headers = self._request_headers(region=region)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers["Referer"] = f"{self._base_url_for_region(region)}/catalog"
        cookie_header = self._cookie_header_value()
        if cookie_header is not None:
            self._attach_cookie_headers(headers, cookie_header)

        response = self._get_listing_page_response(url, headers)
        # A stale authenticated session is redirected to Vinted's cookie-expiry
        # page, even though item pages themselves are publicly readable. Retry
        # without the cookie rather than making Add by URL depend on scan-session
        # health.
        redirect_target = response.headers.get("location", "")
        if cookie_header is not None and (
            response.status_code in {401, 403}
            or "/web/api/auth/expire-cookies" in redirect_target
        ):
            anonymous_headers = self._request_headers(region=region)
            anonymous_headers["Accept"] = headers["Accept"]
            anonymous_headers["Referer"] = headers["Referer"]
            response = self._get_listing_page_response(url, anonymous_headers)

        if not response.is_success:
            raise VintedSearchError(
                self._non_success_detail(method="GET", path=path, response=response, cookie_header=cookie_header),
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            )

        parser = _ListingPageMetadataParser()
        parser.feed(response.text)
        parser.close()
        title = parser.metadata.get("og:title") or parser.title
        title = re.sub(r"\s*\|\s*Vinted\s*$", "", title, flags=re.IGNORECASE).strip()
        if not title:
            raise VintedListingUrlError("Vinted could not find a listing at that URL.")

        canonical_url = parser.canonical_url or url
        image_url = parser.metadata.get("og:image")
        return {
            "id": item_id,
            "title": title,
            "description": parser.metadata.get("og:description") or parser.metadata.get("description") or "",
            "url": canonical_url,
            "photos": [{"url": image_url}] if image_url else [],
            "source": "listing_page_metadata",
        }

    def _get_listing_page_response(self, url: str, headers: dict[str, str]) -> httpx.Response:
        try:
            if self._client is not None:
                return self._client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    return client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise VintedSearchError("Vinted request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise VintedSearchError(f"Vinted request failed: {exc}", retryable=True) from exc

    def search_brands(self, query: str, *, region: str = "de") -> list[dict[str, str]]:
        """Look up Vinted's own brand catalog for autocomplete, e.g. for a blocked-brands picker."""
        text = query.strip()
        if not text:
            return []
        payload = self._request(
            "GET",
            region=region,
            path="/api/v2/catalog/filters/search",
            params={
                "search_text": "",
                "currency": VINTED_CURRENCY,
                "order": "newest_first",
                "filter_search_code": "brand",
                "filter_search_text": text,
            },
        )
        options = payload.get("options")
        flattened = self._flatten_filter_options([o for o in options if isinstance(o, dict)]) if isinstance(options, list) else []
        results: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for option in flattened:
            option_id = option.get("id")
            title = self._first_non_empty(option.get("title"), option.get("code"))
            if option_id is None or title is None:
                continue
            str_id = str(option_id)
            if str_id in seen_ids:
                continue
            seen_ids.add(str_id)
            results.append({"id": str_id, "title": title})
        return results

    @staticmethod
    def _item_id_from_url(url: str) -> str:
        path = VintedClient._listing_path_from_url(url)
        match = _ITEM_URL_RE.match(path)
        if match is None:
            raise VintedListingUrlError("Vinted URL must point to a listing under /items/<id>.")
        return match.group("item_id")

    @staticmethod
    def _listing_path_from_url(url: str) -> str:
        value = url.strip()
        # Browsers commonly show or copy URLs without their scheme. Treat a
        # domain/path pasted that way as HTTPS, which is how Vinted serves
        # listing pages, before applying the normal listing validation.
        if "://" not in value:
            value = f"https://{value}"
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise VintedListingUrlError("Enter a full Vinted listing URL.")
        return parsed.path.rstrip("/")

    def _request(
        self,
        method: str,
        *,
        region: str,
        path: str,
        params: dict[str, Any] | None = None,
        auth_required: bool = False,
    ) -> dict[str, Any]:
        if auth_required or self._cookie_is_expired():
            self._maybe_refresh_token()

        url = f"{self._base_url_for_region(region)}{path}"
        headers = self._request_headers(region=region)
        cookie_header = self._cookie_header_value()
        if auth_required:
            cookie_header = self._cookie_header_value_or_raise()
        elif self._cookie_is_expired():
            cookie_header = None
        if cookie_header is not None:
            self._attach_cookie_headers(headers, cookie_header)

        response: httpx.Response
        try:
            if self._client is not None:
                response = self._client.request(method, url, params=params, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    response = client.request(method, url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise VintedSearchError("Vinted request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise VintedSearchError(f"Vinted request failed: {exc}", retryable=True) from exc

        if response.status_code in {401, 403}:
            detail = "Vinted rejected the configured cookie/session."
            if response.status_code == 403 and self._looks_like_datadome_block(response):
                detail += " The response looks like a DataDome challenge/block; refresh the browser session cookie."
            if cookie_header is not None:
                cookie_quality_detail = self._cookie_quality_detail(cookie_header)
                if cookie_quality_detail:
                    detail += f" {cookie_quality_detail}"
            raise VintedSessionError(detail, retryable=False, status_code=response.status_code)
        if response.status_code == 406:
            detail = "Vinted returned 406 Not Acceptable."
            if self._looks_like_datadome_block(response):
                detail += " The response looks like a DataDome challenge/block; refresh the browser session cookie and ensure the datadome cookie is fresh."
            else:
                detail += " This usually means the request is missing required browser fingerprint headers. Refresh the Vinted cookie from an authenticated browser tab."
            raise VintedSessionError(detail, retryable=False, status_code=response.status_code)
        if response.status_code == 429:
            raise VintedSearchError("Vinted rate-limited the request.", retryable=True, status_code=response.status_code)
        if response.status_code >= 500:
            raise VintedSearchError("Vinted returned an upstream server error.", retryable=True, status_code=response.status_code)
        if not response.is_success:
            raise VintedSearchError(
                self._non_success_detail(method=method, path=path, response=response, cookie_header=cookie_header),
                retryable=False,
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise VintedSearchError("Vinted returned a non-JSON response.", retryable=True) from exc
        if not isinstance(payload, dict):
            raise VintedSearchError("Vinted returned an unexpected response payload.", retryable=False)
        return payload

    def _non_success_detail(
        self,
        *,
        method: str,
        path: str,
        response: httpx.Response,
        cookie_header: str | None,
    ) -> str:
        body_preview = response.text[:500].strip()
        if not body_preview:
            body_preview = "<empty body>"
        detail = (
            f"Vinted request failed with status {response.status_code} {response.reason_phrase} "
            f"while calling {method} {path}. Response body: {body_preview}"
        )
        if self._last_token_refresh_error:
            detail += f" Last token refresh failed: {self._last_token_refresh_error}."
        if cookie_header is not None:
            cookie_quality_detail = self._cookie_quality_detail(cookie_header)
            if cookie_quality_detail:
                detail += f" {cookie_quality_detail}"
        if response.status_code in {406, 418, 419} or self._last_token_refresh_error:
            detail += " Refresh the Vinted credentials in Settings with a fresh full Cookie header from an authenticated browser tab."
        return detail

    def _build_search_params(self, search: SearchRecord) -> dict[str, Any]:
        # A real browser starts a fresh search/browse session per search action; minting these
        # once per VintedClient instance instead (a long-lived api/worker process) reuses the
        # same ids across thousands of unrelated searches over hours, which Vinted's anti-bot
        # system eventually soft-blocks (200 OK, but zero items) rather than rejecting outright.
        params: dict[str, Any] = {
            "search_text": search.query,
            "page": 1,
            "per_page": VINTED_CATALOG_PER_PAGE,
            "time": int(datetime.now(UTC).timestamp()),
            "search_session_id": str(uuid4()),
            "global_catalog_browse_session_id": str(uuid4()),
            "currency": VINTED_CURRENCY,
            "order": "newest_first",
        }
        # Category/catalog resolution is purely static (CATEGORY_ALIAS_IDS) and never depends on
        # anything else in `params`, whereas dynamic facet-based resolution for other id-based
        # fields (size, brand) only scopes itself to catalog_ids if it's already in `params`.
        # Resolve category/catalog filters first regardless of their stored position in
        # search.filters — callers (e.g. SearchService._apply_size_filter) can and do prepend a
        # size filter ahead of category, which would otherwise leave size lookups unscoped.
        ordered_filters = sorted(
            search.filters,
            key=lambda f: 0 if f.field.strip().lower() in {"category", "catalog"} else 1,
        )
        for filter_item in ordered_filters:
            values = [value.strip() for value in filter_item.values if value and value.strip()]
            if not values:
                continue
            field = filter_item.field.strip().lower()

            # Search keyword filters are local draft metadata, not Vinted catalog params.
            # The query itself remains the only upstream keyword search input.
            if field in {"keyword", "keywords"}:
                continue

            # Vinted's catalog API has no native exclude param for these fields; forwarding an
            # exclude filter as a positive include would return exactly the items it should remove.
            if filter_item.mode == "exclude":
                continue

            if filter_item.field == "price" and filter_item.mode == "range":
                non_empty = [v for v in values if str(v).strip()]
                if len(non_empty) >= 2:
                    params["price_from"] = non_empty[0]
                    params["price_to"] = non_empty[1]
                elif len(non_empty) == 1:
                    # Position determines semantics: if the non-empty value was first of
                    # two elements (second was empty/omitted) it is a lower bound (from);
                    # otherwise it is a price ceiling (single-value with no empty placeholder).
                    if len(values) >= 2 and not str(values[-1]).strip():
                        params["price_from"] = non_empty[0]
                    else:
                        params["price_to"] = non_empty[0]
                continue

            id_param = FILTER_ID_PARAM_BY_FIELD.get(field)
            if id_param:
                resolved_ids = self._resolve_filter_ids(
                    field=field,
                    values=values,
                    search=search,
                    current_params=params,
                )
                if resolved_ids:
                    params[id_param] = ",".join(resolved_ids)
                continue

            if filter_item.mode == "exact" and len(values) == 1:
                params[filter_item.field] = values[0]
                continue

            params[f"filter[{filter_item.field}]"] = ",".join(values)

        return params

    def _resolve_filter_ids(
        self,
        *,
        field: str,
        values: list[str],
        search: SearchRecord,
        current_params: dict[str, Any],
    ) -> list[str]:
        field = self._normalise_filter_field(field)
        resolved: list[str] = []
        for value in values:
            normalized = self._normalise_lookup_text(value)

            static_ids = self._static_filter_ids(field=field, normalized_value=normalized)
            if static_ids:
                resolved.extend(static_ids)
                continue

            option_ids = self._resolve_dynamic_filter_ids(
                field=field,
                value=value,
                search=search,
                current_params=current_params,
            )
            resolved.extend(option_ids)

        return list(dict.fromkeys(resolved))

    @staticmethod
    def _static_filter_ids(*, field: str, normalized_value: str) -> list[str]:
        field = VintedClient._normalise_filter_field(field)
        if field in {"category", "catalog"}:
            return CATEGORY_ALIAS_IDS.get(normalized_value, [])
        if field in {"colour", "color"}:
            return COLOR_ALIAS_IDS.get(normalized_value, [])
        if field in {"condition", "status"}:
            return STATUS_ALIAS_IDS.get(normalized_value, [])
        if field == "material":
            return MATERIAL_ALIAS_IDS.get(normalized_value, [])
        return []

    def _resolve_dynamic_filter_ids(
        self,
        *,
        field: str,
        value: str,
        search: SearchRecord,
        current_params: dict[str, Any],
    ) -> list[str]:
        field = self._normalise_filter_field(field)
        if field in {"category", "catalog"}:
            _logger.warning(
                "Skipping unresolved Vinted category filter value %r for search %s; category facets are unsupported",
                value,
                search.id,
            )
            return []

        if field == "brand":
            options = self._search_filter_options(
                field=field,
                value=value,
                search=search,
                current_params=current_params,
            )
        else:
            options = self._facet_filter_options(field=field, search=search, current_params=current_params)

        normalized_value = self._normalise_lookup_text(value)
        matches: list[str] = []
        for option in self._flatten_filter_options(options):
            option_id = option.get("id")
            title = self._first_non_empty(option.get("title"), option.get("code"))
            if option_id is None or title is None:
                continue
            normalized_title = self._normalise_lookup_text(title)
            title_prefix = self._normalise_lookup_text(title.split("/", maxsplit=1)[0])
            if normalized_value in {normalized_title, title_prefix}:
                matches.append(str(option_id))

        if matches:
            return matches

        for option in self._flatten_filter_options(options):
            option_id = option.get("id")
            title = self._first_non_empty(option.get("title"), option.get("code"))
            if option_id is None or title is None:
                continue
            if normalized_value in self._normalise_lookup_text(title):
                return [str(option_id)]
        return []

    def _search_filter_options(
        self,
        *,
        field: str,
        value: str,
        search: SearchRecord,
        current_params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        params = self._filter_context_params(search=search, current_params=current_params)
        params["filter_search_code"] = field
        params["filter_search_text"] = value
        cache_key = (
            search.region,
            field,
            value,
            str(params.get("search_text") or ""),
            str(params.get("catalog_ids") or ""),
        )
        now = datetime.now(UTC)
        cached = self._filter_option_cache.get(cache_key)
        if cached is not None and cached[0] >= now - FILTER_OPTION_CACHE_TTL:
            return cached[1]
        payload = self._request(
            "GET",
            region=search.region,
            path="/api/v2/catalog/filters/search",
            params=params,
        )
        options = payload.get("options")
        result = [item for item in options if isinstance(item, dict)] if isinstance(options, list) else []
        self._filter_option_cache[cache_key] = (now, result)
        return result

    def _facet_filter_options(
        self,
        *,
        field: str,
        search: SearchRecord,
        current_params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        field = self._normalise_filter_field(field)
        if field in {"category", "catalog"}:
            _logger.warning("Skipping Vinted category facets for search %s; category facets are unsupported", search.id)
            return []

        params = self._filter_context_params(search=search, current_params=current_params)
        params["filter_code"] = "color" if field == "colour" else "status" if field == "condition" else field
        cache_key = (
            search.region,
            params["filter_code"],
            str(params.get("search_text") or ""),
            str(params.get("catalog_ids") or ""),
        )
        now = datetime.now(UTC)
        cached = self._filter_option_cache.get(cache_key)
        if cached is not None and cached[0] >= now - FILTER_OPTION_CACHE_TTL:
            return cached[1]

        payload = self._request(
            "GET",
            region=search.region,
            path="/api/v2/catalog/filters/facets",
            params=params,
        )
        options = payload.get("options")
        result = [item for item in options if isinstance(item, dict)] if isinstance(options, list) else []
        self._filter_option_cache[cache_key] = (now, result)
        return result

    @staticmethod
    def _filter_context_params(*, search: SearchRecord, current_params: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "search_text": search.query,
            "order": current_params.get("order", "newest_first"),
            "currency": current_params.get("currency", VINTED_CURRENCY),
        }
        for key in ("catalog_ids", "price_from", "price_to", "currency"):
            if current_params.get(key):
                params[key] = current_params[key]
        return params

    @classmethod
    def _flatten_filter_options(cls, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for option in options:
            flattened.append(option)
            nested = option.get("options")
            if isinstance(nested, list):
                flattened.extend(cls._flatten_filter_options([item for item in nested if isinstance(item, dict)]))
        return flattened

    @staticmethod
    def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("items", "catalog_items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _normalise_item(self, item: dict[str, Any], search: SearchRecord) -> dict[str, Any]:
        external_item_id = self._first_non_empty(item.get("id"), item.get("item_id"), item.get("external_item_id"))
        if not external_item_id:
            raise ValueError("Listing payload is missing an id")

        title = self._first_non_empty(item.get("title"), item.get("name"), item.get("description_title")) or f"Vinted item {external_item_id}"
        brand = self._extract_brand(item)
        description = self._extract_description(item)
        price_eur = self._extract_price(item)
        size = self._first_non_empty(item.get("size_title"), item.get("size"), self._nested_value(item, "size", "title")) or "unknown"
        url = self._extract_url(item, region=search.region, external_item_id=str(external_item_id))
        image_urls = self._extract_image_urls(item, region=search.region)
        created_at = self._extract_created_at(item)
        features = self._extract_features(title=title, brand=brand, description=description)

        return {
            "id": str(external_item_id),
            "external_item_id": str(external_item_id),
            "title": title,
            "brand": brand,
            "price_eur": price_eur,
            "size": size,
            "url": url,
            "image_urls": image_urls,
            "created_at": created_at,
            "description": description,
            "features": features,
            "raw_listing": item,
        }

    @staticmethod
    def _extract_brand(item: dict[str, Any]) -> str:
        brand = VintedClient._first_non_empty(
            item.get("brand_title"),
            VintedClient._nested_value(item, "brand", "title"),
            VintedClient._nested_value(item, "brand_dto", "title"),
            item.get("brand"),
        )
        return brand or "Unknown"

    @staticmethod
    def _extract_description(item: dict[str, Any]) -> str:
        return VintedClient._first_non_empty(item.get("description"), item.get("item_description"), item.get("description_plain")) or ""

    @staticmethod
    def _extract_price(item: dict[str, Any]) -> float:
        candidates = [
            item.get("price_numeric"),
            VintedClient._nested_value(item, "price", "numeric"),
            VintedClient._nested_value(item, "price", "amount"),
            VintedClient._nested_value(item, "total_item_price", "amount"),
            item.get("price_eur"),
            item.get("price"),
        ]
        for candidate in candidates:
            value = VintedClient._coerce_float(candidate)
            if value is not None:
                return value
        return 0.0

    def _extract_url(self, item: dict[str, Any], *, region: str, external_item_id: str) -> str:
        url = self._first_non_empty(item.get("url"), item.get("item_url"), item.get("path"), item.get("item_path"))
        if url:
            if url.startswith("http://") or url.startswith("https://"):
                return url
            return urljoin(f"{self._base_url_for_region(region)}/", url.lstrip("/"))
        return f"{self._base_url_for_region(region)}/items/{external_item_id}"

    def _extract_image_urls(self, item: dict[str, Any], *, region: str) -> list[str]:
        photos = item.get("photos") or item.get("images") or []
        urls: list[str] = []
        if isinstance(photos, list):
            for photo in photos:
                if isinstance(photo, dict):
                    url = self._first_non_empty(
                        photo.get("full_size_url"),
                        photo.get("url"),
                        photo.get("image_url"),
                        self._nested_value(photo, "thumbnails", 0, "url"),
                    )
                else:
                    url = str(photo) if photo else None
                if not url:
                    continue
                if not url.startswith("http://") and not url.startswith("https://"):
                    url = urljoin(f"{self._base_url_for_region(region)}/", url.lstrip("/"))
                urls.append(url)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _extract_created_at(item: dict[str, Any]) -> datetime | None:
        # Return None (not now()) when the listing has no parseable timestamp, so undated or
        # malformed listings are not silently treated as brand-new. Callers fall back to the
        # scan time for first_seen_at.
        raw_value = item.get("created_at_ts") or item.get("created_at") or item.get("created_at_epoch")
        if isinstance(raw_value, (int, float)):
            return datetime.fromtimestamp(raw_value, tz=UTC)
        if isinstance(raw_value, str):
            normalized = raw_value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        return None

    @staticmethod
    def _extract_features(*, title: str, brand: str, description: str) -> list[CandidateFeature]:
        text_blob = " ".join(part for part in [title, brand, description] if part).lower()
        feature_rules = [
            (
                "palette_earth",
                "Earth-tone palette",
                ["brown", "braun", "beige", "camel", "cream", "creme", "olive", "oliv", "taupe", "tan"],
                ["neon", "hot pink", "fluoro", "lime", "multicolor", "mehrfarbig", "bunt"],
            ),
            (
                "fit_boxy",
                "Boxy fit",
                ["boxy", "oversized", "relaxed", "cropped bomber", "wide fit", "weiter schnitt", "locker"],
                ["slim", "skinny", "bodycon", "fitted", "eng geschnitten", "figurbetont"],
            ),
            (
                "material_wool",
                "Wool fabric",
                ["wool", "wolle", "mohair", "alpaca", "alpaka", "cashmere", "kaschmir", "merino"],
                ["polyester", "nylon", "acrylic", "acryl"],
            ),
            (
                "branding_minimal",
                "Minimal branding",
                [
                    "minimal",
                    "plain",
                    "clean",
                    "subtle",
                    "no logo",
                    "kein logo",
                    "minimal logo",
                    "small logo",
                    "kleines logo",
                ],
                [
                    "large logo",
                    "big logo",
                    "grosses logo",
                    "großes logo",
                    "graphic",
                    "big print",
                    "large print",
                    "grosser print",
                    "großer print",
                    "embroidered logo",
                ],
            ),
        ]

        features: list[CandidateFeature] = []
        for key, label, positive_terms, negative_terms in feature_rules:
            positive_hits = [term for term in positive_terms if term in text_blob]
            negative_hits = [term for term in negative_terms if term in text_blob]
            if not positive_hits and not negative_hits:
                continue

            signal_strength = (len(positive_hits) - len(negative_hits)) / max(len(positive_hits) + len(negative_hits), 1)
            features.append(
                CandidateFeature(
                    key=key,
                    label=label,
                    value=", ".join(positive_hits or negative_hits) or "unknown",
                    signal_strength=round(max(-1.0, min(1.0, signal_strength)), 2),
                    source="text_model",
                )
            )
        return features

    def _base_url_for_region(self, region: str) -> str:
        if self.base_url is not None:
            return self.base_url
        suffix = region.strip().lower() or self.settings.vinted_region
        if suffix in {"com", "us"}:
            suffix = "com"
        return f"https://www.vinted.{suffix}"

    def _request_headers(self, *, region: str) -> dict[str, str]:
        base_url = self._base_url_for_region(region)
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": base_url,
            "Referer": f"{base_url}/catalog",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": VINTED_WEB_USER_AGENT,
            "X-Content-Only": "default",
            "X-Next-App": "marketplace-web",
        }

    def _attach_cookie_headers(self, headers: dict[str, str], cookie_header: str) -> None:
        headers["Cookie"] = cookie_header
        csrf_token = self._extract_csrf_token(cookie_header)
        if csrf_token and csrf_token.lower() != "false":
            headers["X-CSRF-Token"] = csrf_token
        headers["X-Anon-Id"] = self._extract_anon_id(cookie_header) or self._generated_anon_id

    @staticmethod
    def _extract_csrf_token(cookie: str) -> str | None:
        for name in ("csrf_token", "csrf-token", "csrf", "x-csrf-token", "XSRF-TOKEN"):
            token = VintedClient._extract_named_token(cookie, name)
            if token:
                return token
        return None

    @staticmethod
    def _extract_anon_id(cookie: str) -> str | None:
        for name in ("anon_id", "anon_id_web", "vinted_anon_id"):
            token = VintedClient._extract_named_token(cookie, name)
            if token:
                return token
        return None

    @staticmethod
    def _looks_like_datadome_block(response: httpx.Response) -> bool:
        if response.headers.get("x-dd-b") or response.headers.get("x-datadome"):
            return True
        set_cookie = response.headers.get("set-cookie", "").lower()
        body_preview = response.text[:500].lower()
        return "datadome" in set_cookie or "datadome" in body_preview

    @classmethod
    def _cookie_quality_detail(cls, cookie: str) -> str:
        missing: list[str] = []
        if not cls._extract_named_token(cookie, "access_token_web"):
            missing.append("access_token_web")
        if not cls._extract_named_token(cookie, "refresh_token_web"):
            missing.append("refresh_token_web")
        if not cls._extract_named_token(cookie, "datadome"):
            missing.append("datadome")
        if not missing:
            return ""
        return (
            "Configured Vinted cookie may be partial; missing required browser cookie pieces: "
            f"{', '.join(missing)}. Copy a fresh full Cookie header from an authenticated Vinted browser tab."
        )

    def _cookie_header_value_or_raise(self) -> str:
        cookie = self._cookie_header_value()
        if cookie is None:
            raise VintedConfigurationError("Vinted cookie is missing. Add VINTED_COOKIE to the environment.")
        return cookie

    def _cookie_header_value(self) -> str | None:
        cookie = self._effective_cookie().strip()
        if not cookie or cookie == "put-your-vinted-cookie-here":
            return None
        if "=" not in cookie:
            cookie = f"access_token_web={cookie}"
        return self._ensure_browser_cookie_tokens(cookie)

    def _ensure_browser_cookie_tokens(self, cookie: str) -> str:
        parts = [p.strip() for p in cookie.split(";") if p.strip()]
        if not self._extract_anon_id(cookie):
            parts.append(f"anon_id={self._generated_anon_id}")
        return "; ".join(parts)

    def _maybe_refresh_token(self) -> None:
        if not self._allow_token_refresh:
            return
        if not self._refresh_token:
            return

        if not self._access_token_needs_refresh():
            return

        with self._token_refresh_lock:
            # Re-check under the lock: a thread that waited here may have been blocked by another
            # thread that already refreshed (and rotated the token), in which case we must not
            # issue a second refresh with a now-stale refresh_token.
            if not self._refresh_token or not self._access_token_needs_refresh():
                return
            self._refresh_token_locked()

    def _access_token_needs_refresh(self) -> bool:
        access_token = self._extract_cookie_token()
        if access_token:
            expiry = self._decode_jwt_expiry(access_token)
            if expiry and expiry > datetime.now(UTC) + TOKEN_REFRESH_BUFFER:
                return False
        return True

    def _refresh_token_locked(self) -> None:
        try:
            response = httpx.post(
                f"{self._base_url_for_region(self.settings.vinted_region)}/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": "web",
                },
                headers={
                        "User-Agent": VINTED_WEB_USER_AGENT,
                        "Accept": "application/json, text/plain, */*",
                        "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            self._last_token_refresh_error = f"request failed: {exc}"
            _logger.warning("Token refresh request failed: %s", exc)
            return

        if not response.is_success:
            self._last_token_refresh_error = f"status {response.status_code} {response.reason_phrase}: {response.text[:200]}"
            _logger.warning("Token refresh failed with status %d: %s", response.status_code, response.text[:200])
            return

        try:
            data = response.json()
        except ValueError:
            self._last_token_refresh_error = "non-JSON response"
            _logger.warning("Token refresh returned non-JSON response")
            return

        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token")
        if not new_access:
            self._last_token_refresh_error = "response missing access_token"
            _logger.warning("Token refresh response missing access_token")
            return

        self._update_access_token(new_access)
        self._last_token_refresh_error = None
        if new_refresh:
            self._refresh_token = new_refresh
        if self._on_tokens_refreshed:
            self._on_tokens_refreshed(self._effective_cookie(), self._refresh_token or "")
        self._session_health_cache.clear()
        _logger.info("Vinted access token refreshed successfully")

    def _update_access_token(self, new_token: str) -> None:
        cookie = self._effective_cookie().strip()
        if not cookie or cookie == "put-your-vinted-cookie-here":
            self._cookie_override = f"access_token_web={new_token}"
            return

        if "=" not in cookie:
            self._cookie_override = new_token
            return

        parts = [p.strip() for p in cookie.split(";") if p.strip()]
        found = False
        for i, part in enumerate(parts):
            name, _, value = part.partition("=")
            if name.strip() == "access_token_web":
                parts[i] = f"access_token_web={new_token}"
                found = True
                break

        if not found:
            parts.insert(0, f"access_token_web={new_token}")

        self._cookie_override = "; ".join(parts)

    @staticmethod
    def _extract_named_token(cookie: str, name: str) -> str | None:
        for part in cookie.split(";"):
            n, _, value = part.strip().partition("=")
            if n.strip() == name and value:
                return value
        return None

    @staticmethod
    def _decode_jwt_expiry(token: str) -> datetime | None:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_segment = parts[1]
        padding = "=" * (-len(payload_segment) % 4)
        try:
            decoded = base64.urlsafe_b64decode(f"{payload_segment}{padding}")
            payload = json.loads(decoded.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return None
        return datetime.fromtimestamp(exp, tz=UTC)

    def get_cookie_expiry(self) -> datetime | None:
        return self._extract_cookie_expiry()

    def get_refresh_token_expiry(self) -> datetime | None:
        token = (self._refresh_token or "").strip()
        if not token:
            token = self._extract_named_token(self._effective_cookie(), "refresh_token_web") or ""
        if not token:
            return None
        return self._decode_jwt_expiry(token)

    def _cookie_is_expired(self) -> bool:
        cookie_expiry = self._extract_cookie_expiry()
        return cookie_expiry is not None and cookie_expiry <= datetime.now(UTC)

    def _extract_cookie_expiry(self) -> datetime | None:
        token = self._extract_cookie_token()
        if token is None:
            return None
        return self._decode_jwt_expiry(token)

    def _extract_cookie_token(self) -> str | None:
        raw_cookie = self._effective_cookie().strip()
        if not raw_cookie or raw_cookie == "put-your-vinted-cookie-here":
            return None
        if "=" not in raw_cookie and raw_cookie.count(".") >= 2:
            return raw_cookie
        for part in raw_cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "access_token_web" and value:
                return value
        return None

    @staticmethod
    def _nested_value(payload: Any, *keys: Any) -> Any:
        value = payload
        for key in keys:
            if isinstance(key, int):
                if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) <= key:
                    return None
                value = value[key]
                continue
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            normalized = value.strip().replace("€", "").replace(" ", "")
            # German formatting: '.' is the thousands separator and ',' the decimal mark.
            # "1.234,56" -> "1234.56"; "1,23" -> "1.23"; "1234.56" stays as-is.
            if "," in normalized and "." in normalized:
                normalized = normalized.replace(".", "").replace(",", ".")
            elif "," in normalized:
                normalized = normalized.replace(",", ".")
            try:
                return float(normalized)
            except ValueError:
                return None
        if isinstance(value, dict):
            for nested_key in ("amount", "numeric"):
                nested_value = value.get(nested_key)
                coerced = VintedClient._coerce_float(nested_value)
                if coerced is not None:
                    return coerced
        return None

    @staticmethod
    def _first_non_empty(*values: Any) -> str | None:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    return normalized
                continue
            if isinstance(value, (int, float, bool)):
                return str(value)
        return None

    @staticmethod
    def _normalise_lookup_text(value: str) -> str:
        return (
            value.strip()
            .lower()
            .replace("&", "and")
            .replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
        )

    @staticmethod
    def _normalise_filter_field(value: str) -> str:
        return VintedClient._normalise_lookup_text(str(value))
