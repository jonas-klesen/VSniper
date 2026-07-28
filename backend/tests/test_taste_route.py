from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from vsniper.api.routes import taste
from vsniper.domain.contracts import TasteOfferCreate
from vsniper.integrations.vinted.client import VintedBrowserActionError


def test_add_offer_route_returns_structured_browser_challenge(monkeypatch) -> None:
    class FakeTasteService:
        def add_offer(self, payload: TasteOfferCreate):
            raise VintedBrowserActionError(
                "Vinted requires browser verification.",
                code="vinted_browser_challenge",
                recovery_path="/vinted-browser/?autoconnect=1&resize=scale",
            )

    monkeypatch.setattr(taste, "get_state", lambda: SimpleNamespace(taste=FakeTasteService()))

    with pytest.raises(HTTPException) as exc_info:
        taste.add_offer(
            TasteOfferCreate(
                vinted_url="https://www.vinted.de/items/123-item",
                clothing_item="hosen",
            )
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "vinted_browser_challenge",
        "message": "Vinted requires browser verification.",
        "recovery_path": "/vinted-browser/?autoconnect=1&resize=scale",
    }
