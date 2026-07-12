from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vsniper.db.models import TasteState
from vsniper.services._mapping import taste_state_to_snapshot


def test_snapshot_handles_naive_last_recomputed_at_with_aware_manual_note() -> None:
    # SQLite hands back DateTime(timezone=True) columns as naive, while a manual note
    # just saved in-session via datetime.now(UTC) is tz-aware. Mixing them in a
    # comparison used to raise TypeError and 500 the taste-note PUT endpoint forever.
    now = datetime.now(UTC)
    state = TasteState(
        id=1,
        manual_note="liebe oversize cargos",
        manual_note_updated_at=now,  # aware (set this session)
        taste_profile={},
        reference_observations=[],
        last_recomputed_at=(now - timedelta(hours=1)).replace(tzinfo=None),  # naive (from DB)
    )

    snapshot = taste_state_to_snapshot(state, samples=[])

    assert snapshot.dirty_counts.manual_note_changed is True
    assert snapshot.manual_note.text == "liebe oversize cargos"
