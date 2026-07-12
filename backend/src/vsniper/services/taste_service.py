from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import cast
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vsniper.core.config import Settings
from vsniper.core.database import session_scope
from vsniper.db.models import AppSettingsState, Candidate, LearningSnapshotState, TasteSampleState, TasteState
from vsniper.domain.contracts import (
    AiProvider,
    CLOTHING_ITEM_LABELS,
    ClothingItem,
    JudgmentPromptPreview,
    LabeledExample,
    ReferenceObservation,
    TasteManualNoteUpdate,
    TasteObservationCacheStats,
    TasteOfferCreate,
    TasteProfile,
    TasteRecomputeResult,
    TasteSample,
    TasteSampleUpdate,
    TasteSnapshot,
)
from vsniper.integrations.openai.client import OpenAITasteClient
from vsniper.integrations.openai.pricing import compute_cost
from vsniper.integrations.openai.tokenization import text_counts
from vsniper.integrations.vinted.client import VintedClient
from vsniper.services._mapping import (
    learning_snapshot_to_contract,
    resolve_ai_model,
    taste_sample_to_contract,
    taste_state_to_snapshot,
)


_IMAGE_FORMAT_SUFFIXES = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}
logger = logging.getLogger(__name__)
TASTE_RECOMPUTE_STALE_AFTER = timedelta(hours=2)
# Allowlist for image URLs fetched from user-supplied Vinted listing URLs.
_VINTED_IMAGE_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*vinted\.[a-z]{2,}$", re.IGNORECASE
)


@dataclass(frozen=True)
class TasteRecomputeClaim:
    claimed: bool
    job_id: str | None
    started_at: datetime | None
    running_job_id: str | None = None
    running_started_at: datetime | None = None


class TasteModelNotConfigured(RuntimeError):
    """Raised when a recompute is attempted but the learn or observation model registry
    reference is unset or points at a deleted row."""


class TasteRecomputeAlreadyRunning(RuntimeError):
    def __init__(self, *, job_id: str | None, started_at: datetime | None) -> None:
        super().__init__("Taste recompute is already running.")
        self.job_id = job_id
        self.started_at = started_at


@dataclass(frozen=True)
class _SampleImageInput:
    sample_id: str
    image_id: str
    file_name: str
    image_bytes: bytes
    clothing_item: ClothingItem
    content_hash: str


class TasteService:
    def __init__(self, settings: Settings, taste_client: OpenAITasteClient, vinted_client: VintedClient) -> None:
        self.settings = settings
        self.taste_client = taste_client
        self.vinted_client = vinted_client
        self._upload_dir_path().mkdir(parents=True, exist_ok=True)
        self._offer_cache_dir().mkdir(parents=True, exist_ok=True)
        self._feedback_asset_dir().mkdir(parents=True, exist_ok=True)
        self.migrate_legacy_feedback_images()

    def _upload_dir_path(self) -> Path:
        upload_root = self.settings.resolve_path(self.settings.upload_dir)
        preferred_dir = upload_root / "taste"
        if not preferred_dir.exists() or os.access(preferred_dir, os.W_OK | os.X_OK):
            return preferred_dir
        return upload_root / "taste_rw"

    def _offer_cache_dir(self) -> Path:
        return self.settings.resolve_path(self.settings.cache_dir) / "taste-offers"

    def _feedback_asset_dir(self) -> Path:
        return self.settings.resolve_path(self.settings.feedback_asset_dir)

    @staticmethod
    def _validated_image_suffix(content: bytes) -> str:
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
                image_format = image.format
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("Uploaded file is not a valid image.") from exc

        suffix = _IMAGE_FORMAT_SUFFIXES.get(str(image_format or "").upper())
        if suffix is None:
            raise ValueError("Only JPEG, PNG, and WebP image uploads are supported.")
        return suffix

    def get_taste_state(self, session: Session) -> TasteState:
        model = session.get(TasteState, 1)
        if model is None:
            model = TasteState(id=1, manual_note="", taste_profile={}, reference_observations=[])
            session.add(model)
            session.flush()
        return model

    def get_snapshot(self) -> TasteSnapshot:
        with session_scope() as session:
            return self._snapshot_in_session(session)

    def _snapshot_in_session(self, session: Session) -> TasteSnapshot:
        state = self.get_taste_state(session)
        samples = list(session.scalars(select(TasteSampleState).order_by(TasteSampleState.created_at.asc())).all())
        latest_snapshot_model = session.scalar(
            select(LearningSnapshotState).order_by(LearningSnapshotState.created_at.desc()).limit(1)
        )
        latest_snapshot = (
            learning_snapshot_to_contract(latest_snapshot_model) if latest_snapshot_model is not None else None
        )
        return taste_state_to_snapshot(state, samples, latest_snapshot=latest_snapshot)

    def add_wardrobe_image(
        self,
        *,
        file_name: str,
        content: bytes,
        note: str,
        clothing_item: ClothingItem,
    ) -> TasteSample:
        normalized_name = Path(file_name).name.strip()
        if not normalized_name:
            raise ValueError("Uploaded image must include a file name.")
        if not content:
            raise ValueError("Uploaded image is empty.")

        suffix = self._validated_image_suffix(content)
        upload_dir = self._upload_dir_path()
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid4().hex}{suffix}"
        relative_storage_path = upload_dir.relative_to(self.settings.resolve_path(self.settings.upload_dir)) / stored_name
        absolute_path = upload_dir / stored_name
        try:
            absolute_path.write_bytes(content)
        except OSError as exc:
            raise RuntimeError(f"Could not write uploaded image to '{upload_dir}'.") from exc

        now = datetime.now(UTC)
        try:
            with session_scope() as session:
                sample = TasteSampleState(
                    id=f"taste-{uuid4().hex[:8]}",
                    kind="wardrobe",
                    clothing_item=clothing_item,
                    note=note.strip(),
                    file_name=normalized_name,
                    storage_path=relative_storage_path.as_posix(),
                    created_at=now,
                    updated_at=now,
                )
                session.add(sample)
                session.flush()
                return taste_sample_to_contract(sample)
        except Exception:
            if absolute_path.exists():
                absolute_path.unlink()
            raise

    def add_offer(self, payload: TasteOfferCreate) -> TasteSample:
        if not payload.vinted_url or not payload.vinted_url.strip():
            raise ValueError("Vinted URL is required.")
        listing = self.vinted_client.fetch_item_by_url(
            payload.vinted_url,
            clothing_item=payload.clothing_item,
            region="de",
        )
        normalized_listing = self._normalise_offer_listing_payload(listing, payload.clothing_item)
        now = datetime.now(UTC)
        with session_scope() as session:
            sample = TasteSampleState(
                id=f"taste-{uuid4().hex[:8]}",
                kind=payload.kind,
                clothing_item=payload.clothing_item,
                note=payload.note.strip(),
                vinted_url=listing["url"],
                external_item_id=listing.get("external_item_id"),
                title=listing["title"],
                brand=listing["brand"],
                price_eur=listing["price_eur"],
                size=listing["size"],
                description=listing.get("description") or "",
                image_urls=listing.get("image_urls") or [],
                normalized_listing=normalized_listing,
                created_at=now,
                updated_at=now,
            )
            sample.cached_image_paths = self._cache_image_urls(sample.id, sample.image_urls)
            session.add(sample)
            session.flush()
            return taste_sample_to_contract(sample)

    @staticmethod
    def _normalise_offer_listing_payload(raw: dict, clothing_item: ClothingItem) -> dict:
        return {
            "external_item_id": raw.get("external_item_id", raw["id"]),
            "clothing_item": clothing_item,
            "region": "de",
            "title": raw["title"],
            "brand": raw["brand"],
            "price_eur": raw["price_eur"],
            "size": raw["size"],
            "url": raw["url"],
            "image_urls": raw.get("image_urls") or [],
            "description": raw.get("description") or "",
            "features": [feature.model_dump(mode="json") for feature in raw.get("features", [])],
            "raw_listing": raw.get("raw_listing", raw),
        }

    def update_sample(self, sample_id: str, payload: TasteSampleUpdate) -> TasteSample:
        with session_scope() as session:
            sample = session.get(TasteSampleState, sample_id)
            if sample is None:
                raise KeyError(sample_id)
            if payload.note is not None:
                sample.note = payload.note.strip()
            if payload.kind is not None:
                sample.kind = payload.kind
            if payload.clothing_item is not None:
                sample.clothing_item = payload.clothing_item
            sample.updated_at = datetime.now(UTC)
            session.flush()
            return taste_sample_to_contract(sample)

    def delete_sample(self, sample_id: str) -> TasteSample:
        with session_scope() as session:
            sample = session.get(TasteSampleState, sample_id)
            if sample is None:
                raise KeyError(sample_id)
            result = taste_sample_to_contract(sample)
            session.delete(sample)
            session.flush()
        self._delete_cached_paths(result.cached_image_paths)
        self._delete_stored_paths(result.stored_image_paths)
        if result.storage_path:
            path = self.settings.resolve_path(self.settings.upload_dir) / result.storage_path
            if path.exists():
                path.unlink()
        return result

    def sample_image_path(self, sample_id: str) -> Path:
        with session_scope() as session:
            sample = session.get(TasteSampleState, sample_id)
            if sample is None:
                raise KeyError(sample_id)
            if not sample.storage_path:
                raise FileNotFoundError(sample_id)

            upload_root = self.settings.resolve_path(self.settings.upload_dir).resolve()
            path = (upload_root / sample.storage_path).resolve()
            try:
                path.relative_to(upload_root)
            except ValueError as exc:
                raise RuntimeError("Stored image path points outside the upload directory.") from exc
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(sample_id)
            return path

    def update_manual_note(self, payload: TasteManualNoteUpdate) -> TasteSnapshot:
        with session_scope() as session:
            state = self.get_taste_state(session)
            now = datetime.now(UTC)
            state.manual_note = payload.text.strip()
            state.manual_note_updated_at = now
            session.flush()
            return self._snapshot_in_session(session)

    def upsert_candidate_feedback_sample(
        self,
        session: Session,
        *,
        candidate: Candidate,
        verdict: str | None,
        comment: str,
        stored_image_paths: list[str] | None = None,
    ) -> TasteSampleState:
        effective_verdict = verdict if verdict in {"like", "dislike"} else candidate.feedback
        kind = (
            "offer_like"
            if effective_verdict == "like"
            else "offer_dislike" if effective_verdict == "dislike" else "offer_note"
        )
        now = datetime.now(UTC)
        sample = session.scalar(select(TasteSampleState).where(TasteSampleState.candidate_id == candidate.id).limit(1))
        if sample is None:
            sample = TasteSampleState(id=f"taste-{uuid4().hex[:8]}", created_at=now)
            session.add(sample)
        sample.kind = kind
        sample.clothing_item = candidate.clothing_item
        sample.note = comment.strip()
        sample.vinted_url = candidate.url
        sample.external_item_id = candidate.external_item_id
        sample.title = candidate.title
        sample.brand = candidate.brand
        sample.price_eur = candidate.price_eur
        sample.size = candidate.size
        sample.description = (candidate.normalized_listing or {}).get("description", "") or ""
        sample.image_urls = candidate.image_urls or []
        sample.normalized_listing = {
            **(candidate.normalized_listing or {}),
            "ai_observation": candidate.ai_observation or {},
            "score_trace": candidate.score_trace or {},
            "decision": candidate.decision,
            "final_score": candidate.final_score,
        }
        sample.candidate_id = candidate.id
        sample.stored_image_paths = (
            sample.stored_image_paths
            or stored_image_paths
            or self._store_feedback_image_urls(candidate.id, sample.image_urls)
        )
        image_paths = sample.stored_image_paths or sample.cached_image_paths
        if candidate.ai_observation and image_paths:
            first_path = self._resolve_sample_asset_path(image_paths[0], durable=bool(sample.stored_image_paths))
            if first_path.exists():
                try:
                    observation = ReferenceObservation.model_validate(candidate.ai_observation)
                    content = first_path.read_bytes()
                    image_input = _SampleImageInput(
                        sample_id=sample.id,
                        image_id=f"{sample.id}:0",
                        file_name=f"{sample.id}-0{first_path.suffix}",
                        image_bytes=content,
                        clothing_item=cast(ClothingItem, candidate.clothing_item),
                        content_hash=self._content_hash(content),
                    )
                    self._replace_cached_observation(
                        sample,
                        image_input,
                        self._normalised_observation(observation, image_input),
                        provider="feedback",
                        model="candidate_ai_observation",
                        image_detail="unknown",
                        observed_at=now,
                    )
                except Exception:
                    logger.debug("Could not copy candidate observation into taste sample cache", exc_info=True)
        sample.updated_at = now
        return sample

    def existing_feedback_image_paths(self, session: Session, *, candidate_id: str) -> list[str]:
        """Durable image paths already stored for this candidate's feedback sample, if any.
        Lets callers skip a redundant re-download when re-recording feedback on the same candidate."""
        sample = session.scalar(
            select(TasteSampleState).where(TasteSampleState.candidate_id == candidate_id).limit(1)
        )
        return list(sample.stored_image_paths or []) if sample is not None else []

    def precache_feedback_images(self, *, candidate_id: str, image_urls: list[str]) -> list[str]:
        """Download a candidate's images outside any DB transaction (no lock held), keyed on the
        candidate id so the result can be handed to upsert_candidate_feedback_sample in Phase 3."""
        return self._store_feedback_image_urls(candidate_id, image_urls or [])

    def _cache_image_urls(self, sample_id: str, image_urls: list[str]) -> list[str]:
        cached: list[str] = []
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            for index, image_url in enumerate(image_urls[:6]):
                try:
                    parsed_host = httpx.URL(image_url).host
                except Exception:
                    logger.warning("_cache_image_urls: skipping unparseable URL at index %d", index)
                    continue
                if not _VINTED_IMAGE_HOST_RE.match(parsed_host):
                    logger.warning(
                        "_cache_image_urls: rejecting non-Vinted image host %r at index %d",
                        parsed_host,
                        index,
                    )
                    continue
                try:
                    response = client.get(image_url, headers={"User-Agent": "vsniper/0.1 (+https://local.vsniper)"})
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                content_type = response.headers.get("content-type", "image/jpeg").split(";", maxsplit=1)[0]
                if not content_type.startswith("image/"):
                    continue
                try:
                    with Image.open(BytesIO(response.content)) as img:
                        img.verify()
                except (UnidentifiedImageError, OSError):
                    continue
                suffix = ".jpg" if content_type in {"image/jpeg", "image/jpg"} else ".png"
                path = self._offer_cache_dir() / f"{sample_id}-{index}{suffix}"
                path.write_bytes(response.content)
                cached.append(path.relative_to(self.settings.resolve_path(self.settings.cache_dir)).as_posix())
        return cached

    def _store_feedback_image_urls(self, candidate_id: str, image_urls: list[str]) -> list[str]:
        stored: list[str] = []
        asset_dir = self._feedback_asset_dir()
        asset_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in candidate_id)
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            for index, image_url in enumerate(image_urls[:6]):
                try:
                    parsed_host = httpx.URL(image_url).host
                except Exception:
                    logger.warning("_store_feedback_image_urls: skipping unparseable URL at index %d", index)
                    continue
                if not _VINTED_IMAGE_HOST_RE.match(parsed_host):
                    logger.warning(
                        "_store_feedback_image_urls: rejecting non-Vinted image host %r at index %d",
                        parsed_host,
                        index,
                    )
                    continue
                try:
                    response = client.get(image_url, headers={"User-Agent": "vsniper/0.1 (+https://local.vsniper)"})
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                content_type = response.headers.get("content-type", "image/jpeg").split(";", maxsplit=1)[0]
                if not content_type.startswith("image/"):
                    continue
                try:
                    with Image.open(BytesIO(response.content)) as img:
                        img.verify()
                except (UnidentifiedImageError, OSError):
                    continue
                suffix = ".jpg" if content_type in {"image/jpeg", "image/jpg"} else ".png"
                path = asset_dir / f"{safe_name}-{index}{suffix}"
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_bytes(response.content)
                tmp_path.replace(path)
                stored.append(path.relative_to(asset_dir).as_posix())
        return stored

    def _delete_cached_paths(self, relative_paths: list[str]) -> None:
        for relative_path in relative_paths:
            path = self.settings.resolve_path(self.settings.cache_dir) / relative_path
            if path.exists():
                path.unlink()

    def _delete_stored_paths(self, relative_paths: list[str]) -> None:
        for relative_path in relative_paths:
            path = self._resolve_sample_asset_path(relative_path, durable=True)
            if path.exists():
                path.unlink()

    def _resolve_sample_asset_path(self, relative_path: str, *, durable: bool) -> Path:
        root = self._feedback_asset_dir() if durable else self.settings.resolve_path(self.settings.cache_dir)
        path = (root / relative_path).resolve()
        root_resolved = root.resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError as exc:
            raise RuntimeError("Stored taste sample image path points outside its storage root.") from exc
        return path

    def migrate_legacy_feedback_images(self) -> int:
        """Copy old feedback sample cache images into durable storage when the cache files remain."""
        migrated = 0
        cache_root = self.settings.resolve_path(self.settings.cache_dir)
        asset_root = self._feedback_asset_dir()
        asset_root.mkdir(parents=True, exist_ok=True)
        try:
            with session_scope() as session:
                samples = session.scalars(
                    select(TasteSampleState).where(TasteSampleState.candidate_id.isnot(None))
                ).all()
                for sample in samples:
                    if sample.stored_image_paths:
                        continue
                    copied: list[str] = []
                    safe_name = "".join(
                        ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in (sample.candidate_id or sample.id)
                    )
                    for index, cached_path in enumerate(sample.cached_image_paths or []):
                        source = (cache_root / cached_path).resolve()
                        try:
                            source.relative_to(cache_root.resolve())
                        except ValueError:
                            continue
                        if not source.exists() or not source.is_file():
                            continue
                        suffix = source.suffix if source.suffix else ".jpg"
                        target = asset_root / f"{safe_name}-{index}{suffix}"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                        copied.append(target.relative_to(asset_root).as_posix())
                    if copied:
                        sample.stored_image_paths = copied
                        sample.updated_at = datetime.now(UTC)
                        migrated += 1
        except Exception:
            logger.exception("Failed to migrate legacy feedback images into durable storage")
        return migrated

    @staticmethod
    def _content_hash(content: bytes) -> str:
        return sha256(content).hexdigest()

    def _sample_image_inputs(self, samples: list[TasteSampleState]) -> list[_SampleImageInput]:
        inputs: list[_SampleImageInput] = []
        upload_root = self.settings.resolve_path(self.settings.upload_dir)
        cache_root = self.settings.resolve_path(self.settings.cache_dir)
        asset_root = self._feedback_asset_dir()
        for sample in samples:
            clothing_item = cast(ClothingItem, sample.clothing_item)
            if sample.storage_path:
                path = upload_root / sample.storage_path
                if path.exists():
                    content = path.read_bytes()
                    inputs.append(
                        _SampleImageInput(
                            sample_id=sample.id,
                            image_id=sample.id,
                            file_name=sample.file_name or sample.id,
                            image_bytes=content,
                            clothing_item=clothing_item,
                            content_hash=self._content_hash(content),
                        )
                    )
            loaded_stored = False
            for index, relative_path in enumerate(sample.stored_image_paths or []):
                path = asset_root / relative_path
                if path.exists():
                    content = path.read_bytes()
                    inputs.append(
                        _SampleImageInput(
                            sample_id=sample.id,
                            image_id=f"{sample.id}:{index}",
                            file_name=f"{sample.id}-{index}{path.suffix}",
                            image_bytes=content,
                            clothing_item=clothing_item,
                            content_hash=self._content_hash(content),
                        )
                    )
                    loaded_stored = True
            for index, relative_path in enumerate(sample.cached_image_paths or []):
                if loaded_stored:
                    break
                path = cache_root / relative_path
                if path.exists():
                    content = path.read_bytes()
                    inputs.append(
                        _SampleImageInput(
                            sample_id=sample.id,
                            image_id=f"{sample.id}:{index}",
                            file_name=f"{sample.id}-{index}{path.suffix}",
                            image_bytes=content,
                            clothing_item=clothing_item,
                            content_hash=self._content_hash(content),
                        )
                    )
        return inputs

    @staticmethod
    def _normalised_observation(observation: ReferenceObservation, image_input: _SampleImageInput) -> ReferenceObservation:
        return observation.model_copy(
            update={
                "image_id": image_input.image_id,
                "file_name": image_input.file_name,
                "clothing_item": image_input.clothing_item,
            }
        )

    def _cached_observation_for_input(
        self,
        sample: TasteSampleState,
        image_input: _SampleImageInput,
    ) -> ReferenceObservation | None:
        for raw_entry in sample.image_observations or []:
            if not isinstance(raw_entry, dict):
                continue
            if raw_entry.get("image_id") != image_input.image_id:
                continue
            if raw_entry.get("content_sha256") != image_input.content_hash:
                continue
            if raw_entry.get("clothing_item") != image_input.clothing_item:
                continue
            raw_observation = raw_entry.get("observation")
            if not isinstance(raw_observation, dict):
                continue
            try:
                return self._normalised_observation(ReferenceObservation.model_validate(raw_observation), image_input)
            except Exception:
                continue

        legacy_payload = (sample.normalized_listing or {}).get("ai_observation")
        if image_input.image_id.endswith(":0") and legacy_payload:
            try:
                return self._normalised_observation(ReferenceObservation.model_validate(legacy_payload), image_input)
            except Exception:
                return None
        return None

    @staticmethod
    def _replace_cached_observation(
        sample: TasteSampleState,
        image_input: _SampleImageInput,
        observation: ReferenceObservation,
        *,
        provider: str,
        model: str,
        image_detail: str,
        observed_at: datetime,
    ) -> None:
        existing = [
            entry
            for entry in (sample.image_observations or [])
            if not isinstance(entry, dict) or entry.get("image_id") != image_input.image_id
        ]
        existing.append(
            {
                "image_id": image_input.image_id,
                "file_name": image_input.file_name,
                "clothing_item": image_input.clothing_item,
                "content_sha256": image_input.content_hash,
                "provider": provider,
                "model": model,
                "image_detail": image_detail,
                "observed_at": observed_at.isoformat(),
                "observation": observation.model_dump(mode="json"),
            }
        )
        sample.image_observations = existing

    def _sample_to_example(self, sample: TasteSampleState) -> LabeledExample:
        observation_payload = (sample.normalized_listing or {}).get("ai_observation")
        try:
            observation = ReferenceObservation.model_validate(observation_payload) if observation_payload else None
        except Exception:
            observation = None
        if observation is None:
            for raw_entry in sample.image_observations or []:
                if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("observation"), dict):
                    continue
                try:
                    observation = ReferenceObservation.model_validate(raw_entry["observation"])
                    break
                except Exception:
                    continue
        return LabeledExample(
            candidate_id=sample.candidate_id or sample.id,
            clothing_item=cast(ClothingItem, sample.clothing_item),
            verdict="like" if sample.kind in {"wardrobe", "offer_like"} else "dislike",
            title=sample.title or sample.file_name or sample.id,
            brand=sample.brand or "",
            description=sample.description or "",
            user_comment=sample.note or "",
            observation=observation,
        )

    @staticmethod
    def _fallback_taste_profile(
        snapshot: TasteSnapshot | None = None,
        *,
        clothing_item: ClothingItem | None = None,
    ) -> TasteProfile:
        manual_note = snapshot.manual_note.text if snapshot is not None else ""
        item_label = CLOTHING_ITEM_LABELS[clothing_item] if clothing_item else "clothing"
        prompt = manual_note or (
            f"User has not generated a {item_label} taste profile yet. Be conservative and prefer distinctive vintage clothing."
        )
        return TasteProfile(
            version=1,
            summary=prompt[:500],
            taste_prompt=prompt,
            core_aesthetic_summary=prompt[:240],
            likes=[],
            dislikes_or_penalties=[],
            source_counts={},
        )

    @staticmethod
    def _profile_for_item(profile: TasteProfile, clothing_item: ClothingItem | None) -> TasteProfile:
        if clothing_item is None:
            return profile
        item_profile = profile.item_profiles.get(clothing_item)
        if item_profile is None:
            return profile
        return TasteProfile(
            version=profile.version,
            summary=item_profile.summary,
            taste_prompt=item_profile.taste_prompt,
            core_aesthetic_summary=item_profile.core_aesthetic_summary,
            item_profiles=profile.item_profiles,
            likes=item_profile.likes,
            dislikes_or_penalties=item_profile.dislikes_or_penalties,
            instant_alert_examples=item_profile.instant_alert_examples,
            instant_reject_examples=item_profile.instant_reject_examples,
            scoring_rubric=item_profile.scoring_rubric,
            transparency_labels=item_profile.transparency_labels,
            generated_searches=[item_profile.generated_search] if item_profile.generated_search else [],
            source_counts=item_profile.source_counts,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            generated_at=profile.generated_at,
        )

    def active_taste_profile(
        self,
        session: Session | None = None,
        *,
        clothing_item: ClothingItem | None = None,
    ) -> TasteProfile:
        if session is not None:
            snapshot = self._snapshot_in_session(session)
            return self._profile_for_item(snapshot.taste_profile, clothing_item) if snapshot.taste_profile else self._fallback_taste_profile(snapshot, clothing_item=clothing_item)
        with session_scope() as owned_session:
            snapshot = self._snapshot_in_session(owned_session)
            return self._profile_for_item(snapshot.taste_profile, clothing_item) if snapshot.taste_profile else self._fallback_taste_profile(snapshot, clothing_item=clothing_item)

    def latest_labeled_anchors(
        self,
        *,
        clothing_item: ClothingItem | None = None,
        limit: int = 5,
    ) -> tuple[list[LabeledExample], list[LabeledExample]]:
        with session_scope() as session:
            samples = session.scalars(
                select(TasteSampleState).order_by(TasteSampleState.updated_at.desc()).limit(limit * 8)
            ).all()
            if clothing_item is not None:
                samples = sorted(samples, key=lambda sample: sample.clothing_item != clothing_item)
            liked: list[LabeledExample] = []
            disliked: list[LabeledExample] = []
            for sample in samples:
                if sample.kind in {"wardrobe", "offer_like"} and len(liked) < limit:
                    liked.append(self._sample_to_example(sample))
                elif sample.kind == "offer_dislike" and len(disliked) < limit:
                    disliked.append(self._sample_to_example(sample))
            return liked, disliked

    def get_judgment_prompt_preview(self, clothing_item: ClothingItem) -> JudgmentPromptPreview:
        profile = self.active_taste_profile(clothing_item=clothing_item)
        liked, disliked = self.latest_labeled_anchors(clothing_item=clothing_item)
        snapshot = self.get_snapshot()
        manual_note = snapshot.manual_note.text if snapshot.manual_note.text.strip() else None
        prompt = self.taste_client.build_judgment_prompt_preview(
            taste_profile=profile,
            liked_anchors=liked or None,
            disliked_anchors=disliked or None,
            manual_note=manual_note,
        )
        character_count, token_count = text_counts(prompt)
        return JudgmentPromptPreview(
            prompt=prompt,
            clothing_item=clothing_item,
            profile_version=profile.version,
            character_count=character_count,
            token_count=token_count,
        )

    def claim_recompute(self, *, source: str = "api") -> TasteRecomputeClaim:
        now = datetime.now(UTC)
        stale_cutoff = now - TASTE_RECOMPUTE_STALE_AFTER
        job_id = f"taste-{source}-{uuid4().hex[:8]}"
        with session_scope() as session:
            self.get_taste_state(session)
            result = session.execute(
                update(TasteState)
                .where(TasteState.id == 1)
                .where(
                    (TasteState.recompute_status != "running")
                    | (TasteState.recompute_started_at == None)  # noqa: E711
                    | (TasteState.recompute_started_at < stale_cutoff)
                )
                .values(
                    recompute_status="running",
                    recompute_job_id=job_id,
                    recompute_started_at=now,
                    recompute_finished_at=None,
                    recompute_error=None,
                )
            )
            if result.rowcount == 1:
                return TasteRecomputeClaim(claimed=True, job_id=job_id, started_at=now)

            state = self.get_taste_state(session)
            return TasteRecomputeClaim(
                claimed=False,
                job_id=None,
                started_at=None,
                running_job_id=state.recompute_job_id,
                running_started_at=state.recompute_started_at,
            )

    def _finish_recompute(
        self,
        *,
        job_id: str,
        status: str,
        error: str | None = None,
        result: TasteRecomputeResult | None = None,
    ) -> None:
        values: dict[str, object] = {
            "recompute_status": status,
            "recompute_finished_at": datetime.now(UTC),
            "recompute_error": error,
        }
        if status == "failed":
            values["recompute_started_at"] = None
        if result is not None:
            values.update(
                {
                    "last_recompute_cost_usd": result.cost_usd,
                    "last_recompute_input_tokens": result.input_tokens,
                    "last_recompute_output_tokens": result.output_tokens,
                }
            )
        with session_scope() as session:
            session.execute(
                update(TasteState)
                .where(TasteState.id == 1)
                .where(TasteState.recompute_job_id == job_id)
                .values(**values)
            )

    def run_claimed_recompute(self, job_id: str) -> TasteRecomputeResult:
        try:
            result = self._run_recompute_unlocked(job_id)
        except Exception as exc:
            self._finish_recompute(job_id=job_id, status="failed", error=str(exc)[:1000])
            raise
        self._finish_recompute(job_id=job_id, status="succeeded", result=result)
        return result.model_copy(update={"snapshot": self.get_snapshot()})

    def recompute(self) -> TasteRecomputeResult:
        claim = self.claim_recompute(source="api")
        if not claim.claimed or claim.job_id is None:
            raise TasteRecomputeAlreadyRunning(job_id=claim.running_job_id, started_at=claim.running_started_at)
        return self.run_claimed_recompute(claim.job_id)

    def cancel_recompute(self) -> TasteSnapshot:
        """Force-clears a running recompute claim so the UI can retry immediately.

        There is no cooperative checkpoint inside `_run_recompute_unlocked` (unlike search
        scanning's `_check_cancel`), so this can't interrupt an OpenAI call already in flight —
        it only unblocks the *next* attempt. Clearing `recompute_job_id` means that if the
        orphaned call eventually returns, `_finish_recompute`'s job_id-scoped WHERE clause makes
        its write a no-op instead of clobbering whatever run started after the cancel.
        """
        with session_scope() as session:
            session.execute(
                update(TasteState)
                .where(TasteState.id == 1)
                .where(TasteState.recompute_status == "running")
                .values(
                    recompute_status="cancelled",
                    recompute_job_id=None,
                    recompute_started_at=None,
                    recompute_finished_at=datetime.now(UTC),
                    recompute_error="Cancelled by user.",
                )
            )
        return self.get_snapshot()

    def _run_recompute_unlocked(self, job_id: str) -> TasteRecomputeResult:
        started = perf_counter()
        accumulated: dict[str, float] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

        def _track(_op: str, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> None:
            accumulated["input_tokens"] += input_tokens
            accumulated["output_tokens"] += output_tokens
            accumulated["cost_usd"] += compute_cost(model, input_tokens, output_tokens, cached_input_tokens)
            logger.info(
                "taste recompute usage op=%s model=%s input_tokens=%d output_tokens=%d accumulated_cost_usd=%.4f",
                _op,
                model,
                input_tokens,
                output_tokens,
                accumulated["cost_usd"],
            )

        with session_scope() as session:
            state = self.get_taste_state(session)
            db_settings = session.get(AppSettingsState, 1)
            if db_settings is None:
                raise RuntimeError("Settings row is missing from SQLite storage")
            learn_model = resolve_ai_model(session, db_settings.learn_model_id)
            if learn_model is None:
                raise TasteModelNotConfigured("No learn model is configured. Set one on the Settings page.")
            observation_model = resolve_ai_model(session, db_settings.observation_model_id)
            if observation_model is None:
                raise TasteModelNotConfigured("No observation model is configured. Set one on the Settings page.")
            observation_detail = self.settings.ai_learn_image_detail
            samples = session.scalars(select(TasteSampleState).order_by(TasteSampleState.created_at.asc())).all()
            previous_profile = TasteProfile.model_validate(state.taste_profile) if state.taste_profile else None
            image_inputs = self._sample_image_inputs(samples)
            samples_by_id = {sample.id: sample for sample in samples}
            liked_count = sum(1 for sample in samples if sample.kind in {"wardrobe", "offer_like"})
            disliked_count = sum(1 for sample in samples if sample.kind == "offer_dislike")
            noted_count = sum(1 for sample in samples if sample.note.strip())
            logger.info(
                (
                    "taste recompute loaded state samples=%d image_inputs=%d liked_examples=%d "
                    "disliked_examples=%d sample_notes=%d manual_note=%s previous_profile=%s model=%s reasoning=%s"
                ),
                len(samples),
                len(image_inputs),
                liked_count,
                disliked_count,
                noted_count,
                bool(state.manual_note.strip()),
                previous_profile is not None,
                learn_model.model_name,
                learn_model.reasoning_effort,
            )
            observations_by_image_id: dict[str, ReferenceObservation] = {}
            missing_inputs: list[_SampleImageInput] = []
            for image_input in image_inputs:
                sample = samples_by_id[image_input.sample_id]
                cached_observation = self._cached_observation_for_input(sample, image_input)
                if cached_observation is None:
                    missing_inputs.append(image_input)
                else:
                    observations_by_image_id[image_input.image_id] = cached_observation

            phase_started = perf_counter()
            logger.info(
                "taste recompute reference observations cache total=%d hits=%d misses=%d provider=%s model=%s",
                len(image_inputs),
                len(observations_by_image_id),
                len(missing_inputs),
                observation_model.provider,
                observation_model.model_name,
            )
            fresh_observations: list[ReferenceObservation] = []
            if missing_inputs:
                batch_size = max(1, self.settings.ai_learn_observation_batch_size)
                batches = [missing_inputs[i : i + batch_size] for i in range(0, len(missing_inputs), batch_size)]
                for batch_index, batch in enumerate(batches, start=1):
                    logger.info(
                        "taste recompute describing image batch %d/%d size=%d",
                        batch_index,
                        len(batches),
                        len(batch),
                    )
                    batch_observations = self.taste_client.describe_reference_images(
                        [(item.image_id, item.file_name, item.image_bytes, item.clothing_item) for item in batch],
                        provider=observation_model.provider,
                        model=observation_model.model_name,
                        reasoning_effort=observation_model.reasoning_effort,
                        local_base_url=observation_model.local_base_url,
                        image_detail=observation_detail,
                        on_usage=_track,
                    )
                    fresh_observations.extend(batch_observations)
                    observed_at = datetime.now(UTC)
                    for image_input, observation in zip(batch, batch_observations, strict=False):
                        normalised = self._normalised_observation(observation, image_input)
                        observations_by_image_id[image_input.image_id] = normalised
                        self._replace_cached_observation(
                            samples_by_id[image_input.sample_id],
                            image_input,
                            normalised,
                            provider=observation_model.provider,
                            model=observation_model.model_name,
                            image_detail=observation_detail,
                            observed_at=observed_at,
                        )
            observations = [
                observations_by_image_id[item.image_id]
                for item in image_inputs
                if item.image_id in observations_by_image_id
            ]
            logger.info(
                "taste recompute resolved reference observations count=%d fresh=%d duration=%.1fs",
                len(observations),
                len(fresh_observations),
                perf_counter() - phase_started,
            )
            liked_examples = [
                self._sample_to_example(sample) for sample in samples if sample.kind in {"wardrobe", "offer_like"}
            ]
            disliked_examples = [self._sample_to_example(sample) for sample in samples if sample.kind == "offer_dislike"]
            notes = [state.manual_note] if state.manual_note.strip() else []
            notes.extend(
                f"{CLOTHING_ITEM_LABELS[sample.clothing_item]}: {sample.note.strip()}"
                for sample in samples
                if sample.note.strip()
            )
            learn_model_name = learn_model.model_name
            learn_reasoning_effort = learn_model.reasoning_effort
            default_region = db_settings.vinted_region
            observation_cache = TasteObservationCacheStats(
                total_image_inputs=len(image_inputs),
                cached_observations=len(image_inputs) - len(missing_inputs),
                fresh_observations=len(fresh_observations),
                observation_provider=cast(AiProvider, observation_model.provider),
                observation_model=observation_model.model_name,
                profile_model=learn_model.model_name,
            )
        # Observations are now durably committed. A failure in the (paid, network-bound)
        # profile build below therefore preserves the freshly described image observations
        # so the next recompute reuses the cache instead of re-paying for them.

        phase_started = perf_counter()
        logger.info(
            "taste recompute building profile observations=%d notes=%d liked_examples=%d disliked_examples=%d",
            len(observations),
            len(notes),
            len(liked_examples),
            len(disliked_examples),
        )
        taste_profile = self.taste_client.build_taste_profile(
            observations=observations,
            notes=notes,
            liked_examples=liked_examples,
            disliked_examples=disliked_examples,
            previous_profile=previous_profile,
            model=learn_model_name,
            reasoning_effort=learn_reasoning_effort,
            default_region=default_region,
            on_usage=_track,
        )
        logger.info(
            "taste recompute built profile version=%d generated_searches=%d duration=%.1fs",
            taste_profile.version,
            len(taste_profile.generated_searches),
            perf_counter() - phase_started,
        )

        with session_scope() as session:
            now = datetime.now(UTC)
            # Scope the write to this job_id: if the claim was cancelled (or reclaimed by a
            # newer run) while this call was in flight, skip persisting — an orphaned call
            # returning late must not clobber a result that superseded it.
            result_row = session.execute(
                update(TasteState)
                .where(TasteState.id == 1)
                .where(TasteState.recompute_job_id == job_id)
                .values(
                    taste_profile=taste_profile.model_dump(mode="json"),
                    reference_observations=[item.model_dump(mode="json") for item in observations],
                    last_recomputed_at=now,
                )
            )
            if result_row.rowcount == 1:
                session.add(
                    LearningSnapshotState(
                        id=f"learn-{uuid4().hex[:8]}",
                        created_at=now,
                        reason="Recomputed taste profile from wardrobe samples, offer feedback, and manual taste note.",
                        changed_weights=[],
                        summary=taste_profile.summary,
                        old_prompt=previous_profile.taste_prompt if previous_profile else None,
                        new_prompt=taste_profile.taste_prompt,
                        old_taste_profile=previous_profile.model_dump(mode="json") if previous_profile else None,
                        new_taste_profile=taste_profile.model_dump(mode="json"),
                        source_counts=taste_profile.source_counts,
                    )
                )
                session.flush()
                logger.info("taste recompute persisted profile version=%d", taste_profile.version)
            else:
                logger.warning(
                    "taste recompute job=%s superseded before persisting — discarding result", job_id
                )

        input_tokens = int(accumulated["input_tokens"])
        output_tokens = int(accumulated["output_tokens"])

        with session_scope() as session:
            logger.info(
                "taste recompute completed duration=%.1fs cost_usd=%.4f input_tokens=%d output_tokens=%d",
                perf_counter() - started,
                accumulated["cost_usd"],
                input_tokens,
                output_tokens,
            )
            return TasteRecomputeResult(
                snapshot=self._snapshot_in_session(session),
                cost_usd=float(accumulated["cost_usd"]),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                observation_cache=observation_cache,
            )
