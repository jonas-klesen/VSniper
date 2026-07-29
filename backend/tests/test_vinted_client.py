from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vsniper.domain.contracts import SearchFilter, SearchRecord
from vsniper.integrations.vinted.browser import BrowserListingChallengeError
from vsniper.integrations.vinted.client import (
    VintedBrowserActionError,
    VintedClient,
    VintedListingUrlError,
    VintedSessionError,
)


def _jwt_with_expiry(expiry: datetime) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b'=').decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(expiry.timestamp())}).encode()).rstrip(b'=').decode()
    return f'{header}.{payload}.signature'


def test_vinted_client_validates_session_and_normalises_results(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    cookie = f'access_token_web={token}; refresh_token_web={token}; anon_id=anon-1; datadome=dd-1; csrf_token=csrf-1'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=cookie, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers['cookie'].startswith('access_token_web=')
        assert 'content-type' not in request.headers
        assert request.headers['x-anon-id'] == 'anon-1'
        assert request.headers['x-csrf-token'] == 'csrf-1'
        if request.url.path == '/api/v2/users/current':
            return httpx.Response(200, json={"user": {"login": "alice"}})
        if request.url.path == '/api/v2/catalog/filters/facets':
            assert request.url.params['filter_code'] == 'size'
            return httpx.Response(
                200,
                json={
                    "filter_code": "size",
                    "options": [
                        {
                            "id": 1,
                            "title": "Clothing sizes",
                            "type": "group",
                            "options": [
                                {"id": 208, "title": "M", "type": "default"},
                                {"id": 209, "title": "L", "type": "default"},
                            ],
                        }
                    ],
                },
            )
        if request.url.path == '/api/v2/catalog/items':
            assert request.url.params['search_text'] == 'wool jacket'
            assert request.url.params['per_page'] == '96'
            assert request.url.params['currency'] == 'EUR'
            assert request.url.params['time'].isdigit()
            assert request.url.params['size_ids'] == '208,209'
            assert request.url.params['price_to'] == '200'
            assert 'price_from' not in request.url.params
            assert 'filter[keywords]' not in request.url.params
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 12345,
                            "title": "Boxy wool bomber jacket",
                            "brand_title": "Acne Studios",
                            "price": {"amount": "149.95"},
                            "size_title": "M",
                            "url": "/items/12345-boxy-wool-bomber-jacket",
                            "description": "Brown oversized wool bomber with minimal logo",
                            "photos": [{"full_size_url": "https://images.vinted.example/12345.jpg"}],
                            "created_at": "2026-05-19T10:15:00Z",
                        }
                    ]
                },
            )
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Wool jackets',
        enabled=True,
        clothing_item='obenrum_kalt',
        query='wool jacket',
        region='de',
        filters=[
            SearchFilter(field='size', label='Sizes', values=['M', 'L'], mode='include'),
            SearchFilter(field='price', label='Price ceiling', values=['200'], mode='range'),
            SearchFilter(field='keywords', label='Prefer', values=['boxy'], mode='include'),
            SearchFilter(field='keywords', label='Avoid', values=['office'], mode='exclude'),
        ],
        last_run_at=None,
        last_found_count=0,
    )

    health = client.get_session_health(region='de', force=True)
    listings = client.run_search(search, validate_session=False)

    assert health.status == 'healthy'
    assert 'missing full browser cookie pieces' not in health.detail
    assert health.last_validated_at is not None
    assert listings[0]['external_item_id'] == '12345'
    assert listings[0]['price_eur'] == 149.95
    assert listings[0]['url'] == 'https://www.vinted.test/items/12345-boxy-wool-bomber-jacket'
    assert listings[0]['image_urls'] == ['https://images.vinted.example/12345.jpg']
    feature_map = {feature.key: feature for feature in listings[0]['features']}
    assert feature_map['material_wool'].signal_strength > 0
    assert feature_map['fit_boxy'].signal_strength > 0
    assert feature_map['branding_minimal'].signal_strength > 0


def test_vinted_client_resolves_numeric_looking_size_titles_via_facets(monkeypatch) -> None:
    """A size title that happens to look numeric (e.g. EU size "54") is a Vinted display
    title, not a pre-resolved filter id — it must be matched against catalog facets like any
    other title, not passed through raw (it would otherwise filter by the wrong option id)."""
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v2/catalog/filters/facets':
            assert request.url.params['filter_code'] == 'size'
            return httpx.Response(
                200,
                json={
                    "filter_code": "size",
                    "options": [
                        {"id": 1598, "title": "54"},
                        {"id": 786, "title": "43"},
                    ],
                },
            )
        if request.url.path == '/api/v2/catalog/items':
            assert request.url.params['size_ids'] == '1598'
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Suit jackets',
        enabled=True,
        clothing_item='obenrum_kalt',
        query='suit jacket',
        region='de',
        filters=[SearchFilter(field='size', label='Sizes', values=['54'], mode='include')],
        last_run_at=None,
        last_found_count=0,
    )

    assert client.run_search(search, validate_session=False) == []


def test_vinted_client_scopes_size_facets_to_category_even_when_size_filter_is_listed_first(monkeypatch) -> None:
    """SearchService._apply_size_filter always prepends the size filter ahead of category, so
    size facet lookups must still be scoped to the search's resolved catalog regardless of
    where "size" sits in search.filters — otherwise a size value can resolve to the wrong
    catalog's facet id (e.g. a shoe size for a trousers search)."""
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    facets_params: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v2/catalog/filters/facets':
            facets_params.append(request.url.params)
            assert request.url.params['filter_code'] == 'size'
            return httpx.Response(200, json={"options": [{"id": 1598, "title": "M"}]})
        if request.url.path == '/api/v2/catalog/items':
            assert request.url.params['catalog_ids'] == '34'
            assert request.url.params['size_ids'] == '1598'
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Cargo pants',
        enabled=True,
        clothing_item='hosen',
        query='cargo pants',
        region='de',
        filters=[
            SearchFilter(field='size', label='Sizes', values=['M'], mode='include'),
            SearchFilter(field='category', label='Category', values=['hosen'], mode='include'),
        ],
        last_run_at=None,
        last_found_count=0,
    )

    assert client.run_search(search, validate_session=False) == []
    assert facets_params, 'expected a facets request for size resolution'
    assert facets_params[0].get('catalog_ids') == '34'


def test_vinted_client_builds_id_based_filter_params(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    requested_paths: list[tuple[str, httpx.QueryParams, str | None, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert 'content-type' not in request.headers
        requested_paths.append(
            (
                request.url.path,
                request.url.params,
                request.headers.get('cookie'),
                request.headers.get('x-anon-id'),
                request.headers.get('x-csrf-token'),
            )
        )
        if request.url.path == '/api/v2/catalog/filters/search':
            assert request.url.params['currency'] == 'EUR'
            assert request.url.params['filter_search_code'] == 'brand'
            assert request.url.params['filter_search_text'] == 'Acne Studios'
            return httpx.Response(200, json={"options": [{"id": 180798, "title": "Acne Studios"}]})
        if request.url.path == '/api/v2/catalog/items':
            assert request.url.params['catalog_ids'] == '2052,2051'
            assert request.url.params['color_ids'] == '2'
            assert request.url.params['status_ids'] == '2,3'
            assert request.url.params['brand_ids'] == '180798'
            assert request.url.params['material_ids'] == '46'
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Wool jackets',
        enabled=True,
        clothing_item='obenrum_kalt',
        query='wool jacket',
        region='de',
        filters=[
            SearchFilter(field='category', label='Category', values=['jackets', 'coats'], mode='include'),
            SearchFilter(field='colour', label='Colours', values=['brown'], mode='include'),
            SearchFilter(field='condition', label='Condition', values=['very good', 'good'], mode='include'),
            SearchFilter(field='brand', label='Brand', values=['Acne Studios'], mode='include'),
            SearchFilter(field='material', label='Material', values=['wool'], mode='include'),
        ],
        last_run_at=None,
        last_found_count=0,
    )

    cookie_value = client._cookie_header_value()
    assert cookie_value.startswith(f'access_token_web={token}')
    assert 'anon_id=' in cookie_value
    assert client.run_search(search, validate_session=False) == []
    assert requested_paths[-1][0] == '/api/v2/catalog/items'
    assert requested_paths[-1][3]
    assert requested_paths[-1][4] is None


def test_fetch_item_by_url_reads_listing_page_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie='', vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/items/12345-wide-black-cargos':
            return httpx.Response(
                200,
                text='''
                    <html><head>
                      <link rel="canonical" href="https://www.vinted.test/items/12345-wide-black-cargos">
                      <meta property="og:title" content="Wide black cargos | Vinted">
                      <meta property="og:description" content="Wide relaxed black cotton trousers">
                      <meta property="og:image" content="https://images.vinted.example/12345.jpg">
                    </head></html>
                ''',
            )
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    listing = client.fetch_item_by_url(
        'https://www.vinted.test/items/12345-wide-black-cargos?foo=bar',
        clothing_item='hosen',
    )

    assert listing['external_item_id'] == '12345'
    assert listing['title'] == 'Wide black cargos'
    assert listing['brand'] == 'Unknown'
    assert listing['price_eur'] == 0.0
    assert listing['url'] == 'https://www.vinted.test/items/12345-wide-black-cargos'
    assert listing['image_urls'] == ['https://images.vinted.example/12345.jpg']


def test_fetch_item_by_url_retries_anonymously_after_stale_cookie_redirect(monkeypatch) -> None:
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie='access_token_web=stale-token', vinted_region='de'),
    )
    cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get('cookie'))
        if request.headers.get('cookie'):
            return httpx.Response(307, headers={'location': '/web/api/auth/expire-cookies?ref_url=%2Fitems%2F12345'})
        return httpx.Response(
            200,
            text='''
                <meta property="og:title" content="Wide black cargos | Vinted">
                <meta property="og:image" content="https://images.vinted.example/12345.jpg">
            ''',
        )

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    listing = client.fetch_item_by_url('https://www.vinted.test/items/12345-wide-black-cargos', clothing_item='hosen')

    assert listing['title'] == 'Wide black cargos'
    assert cookies[0] is not None
    assert cookies[1] is None


def test_fetch_item_by_url_parses_complete_de_listing_fixture(monkeypatch) -> None:
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie='', vinted_region='de'),
    )
    fixture = Path(__file__).parent / 'fixtures' / 'vinted' / 'de' / 'item_9491586544.html'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/items/9491586544-hose-mit-muster'
        return httpx.Response(200, text=fixture.read_text())

    client = VintedClient(
        base_url='https://www.vinted.de',
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    listing = client.fetch_item_by_url(
        'https://www.vinted.de/items/9491586544-hose-mit-muster',
        clothing_item='hosen',
    )

    assert listing['external_item_id'] == '9491586544'
    assert listing['title'] == 'Hose mit Muster'
    assert listing['brand'] == 'Cider'
    assert listing['size'] == 'L'
    assert listing['price_eur'] == 12.0
    assert listing['url'] == 'https://www.vinted.de/items/9491586544-hose-mit-muster'
    assert listing['image_urls'] == [
        'https://images1.vinted.net/t/02_0116a/f800/1785048655.webp',
        'https://images1.vinted.net/t/01_0236b/f800/1785048655.webp',
        'https://images1.vinted.net/t/05_00473/f800/1785048655.webp',
        'https://images1.vinted.net/t/05_00394/f800/1785048655.webp',
    ]


def test_fetch_item_by_url_uses_browser_transport_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie='', vinted_region='de'),
    )

    class FakeBrowser:
        def fetch_html(self, url: str, *, cookie_header: str) -> str:
            assert url == 'https://www.vinted.test/items/12345-wide-black-cargos'
            assert cookie_header == ''
            return '''
                <link rel="canonical" href="https://www.vinted.test/items/12345-wide-black-cargos">
                <meta property="og:title" content="Wide black cargos | Vinted">
                <meta property="og:image" content="https://images.vinted.example/12345.jpg">
            '''

    client = VintedClient(base_url='https://www.vinted.test', browser_client=FakeBrowser())  # type: ignore[arg-type]
    listing = client.fetch_item_by_url(
        'https://www.vinted.test/items/12345-wide-black-cargos',
        clothing_item='hosen',
    )

    assert listing['external_item_id'] == '12345'
    assert listing['title'] == 'Wide black cargos'


def test_fetch_item_by_url_maps_browser_challenge_to_actionable_error(monkeypatch) -> None:
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie='', vinted_region='de'),
    )

    class FakeBrowser:
        def fetch_html(self, url: str, *, cookie_header: str) -> str:
            raise BrowserListingChallengeError("verification required")

    client = VintedClient(base_url='https://www.vinted.test', browser_client=FakeBrowser())  # type: ignore[arg-type]
    with pytest.raises(VintedBrowserActionError) as exc_info:
        client.fetch_item_by_url(
            'https://www.vinted.test/items/12345-wide-black-cargos',
            clothing_item='hosen',
        )

    assert exc_info.value.code == 'vinted_browser_challenge'
    assert exc_info.value.recovery_path == '/vinted-browser/?autoconnect=1&resize=scale'


def test_fetch_item_by_url_rejects_non_item_urls() -> None:
    with pytest.raises(VintedListingUrlError, match="/items/<id>"):
        VintedClient._item_id_from_url("https://www.vinted.de/member/123")


def test_fetch_item_by_url_rejects_non_german_vinted_domain() -> None:
    with pytest.raises(VintedListingUrlError, match="vinted.de"):
        VintedClient._item_id_from_url("https://www.vinted.com/items/123-item")


def test_item_id_from_url_accepts_listing_url_without_scheme() -> None:
    assert VintedClient._item_id_from_url(
        "vinted.de/items/9360600019-bunte-patchwork-hose-im-ethno-hippie-stil"
    ) == "9360600019"


def test_vinted_client_generated_anon_id_is_stable_and_does_not_fake_csrf(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    captured: list[tuple[str | None, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.headers.get('cookie'),
                request.headers.get('x-anon-id'),
                request.headers.get('x-csrf-token'),
            )
        )
        if request.url.path == '/api/v2/users/current':
            return httpx.Response(200, json={"user": {"login": "alice"}})
        if request.url.path == '/api/v2/catalog/items':
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Sneakers',
        enabled=True,
        clothing_item='schuhe',
        query='sneakers',
        region='de',
        filters=[],
        last_run_at=None,
        last_found_count=0,
    )

    assert client.get_session_health(region='de', force=True).status == 'healthy'
    assert client.run_search(search, validate_session=False) == []

    assert len(captured) == 2
    first_cookie, first_anon, first_csrf = captured[0]
    second_cookie, second_anon, second_csrf = captured[1]
    assert first_anon
    assert first_anon == second_anon
    assert f'anon_id={first_anon}' in (first_cookie or '')
    assert f'anon_id={second_anon}' in (second_cookie or '')
    assert first_csrf is None
    assert second_csrf is None


def test_vinted_client_does_not_forward_exclude_filters(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v2/catalog/items':
            # An exclude-mode filter must NOT be sent as a positive include, otherwise Vinted
            # returns exactly the items it should remove and we burn VLM budget on them.
            assert 'status_ids' not in request.url.params
            assert 'filter[condition]' not in request.url.params
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Sneakers',
        enabled=True,
        clothing_item='schuhe',
        query='sneakers',
        region='de',
        filters=[
            SearchFilter(field='condition', label='Condition', values=['satisfactory'], mode='exclude'),
        ],
        last_run_at=None,
        last_found_count=0,
    )

    assert client.run_search(search, validate_session=False) == []
    params = client._build_search_params(search)
    assert 'status_ids' not in params
    assert 'filter[condition]' not in params


def test_vinted_client_refresh_token_is_serialised_under_concurrency(monkeypatch) -> None:
    import threading

    expired_access = _jwt_with_expiry(datetime.now(UTC) - timedelta(minutes=5))
    refresh = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    cookie = f'access_token_web={expired_access}; refresh_token_web={refresh}'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=cookie, vinted_region='de'),
    )

    call_count = 0
    barrier = threading.Barrier(4)
    fresh_access = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))

    def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"access_token": fresh_access, "refresh_token": refresh})

    monkeypatch.setattr('vsniper.integrations.vinted.client.httpx.post', fake_post)

    client = VintedClient(base_url='https://www.vinted.test')
    client.set_cookie(cookie)

    def worker() -> None:
        barrier.wait()
        client._maybe_refresh_token()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Only one thread should have actually hit the refresh endpoint; the rest re-check the
    # (now fresh) token under the lock and return early.
    assert call_count == 1


def test_sync_persisted_credentials_skips_when_unchanged(monkeypatch) -> None:
    expired_access = _jwt_with_expiry(datetime.now(UTC) - timedelta(minutes=5))
    old_refresh = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    cookie = f'access_token_web={expired_access}; refresh_token_web={old_refresh}'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=cookie, vinted_region='de'),
    )

    fresh_access = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    rotated_refresh = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=3))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.httpx.post',
        lambda *a, **k: httpx.Response(200, json={"access_token": fresh_access, "refresh_token": rotated_refresh}),
    )

    client = VintedClient(base_url='https://www.vinted.test')
    client.set_cookie(cookie)

    # Rotate the single-use refresh token and record what the worker would persist to the DB.
    client._maybe_refresh_token()
    persisted_cookie = client._effective_cookie()
    persisted_refresh = client._refresh_token
    assert persisted_refresh == rotated_refresh

    # Re-applying the persisted (post-rotation) values must be a no-op: a concurrent scan that
    # reads the same DB row should not clobber the rotated token or poison the health cache.
    client._session_health_cache['de'] = object()
    client.sync_persisted_credentials(persisted_cookie, persisted_refresh)
    assert client._refresh_token == rotated_refresh
    assert client._cookie_override == persisted_cookie
    assert 'de' in client._session_health_cache

    # A genuinely new credential (cookie pasted in the web UI) is still applied.
    new_refresh = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=4))
    new_cookie = f'access_token_web={fresh_access}; refresh_token_web={new_refresh}'
    client.sync_persisted_credentials(new_cookie, new_refresh)
    assert client._refresh_token == new_refresh


def test_vinted_client_resolves_real_mens_category_aliases(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == '/api/v2/catalog/filters/facets':
            raise AssertionError(f'category filters must not use facets: {request.url}')
        if request.url.path == '/api/v2/catalog/items':
            assert request.url.params['catalog_ids'] == '76,79,34,257,80,1206,2052,2051,2553,2552,1231,1242,86'
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Mens wardrobe categories',
        enabled=True,
        clothing_item='obenrum_kalt',
        query='vintage menswear',
        region='de',
        filters=[
            SearchFilter(
                field='category',
                label='Category',
                values=[
                    'tops',
                    'pullover',
                    'hosen',
                    'pants',
                    'jeans',
                    'shorts',
                    'jacken & mäntel',
                    'jacken',
                    'mäntel',
                    'westen',
                    'ponchos',
                    'schuhe',
                    'sneaker',
                    'kopf',
                ],
                mode='include',
            ),
        ],
        last_run_at=None,
        last_found_count=0,
    )

    assert client.run_search(search, validate_session=False) == []
    assert len(requested_urls) == 1


def test_vinted_client_skips_unresolved_category_filters(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(str(request.url))
        if request.url.path == '/api/v2/catalog/filters/facets':
            raise AssertionError(f'category filters must not use facets: {request.url}')
        if request.url.path == '/api/v2/catalog/items':
            assert 'catalog_ids' not in request.url.params
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    search = SearchRecord(
        id='search-1',
        name='Colorful cargos',
        enabled=True,
        clothing_item='hosen',
        query='cargo pants',
        region='de',
        filters=[
            SearchFilter(field='category', label='Category', values=['gorp fairy', 'space wizard'], mode='include'),
        ],
        last_run_at=None,
        last_found_count=0,
    )

    assert client.run_search(search, validate_session=False) == []
    assert client._facet_filter_options(field='category', search=search, current_params={}) == []
    assert len(requested_paths) == 1
    assert requested_paths[0].startswith('https://www.vinted.test/api/v2/catalog/items?')
    assert 'search_text=cargo+pants' in requested_paths[0]
    assert 'per_page=96' in requested_paths[0]
    assert 'currency=EUR' in requested_paths[0]


def test_vinted_client_session_health_reports_partial_cookie_quality(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=token, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v2/users/current':
            return httpx.Response(200, json={"user": {"login": "alice"}})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))

    health = client.get_session_health(region='de', force=True)

    assert health.status == 'healthy'
    assert 'missing required browser cookie pieces' in health.detail
    assert 'refresh_token_web' in health.detail
    assert 'datadome' in health.detail


def test_vinted_client_non_success_error_includes_empty_body_and_refresh_context(monkeypatch) -> None:
    expired_access = _jwt_with_expiry(datetime.now(UTC) - timedelta(minutes=5))
    expired_refresh = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    cookie = f'access_token_web={expired_access}; refresh_token_web={expired_refresh}; anon_id=anon-1; datadome=dd-1; csrf_token=csrf-1'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=cookie, vinted_region='de'),
    )
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.httpx.post',
        lambda *args, **kwargs: httpx.Response(401, json={"error": "invalid_grant"}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v2/catalog/items':
            return httpx.Response(406, text='')
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.set_cookie(cookie)
    search = SearchRecord(
        id='search-1',
        name='Sneakers',
        enabled=True,
        clothing_item='schuhe',
        query='vintage sneakers',
        region='de',
        filters=[],
        last_run_at=None,
        last_found_count=0,
    )

    with pytest.raises(VintedSessionError) as exc_info:
        client.run_search(search, validate_session=False)

    message = str(exc_info.value)
    assert '406 Not Acceptable' in message
    assert 'Refresh the Vinted cookie from an authenticated browser tab' in message


@pytest.mark.parametrize(
    "raw,expected",
    [
        (149.95, 149.95),
        ("149.95", 149.95),
        ("1,23", 1.23),
        ("1.234,56", 1234.56),
        ("1.000.000,00", 1000000.0),
        ("1234.56", 1234.56),
        ("€ 1.234,56", 1234.56),
        ({"amount": "1.234,56"}, 1234.56),
        ("not a number", None),
    ],
)
def test_coerce_float_handles_german_number_formats(raw, expected) -> None:
    assert VintedClient._coerce_float(raw) == expected


def test_extract_created_at_supports_known_formats_and_falls_back_to_none() -> None:
    epoch = datetime(2026, 5, 19, 10, 15, tzinfo=UTC)
    # Integer epoch (created_at_ts / created_at_epoch) and ISO 'Z' string all parse to UTC.
    assert VintedClient._extract_created_at({"created_at_ts": int(epoch.timestamp())}) == epoch
    assert VintedClient._extract_created_at({"created_at_epoch": int(epoch.timestamp())}) == epoch
    assert VintedClient._extract_created_at({"created_at": "2026-05-19T10:15:00Z"}) == epoch
    # Naive ISO strings are assumed UTC.
    assert VintedClient._extract_created_at({"created_at": "2026-05-19T10:15:00"}) == epoch
    # Missing or unparseable timestamps yield None (not now()), so undated listings are not
    # silently treated as brand-new.
    assert VintedClient._extract_created_at({}) is None
    assert VintedClient._extract_created_at({"created_at": "yesterday"}) is None


def test_normalise_item_handles_photo_shapes_and_missing_timestamp() -> None:
    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    search = SearchRecord(
        id='search-1',
        name='Wool jackets',
        enabled=True,
        clothing_item='obenrum_kalt',
        query='wool jacket',
        region='de',
        filters=[],
        last_run_at=None,
        last_found_count=0,
    )

    # Single dict photo with full_size_url, German price, and no created_at field.
    normalised = client._normalise_item(
        {
            "id": 999,
            "title": "Wool coat",
            "brand_title": "Acne Studios",
            "price": {"amount": "1.234,56"},
            "size_title": "L",
            "url": "/items/999-wool-coat",
            "photos": [{"full_size_url": "https://images.vinted.example/999.jpg"}],
        },
        search,
    )
    assert normalised["price_eur"] == 1234.56
    assert normalised["image_urls"] == ["https://images.vinted.example/999.jpg"]
    assert normalised["created_at"] is None

    # Bare-string photo entries are also accepted and resolved to absolute URLs.
    string_photo = client._normalise_item(
        {
            "id": 1000,
            "title": "Wool coat",
            "price": {"amount": "10,00"},
            "photos": ["/photos/1000.jpg"],
            "created_at": "2026-05-19T10:15:00Z",
        },
        search,
    )
    assert string_photo["image_urls"] == ["https://www.vinted.test/photos/1000.jpg"]
    assert string_photo["created_at"] == datetime(2026, 5, 19, 10, 15, tzinfo=UTC)


def test_validate_cookie_does_not_mutate_live_client_state(monkeypatch) -> None:
    live_refresh = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    live_cookie = f'access_token_web={live_refresh}; refresh_token_web={live_refresh}; anon_id=live; datadome=dd; csrf_token=csrf'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=live_cookie, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v2/users/current':
            return httpx.Response(200, json={"user": {"login": "probe"}})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.set_cookie(live_cookie)
    cached_health = client.get_session_health(region='de', force=True)

    snapshot_cookie = client._cookie_override
    snapshot_refresh = client._refresh_token

    probe_refresh = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=5))
    probe_cookie = f'access_token_web={probe_refresh}; refresh_token_web={probe_refresh}; anon_id=probe; datadome=dd2; csrf_token=csrf2'
    health = client.validate_cookie(probe_cookie)

    assert health.status == 'healthy'
    # Live state must be exactly as it was before the probe.
    assert client._cookie_override == snapshot_cookie
    assert client._refresh_token == snapshot_refresh
    assert client._session_health_cache.get('de') is cached_health


def test_validate_cookie_does_not_refresh_or_persist_tokens(monkeypatch) -> None:
    # A probe cookie whose access token is within the refresh buffer (valid but soon to expire)
    # must NOT trigger a token refresh: refreshing rotates and upstream-invalidates the refresh
    # token inside the very cookie the user is about to save, and would persist it to the DB.
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie='', vinted_region='de'),
    )
    persisted: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/token':
            raise AssertionError('validation probe must not refresh the token')
        if request.url.path == '/api/v2/users/current':
            return httpx.Response(200, json={"user": {"login": "probe"}})
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    client = VintedClient(
        base_url='https://www.vinted.test',
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        on_tokens_refreshed=lambda cookie, refresh: persisted.append((cookie, refresh)),
    )

    # Access token expires in 1 minute — inside TOKEN_REFRESH_BUFFER (5 min), so a live request
    # would refresh, but the probe must not.
    near_expiry = _jwt_with_expiry(datetime.now(UTC) + timedelta(minutes=1))
    probe_cookie = (
        f'access_token_web={near_expiry}; refresh_token_web=valid-refresh; '
        'anon_id=p; datadome=dd; csrf_token=csrf'
    )

    health = client.validate_cookie(probe_cookie)

    assert health.status == 'healthy'
    assert persisted == []  # nothing was persisted to the DB


_RSC_WITH_SIZES = (
    '0:{"P":null}\n'
    '1:["$","$L1",null,{"categories":['
    '{"title":"Damen","code":"WOMEN_ROOT","sizeGroupIds":[4],"userSelectedSizes":[]},'
    '{"title":"Herren","code":"MENS","sizeGroupIds":[14],"userSelectedSizes":['
    '{"id":209,"title":"L","is_default":0,"is_other":0,"order":4},'
    '{"id":208,"title":"M","is_default":0,"is_other":0,"order":3}'
    ']}'
    ']}]\n'
)

_RSC_EMPTY_SIZES = (
    '0:{"P":null}\n'
    '1:["$","$L1",null,{"categories":['
    '{"title":"Damen","code":"WOMEN_ROOT","userSelectedSizes":[]},'
    '{"title":"Herren","code":"MENS","userSelectedSizes":[]}'
    ']}]\n'
)


def test_fetch_user_sizes_returns_selected_titles(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    cookie = f'access_token_web={token}; refresh_token_web={token}; anon_id=anon-1; datadome=dd-1; csrf_token=csrf-1'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=cookie, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/member/personalization/sizes/mens'
        assert request.headers.get('RSC') == '1'
        return httpx.Response(200, text=_RSC_WITH_SIZES, headers={'content-type': 'text/x-component'})

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    sizes = client.fetch_user_sizes(region='de')

    assert sizes == ['L', 'M']


_RSC_WITH_SIZE_GROUPS = (
    '0:{"P":null}\n'
    '1:["$","$L1",null,{"categories":['
    '{"title":"Damen","code":"WOMEN_ROOT","sizeGroupIds":[4],"userSelectedSizes":[]},'
    '{"title":"Herren","code":"MENS","sizeGroupIds":[14,38],"userSelectedSizes":['
    '{"id":209,"title":"L","is_default":0,"is_other":0,"order":4},'
    '{"id":786,"title":"43","is_default":0,"is_other":0,"order":10},'
    '{"id":999,"title":"M","is_default":0,"is_other":0,"order":3}'
    ']}'
    '],"sizeGroups":['
    '{"id":4,"caption_code":10,"caption":"Größe","description":"Kleidergrößen","size_ids":[209]},'
    '{"id":38,"caption_code":10,"caption":"Größe","description":"Schuhe Männer","size_ids":[786]}'
    ']}]\n'
)


def test_fetch_user_sizes_grouped_attaches_size_group_description(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    cookie = f'access_token_web={token}; refresh_token_web={token}; anon_id=anon-1; datadome=dd-1; csrf_token=csrf-1'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=cookie, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/member/personalization/sizes/mens'
        return httpx.Response(200, text=_RSC_WITH_SIZE_GROUPS, headers={'content-type': 'text/x-component'})

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    grouped = client.fetch_user_sizes_grouped(region='de')

    # Size id 999 ("M") has no entry in any sizeGroups.size_ids, so it has no known
    # category and must be dropped rather than guessed.
    assert grouped == [
        {'title': 'L', 'group_description': 'Kleidergrößen'},
        {'title': '43', 'group_description': 'Schuhe Männer'},
    ]


def test_fetch_user_sizes_empty_when_none_selected(monkeypatch) -> None:
    token = _jwt_with_expiry(datetime.now(UTC) + timedelta(hours=2))
    cookie = f'access_token_web={token}; refresh_token_web={token}; anon_id=anon-1; datadome=dd-1; csrf_token=csrf-1'
    monkeypatch.setattr(
        'vsniper.integrations.vinted.client.get_settings',
        lambda: SimpleNamespace(vinted_cookie=cookie, vinted_region='de'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/member/personalization/sizes/mens'
        return httpx.Response(200, text=_RSC_EMPTY_SIZES, headers={'content-type': 'text/x-component'})

    client = VintedClient(base_url='https://www.vinted.test', client=httpx.Client(transport=httpx.MockTransport(handler)))
    sizes = client.fetch_user_sizes(region='de')

    assert sizes == []
