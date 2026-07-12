from datetime import UTC, datetime
from types import SimpleNamespace

from vsniper.db.models import LearningSnapshotState
from vsniper.domain.contracts import TasteProfile
from vsniper.integrations.openai.tokenization import text_counts
from vsniper.services._mapping import learning_snapshot_to_contract
from vsniper.services.taste_service import TasteService


def test_judgment_prompt_preview_includes_o200k_base_counts() -> None:
    service = TasteService.__new__(TasteService)
    service.active_taste_profile = lambda *, clothing_item: SimpleNamespace(version=4)
    service.latest_labeled_anchors = lambda *, clothing_item: ([], [])
    service.get_snapshot = lambda: SimpleNamespace(manual_note=SimpleNamespace(text=""))
    service.taste_client = SimpleNamespace(
        build_judgment_prompt_preview=lambda **kwargs: "Hello, Vinted 👟"
    )

    preview = service.get_judgment_prompt_preview("schuhe")

    assert preview.character_count == 15
    assert preview.token_count == 6
    assert preview.tokenizer == "o200k_base"


def test_learning_snapshot_mapping_counts_old_and_new_prompts() -> None:
    model = LearningSnapshotState(
        id="learn-counts",
        created_at=datetime.now(UTC),
        reason="Recomputed taste profile.",
        changed_weights=[],
        summary="Updated prompt.",
        old_prompt="Hello, Vinted 👟",
        new_prompt="new",
        source_counts={},
    )

    snapshot = learning_snapshot_to_contract(model)

    assert snapshot.old_prompt_character_count == 15
    assert snapshot.old_prompt_token_count == 6
    assert snapshot.new_prompt_character_count == 3
    assert snapshot.new_prompt_token_count == 1
    assert snapshot.prompt_tokenizer == "o200k_base"


def test_learning_snapshot_mapping_exposes_before_and_after_profiles() -> None:
    old_profile = TasteProfile(version=1, summary="old", taste_prompt="old").model_dump(mode="json")
    new_profile = TasteProfile(version=2, summary="new", taste_prompt="new").model_dump(mode="json")
    model = LearningSnapshotState(
        id="learn-profiles",
        created_at=datetime.now(UTC),
        reason="Recomputed taste profile.",
        changed_weights=[],
        summary="new",
        old_taste_profile=old_profile,
        new_taste_profile=new_profile,
        source_counts={},
    )

    snapshot = learning_snapshot_to_contract(model)

    assert snapshot.old_taste_profile is not None and snapshot.old_taste_profile.version == 1
    assert snapshot.new_taste_profile is not None and snapshot.new_taste_profile.version == 2


def test_prompt_counts_treat_special_token_text_as_literal_content() -> None:
    character_count, token_count = text_counts("Taste note: <|endoftext|>")

    assert character_count == 25
    assert token_count > 0
