from __future__ import annotations

from vsniper.core.state import get_state
from vsniper.domain.contracts import DeliveryProcessingResult


def run_once(limit: int = 25) -> DeliveryProcessingResult:
    return get_state().telegram.process_pending_deliveries(limit=limit)
