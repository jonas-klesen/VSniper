from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from vsniper.core.config import Settings
from vsniper.core.database import session_scope
from vsniper.db.models import AppSettingsState, Candidate, Search
from vsniper.domain.contracts import (
    CLOTHING_ITEM_LABELS,
    LabeledExample,
    ScanMode,
    ScoreTrace,
    SearchCategoryOption,
    SearchDraftApplyResult,
    SearchFilter,
    SearchRecord,
    SearchRunResult,
    SearchUpdate,
    SessionHealth,
    SettingsSnapshot,
    SettingsUpdate,
    TasteProfile,
    VintedSizesResult,
)
from vsniper.domain.scoring.service import (
    build_failed_judgment_trace,
    build_judgment_trace,
)
from vsniper.integrations.openai.client import (
    CandidateGridResult,
    CandidateImageInput,
    OpenAIIntegrationError,
    OpenAITasteClient,
    UsageCallback,
)
from vsniper.integrations.openai.pricing import compute_cost
from vsniper.integrations._retry import retry_transient
from vsniper.integrations.vinted.client import VintedClient, VintedClientError, VintedSearchError, VintedSessionError
from vsniper.integrations.vinted.categories import (
    category_options_by_clothing_item,
    clothing_items_for_size_group,
    ensure_category_filter_for_clothing_item,
)
from vsniper.services._mapping import (
    ResolvedAiModel,
    as_aware,
    build_session_health,
    extract_condition,
    integration_configuration,
    resolve_ai_model,
    search_to_record,
    settings_to_contract,
)
from vsniper.services.error_service import ErrorService
from vsniper.services.operations_service import (
    CLAIM_STALE_THRESHOLD,
    close_orphaned_running_runs,
    create_search_run,
    finish_search_run,
)
from vsniper.services.search_defaults import CANONICAL_SEARCH_ORDER
from vsniper.services.telegram_service import TelegramService
from vsniper.services.taste_service import TasteService

SESSION_HEALTH_REFRESH_INTERVAL = timedelta(minutes=15)
_CACHE_MAX_AGE_DAYS = 30
_CACHE_MAX_SIZE_MB = 500
_MANUAL_RUN_STALE_AFTER = timedelta(minutes=30)


def _apply_size_filter(filters: list[SearchFilter], sizes: list[str]) -> list[SearchFilter]:
    without_size = [f for f in filters if f.field.strip().lower() != "size"]
    if not sizes:
        return without_size
    size_filter = SearchFilter(field="size", label="Sizes", mode="include", values=sizes)
    return [size_filter, *without_size]


def _is_price_filter(filter_: SearchFilter) -> bool:
    """Price ceilings are a user-owned budget setting, never taste-draft input."""
    return filter_.field.strip().lower() == "price"


def _normalize_blocked_brands(brands: list[str]) -> list[str]:
    """Strip, drop empties, and dedupe case-insensitively while keeping first-seen casing/order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for brand in brands:
        stripped = brand.strip()
        if not stripped:
            continue
        key = stripped.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(stripped)
    return normalized


def _filter_blocked_brands(raw_candidates: list[dict], blocked_brands: list[str]) -> list[dict]:
    """Drop candidates from a hard-blocked brand before judging — they should never be rated."""
    if not blocked_brands:
        return raw_candidates
    blocked = {brand.lower() for brand in blocked_brands}
    return [
        candidate
        for candidate in raw_candidates
        if str(candidate.get("brand", "")).strip().lower() not in blocked
    ]


def _canonical_grid_size(value: int) -> int:
    parsed = int(value)
    return parsed if parsed in {1, 4, 9} else 1


def _judge_request_concurrency(value: int) -> int:
    return max(1, min(16, int(value)))


def _alert_threshold(value: int | None, *, fallback: int = 95) -> int:
    if value is None:
        value = fallback
    return max(1, min(100, int(value)))


_logger = logging.getLogger(__name__)


class SearchRunAlreadyClaimed(RuntimeError):
    """Raised when another API or worker process already owns the DB-backed run lease."""


class SearchClothingItemImmutable(ValueError):
    """Raised when a caller tries to move a canonical search to another clothing bucket."""


class SearchNotConfigured(ValueError):
    """Raised when a canonical search is run before it has a usable Vinted query."""


def _evict_stale_cache(cache_dir: Path) -> None:
    """Remove files older than _CACHE_MAX_AGE_DAYS and trim to _CACHE_MAX_SIZE_MB (LRU) if needed."""
    if not cache_dir.exists():
        return
    cutoff = time.time() - _CACHE_MAX_AGE_DAYS * 86400
    files = [(p, p.stat()) for p in cache_dir.iterdir() if p.is_file()]
    for path, st in files:
        if st.st_mtime < cutoff:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    files = []
    for p in cache_dir.iterdir():
        if p.is_file():
            try:
                files.append((p, p.stat()))
            except OSError:
                pass
    total_bytes = sum(st.st_size for _, st in files)
    if total_bytes > _CACHE_MAX_SIZE_MB * 1024 * 1024:
        for path, st in sorted(files, key=lambda x: x[1].st_atime):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            total_bytes -= st.st_size
            if total_bytes <= _CACHE_MAX_SIZE_MB * 1024 * 1024:
                break


class SearchService:
    def __init__(
        self,
        settings: Settings,
        vinted_client: VintedClient,
        taste_client: OpenAITasteClient,
        preferences: TasteService,
        telegram: TelegramService,
        errors: ErrorService | None = None,
    ) -> None:
        self.settings = settings
        self.vinted_client = vinted_client
        self.taste_client = taste_client
        self.preferences = preferences
        self.telegram = telegram
        self.errors = errors

    def all(self) -> list[SearchRecord]:
        with session_scope() as session:
            settings = self._get_settings_state(session)
            records = session.scalars(select(Search)).all()
            ordered = sorted(
                records,
                key=lambda item: (CANONICAL_SEARCH_ORDER.get(item.clothing_item, len(CANONICAL_SEARCH_ORDER)), item.created_at),
            )
            return [search_to_record(item, default_alert_threshold=settings.alert_threshold) for item in ordered]

    def update(self, search_id: str, payload: SearchUpdate) -> SearchRecord:
        with session_scope() as session:
            model = session.get(Search, search_id)
            if model is None:
                raise KeyError(search_id)
            if payload.clothing_item != model.clothing_item:
                raise SearchClothingItemImmutable(
                    "A saved search is fixed to its clothing bucket. Edit the target bucket's existing search instead."
                )
            filters = ensure_category_filter_for_clothing_item(payload.filters, model.clothing_item, strict=True)

            model.name = CLOTHING_ITEM_LABELS.get(model.clothing_item, model.name)
            model.query = payload.query
            model.region = payload.region
            model.filters = [item.model_dump(mode="json") for item in filters]
            model.alert_threshold = payload.alert_threshold
            model.enabled = payload.enabled
            session.flush()
            settings = session.get(AppSettingsState, 1)
            return search_to_record(
                model,
                default_alert_threshold=settings.alert_threshold if settings is not None else 9,
            )

    def toggle(self, search_id: str) -> SearchRecord:
        with session_scope() as session:
            model = session.get(Search, search_id)
            if model is None:
                raise KeyError(search_id)
            model.enabled = not model.enabled
            session.flush()
            settings = session.get(AppSettingsState, 1)
            return search_to_record(
                model,
                default_alert_threshold=settings.alert_threshold if settings is not None else 9,
            )

    def fetch_profile_sizes(self) -> VintedSizesResult:
        with session_scope() as session:
            region = self._get_settings_state(session).vinted_region
        sizes = self.vinted_client.fetch_user_sizes(region=region)
        return VintedSizesResult(sizes=sizes, region=region)

    def apply_profile_sizes_to_all(self) -> VintedSizesResult:
        with session_scope() as session:
            region = self._get_settings_state(session).vinted_region
        grouped = self.vinted_client.fetch_user_sizes_grouped(region=region)
        sizes_by_item: dict[str, list[str]] = {}
        unmapped_descriptions: set[str] = set()
        for entry in grouped:
            items = clothing_items_for_size_group(entry["group_description"])
            if not items:
                unmapped_descriptions.add(entry["group_description"])
                continue
            for item in items:
                sizes_by_item.setdefault(item, [])
                if entry["title"] not in sizes_by_item[item]:
                    sizes_by_item[item].append(entry["title"])
        if unmapped_descriptions:
            _logger.info(
                "Skipped Vinted size groups with no clothing-item mapping: %s",
                ", ".join(sorted(unmapped_descriptions)),
            )

        with session_scope() as session:
            models = session.scalars(select(Search)).all()
            for model in models:
                sizes = sizes_by_item.get(model.clothing_item)
                if not sizes:
                    continue
                existing = [SearchFilter.model_validate(f) for f in model.filters]
                model.filters = [f.model_dump(mode="json") for f in _apply_size_filter(existing, sizes)]
            session.flush()

        flat_sizes = sorted({entry["title"] for entry in grouped})
        return VintedSizesResult(sizes=flat_sizes, region=region)

    def category_options(self) -> dict[str, SearchCategoryOption]:
        return {
            clothing_item: SearchCategoryOption.model_validate(options)
            for clothing_item, options in category_options_by_clothing_item().items()
        }

    def apply_generated_search_drafts(self, profile_version: int | None = None) -> SearchDraftApplyResult:
        with session_scope() as session:
            state = self.preferences.get_taste_state(session)
            if not state.taste_profile:
                return SearchDraftApplyResult(
                    requested_profile_version=profile_version,
                    summary="No taste profile exists yet; there are no generated drafts to apply.",
                )
            profile = TasteProfile.model_validate(state.taste_profile)
            if profile_version is not None and profile.version != profile_version:
                return SearchDraftApplyResult(
                    profile_version=profile.version,
                    requested_profile_version=profile_version,
                    stale=True,
                    summary=(
                        f"Taste profile is now v{profile.version}, but this button was for v{profile_version}. "
                        "No searches were changed."
                    ),
                )

            applied: list[str] = []
            unchanged: list[str] = []
            skipped: list[str] = []
            for draft in profile.generated_searches:
                search = session.scalar(select(Search).where(Search.clothing_item == draft.clothing_item).limit(1))
                if search is None:
                    skipped.append(draft.clothing_item)
                    continue
                # Preserve user-owned sizes and budget — taste drafts only describe what to find.
                existing_size_filters = [f for f in (search.filters or []) if (f.get("field") or "").strip().lower() == "size"]
                existing_price_filters = [
                    f for f in (search.filters or []) if (f.get("field") or "").strip().lower() == "price"
                ]
                draft_filters = [
                    f for f in draft.filters if f.field.strip().lower() != "size" and not _is_price_filter(f)
                ]
                filters = ensure_category_filter_for_clothing_item(draft_filters, draft.clothing_item, strict=True)
                filter_payload = [
                    *existing_size_filters,
                    *existing_price_filters,
                    *(item.model_dump(mode="json") for item in filters),
                ]
                changed = (
                    search.name != CLOTHING_ITEM_LABELS.get(search.clothing_item, search.name)
                    or search.query != draft.query
                    or (search.filters or []) != filter_payload
                )
                search.name = CLOTHING_ITEM_LABELS.get(search.clothing_item, search.name)
                search.query = draft.query
                # Preserve existing region — taste drafts shouldn't override per-search region
                search.filters = filter_payload
                if changed:
                    applied.append(search.clothing_item)
                else:
                    unchanged.append(search.clothing_item)

            session.flush()
            return SearchDraftApplyResult(
                profile_version=profile.version,
                requested_profile_version=profile_version,
                applied_searches=len(applied),
                unchanged_searches=len(unchanged),
                skipped_searches=len(skipped),
                applied=applied,
                unchanged=unchanged,
                skipped=skipped,
                summary=(
                    f"Applied generated drafts from taste profile v{profile.version}."
                    if applied or unchanged
                    else f"Taste profile v{profile.version} has no generated drafts to apply."
                ),
            )

    def run_live(self, search_id: str, *, already_claimed: bool = False, trigger: str = "manual") -> SearchRunResult:
        if not already_claimed:
            if not self.claim_for_run(search_id, timedelta(0), stale_after=_MANUAL_RUN_STALE_AFTER):
                if not self._search_exists(search_id):
                    raise KeyError(search_id)
                raise SearchRunAlreadyClaimed(search_id)
        return self._run(search_id, mode="live", trigger=trigger)

    def run_all_enabled(self) -> list[SearchRunResult]:
        """Run every enabled search live, sequentially. Per-search failures are skipped so one
        broken search does not abort the rest; each run claims the DB lock like a normal live run."""
        with session_scope() as session:
            ids = list(
                session.scalars(select(Search.id).where(Search.enabled == True))  # noqa: E712
            )
        results: list[SearchRunResult] = []
        for search_id in ids:
            try:
                results.append(self.run_live(search_id))
            except SearchRunAlreadyClaimed:
                continue
            except (SearchNotConfigured, VintedClientError) as exc:
                _logger.warning("run_all_enabled: skipping search %s — %s", search_id, exc)
                continue
            except Exception:
                _logger.warning("run_all_enabled: skipping search %s due to unexpected error", search_id, exc_info=True)
                continue
        return results

    def claim_for_run(
        self,
        search_id: str,
        min_interval: timedelta,
        *,
        stale_after: timedelta | None = None,
    ) -> bool:
        """Atomically marks a search as running now. Returns False if another worker already claimed it or it ran too recently.

        Re-claim is allowed when:
        - never claimed before, OR
        - the previous run finished (run_status is idle/completed/failed) and min_interval has elapsed, OR
        - the claim is stale (> stale_after, default 3× min_interval) to recover from a worker that crashed mid-run.

        Keying the "finished" check on run_status rather than last_run_at means a run that
        failed before persisting (run_status="failed") becomes reclaimable after one interval
        instead of being locked out until the 3× stale cutoff.
        """
        now = datetime.now(UTC)
        normal_cutoff = now - min_interval
        stale_cutoff = now - (stale_after if stale_after is not None else min_interval * 3)
        with session_scope() as session:
            finished_and_due = (Search.last_claimed_at < normal_cutoff) & Search.run_status.in_(
                ("idle", "completed", "failed")
            )
            result = session.execute(
                update(Search)
                .where(Search.id == search_id)
                .where(
                    (Search.last_claimed_at == None)  # noqa: E711
                    | finished_and_due
                    | (Search.last_claimed_at < stale_cutoff)
                )
                .values(last_claimed_at=now, run_status="running", cancel_requested_at=None)
            )
            claimed = result.rowcount == 1
        if claimed:
            self._close_orphaned_runs(search_id)
        return claimed

    def _close_orphaned_runs(self, search_id: str) -> None:
        for run_id in close_orphaned_running_runs(search_id):
            errors = getattr(self, "errors", None)
            if errors is not None:
                errors.record(
                    source="search",
                    operation="recover_orphaned_run",
                    summary="Search run was orphaned",
                    message="The worker process stopped before the search run could finish.",
                    details={"search_id": search_id, "run_id": run_id},
                    related_entity_type="search_run",
                    related_entity_id=run_id,
                )

    @staticmethod
    def _search_exists(search_id: str) -> bool:
        with session_scope() as session:
            return session.get(Search, search_id) is not None

    @staticmethod
    def _normalise_listing_payload(raw: dict, search_record: SearchRecord) -> dict:
        return {
            "external_item_id": raw.get("external_item_id", raw["id"]),
            "search_id": search_record.id,
            "search_name": search_record.name,
            "clothing_item": search_record.clothing_item,
            "query": search_record.query,
            "region": search_record.region,
            "title": raw["title"],
            "brand": raw["brand"],
            "price_eur": raw["price_eur"],
            "size": raw["size"],
            "url": raw["url"],
            "image_urls": raw["image_urls"],
            "description": raw.get("description"),
            "matched_filters": [item.model_dump(mode="json") for item in search_record.filters],
            "raw_listing": raw.get("raw_listing", raw),
        }

    def _candidate_cache_path(self, candidate_id: str, *, image_index: int = 0) -> Path:
        cache_dir = self.settings.resolve_path(self.settings.cache_dir) / "candidate-images"
        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in candidate_id)
        suffix = "" if image_index == 0 else f"-{image_index + 1}"
        return cache_dir / f"{safe_name}{suffix}.jpg"

    @staticmethod
    def _is_decodable_image(content: bytes) -> bool:
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError):
            return False
        return True

    def _download_one_image(
        self,
        *,
        image_url: str,
        cache_path: Path,
    ) -> tuple[bytes, str, Path] | None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            raw = cache_path.read_bytes()
            if self._is_decodable_image(raw):
                return raw, "image/jpeg", cache_path
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.get(image_url, headers={"User-Agent": "vsniper/0.1 (+https://local.vsniper)"})
            response.raise_for_status()
        content_type = response.headers.get("content-type", "image/jpeg").split(";", maxsplit=1)[0]
        if not content_type.startswith("image/"):
            return None
        if not self._is_decodable_image(response.content):
            return None
        tmp_path = cache_path.with_suffix(".tmp")
        tmp_path.write_bytes(response.content)
        tmp_path.rename(cache_path)
        return response.content, content_type, cache_path

    def _download_candidate_image(self, *, candidate_id: str, image_urls: list[str]) -> CandidateImageInput | None:
        if not image_urls:
            return None
        urls = image_urls[:4]
        cache_paths = [self._candidate_cache_path(candidate_id, image_index=i) for i in range(len(urls))]

        usable: list[tuple[bytes, str, Path] | None] = [None] * len(urls)
        with ThreadPoolExecutor(max_workers=len(urls)) as pool:
            futures = {
                pool.submit(
                    self._download_one_image,
                    image_url=url,
                    cache_path=cache_paths[i],
                ): i
                for i, url in enumerate(urls)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    usable[idx] = future.result(timeout=20)
                except Exception:
                    usable[idx] = None

        results = [r for r in usable if r is not None]
        if not results:
            return None
        primary_bytes, primary_mime, _ = results[0]
        return CandidateImageInput(
            candidate_id=candidate_id,
            image_bytes=primary_bytes,
            mime_type=primary_mime,
            extra_image_bytes=[content for content, _, _ in results[1:]],
            cache_paths=[path for _, _, path in results],
        )

    @staticmethod
    def _memoized_judgments(
        *, candidate_ids: list[str], profile_version: int, alert_threshold: int
    ) -> dict[str, ScoreTrace]:
        """Return stored VLM judgments still valid for the current taste-profile version.

        Reads (no write lock held) candidate rows that were already VLM-judged under this exact
        profile version, so the scan skips re-downloading and re-judging unchanged listings.
        build_judgment_trace stamps the profile version and alert threshold into ScoreTrace,
        which form the equality key. "failed" rows are not reused, so a transient image/VLM
        failure is retried on the next cycle.
        """
        if not candidate_ids:
            return {}
        reused: dict[str, ScoreTrace] = {}
        with session_scope() as session:
            rows = session.execute(
                select(Candidate.id, Candidate.grading_stage, Candidate.score_trace).where(
                    Candidate.id.in_(candidate_ids)
                )
            ).all()
        for candidate_id, grading_stage, score_trace_json in rows:
            if grading_stage != "vlm_judged" or not score_trace_json:
                continue
            if score_trace_json.get("prompt_version") != profile_version:
                continue
            if score_trace_json.get("threshold") != round(alert_threshold / 100, 3):
                continue
            try:
                reused[candidate_id] = ScoreTrace.model_validate(score_trace_json)
            except ValidationError:
                _logger.warning("could not reuse stored score trace for candidate %s", candidate_id)
        return reused

    def _judge_candidates(
        self,
        raw_candidates: list[dict],
        search_record: SearchRecord,
        taste_profile: TasteProfile,
        db_settings: AppSettingsState,
        judge_model: ResolvedAiModel,
        judge_fallback_model: ResolvedAiModel | None,
        mode: ScanMode,
        manual_note: str = "",
        on_usage: UsageCallback | None = None,
        new_judged_ids: set[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, ScoreTrace], dict[str, str], int, int]:
        traces: dict[str, ScoreTrace] = {}
        stages: dict[str, str] = {}
        raw_by_candidate_id: dict[str, dict] = {}
        candidate_rows: list[tuple[str, dict]] = []
        for raw in raw_candidates:
            external_item_id = raw.get("external_item_id", raw["id"])
            candidate_id = f"{search_record.id}:{external_item_id}"
            raw_by_candidate_id[candidate_id] = raw
            candidate_rows.append((candidate_id, raw))

        persisted_alert_count = 0
        persisted_queued_deliveries = 0

        def _persist(candidate_ids: list[str]) -> None:
            nonlocal persisted_alert_count, persisted_queued_deliveries
            if not candidate_ids:
                return
            batch_raw = [raw_by_candidate_id[cid] for cid in candidate_ids]
            alert_count, queued, _judged = self._persist_candidate_batch(
                raw_candidates=batch_raw,
                search_record=search_record,
                taste_profile=taste_profile,
                score_traces=traces,
                stages=stages,
                mode=mode,
            )
            persisted_alert_count += alert_count
            persisted_queued_deliveries += queued

        # Skip VLM re-judging of candidates already judged under the current taste-profile version.
        # run_search returns the newest listings every cycle, so without this an unchanged listing
        # is re-downloaded and re-judged on every worker tick until it falls off page 1 — unbounded
        # VLM cost and alert decisions that flap as borderline items re-score across the threshold.
        # A stored judgment is reused only when it came from the VLM (grading_stage="vlm_judged") at
        # this exact profile version; "failed" rows and stale-version rows fall through and re-judge.
        # A taste recompute bumps the version, which invalidates the whole memo and forces re-judging.
        memoized = self._memoized_judgments(
            candidate_ids=[cid for cid, _ in candidate_rows],
            profile_version=taste_profile.version,
            alert_threshold=search_record.effective_alert_threshold,
        )
        selected: list[tuple[str, dict]] = []
        memoized_ids: list[str] = []
        for cid, raw in candidate_rows:
            cached_trace = memoized.get(cid)
            if cached_trace is not None:
                traces[cid] = cached_trace
                stages[cid] = "vlm_judged"
                memoized_ids.append(cid)
            else:
                selected.append((cid, raw))
        # Memoized candidates still need their listing fields (price, last_seen_at, ...) refreshed
        # even though their judgment is reused, so persist them like any other judged batch.
        _persist(memoized_ids)

        liked_anchors, disliked_anchors = self.preferences.latest_labeled_anchors(
            clothing_item=search_record.clothing_item,
            limit=5,
        )
        pending: list[CandidateImageInput] = []

        if selected and cancel_check is not None and cancel_check():
            selected = []

        if selected:
            _evict_stale_cache(self.settings.resolve_path(self.settings.cache_dir) / "candidate-images")
            max_listing_images = 4 if getattr(db_settings, "vlm_pack_multiple_listing_images", True) else 1
            download_failed_ids: list[str] = []
            with ThreadPoolExecutor(max_workers=min(len(selected), 8)) as pool:
                futures_in_order = [
                    (
                        pool.submit(
                            self._download_candidate_image,
                            candidate_id=cid,
                            image_urls=(raw.get("image_urls", []) or [])[:max_listing_images],
                        ),
                        cid,
                        raw,
                    )
                    for cid, raw in selected
                ]
                for future, candidate_id, raw in futures_in_order:
                    try:
                        image_input = future.result(timeout=30)
                    except FutureTimeoutError:
                        _logger.warning("image download timed out for candidate %s", candidate_id)
                        image_input = None
                    except Exception:
                        _logger.exception("image download failed for candidate %s", candidate_id)
                        image_input = None
                    if image_input is None:
                        stages[candidate_id] = "failed"
                        traces[candidate_id] = build_failed_judgment_trace(
                            title=raw["title"],
                            error="No usable candidate image was available for AI judging.",
                            model=judge_model.model_name,
                            alert_threshold=search_record.effective_alert_threshold,
                        )
                        download_failed_ids.append(candidate_id)
                    else:
                        image_input.title = raw.get("title", "")
                        image_input.brand = raw.get("brand", "")
                        image_input.condition = extract_condition(raw.get("raw_listing") or {})
                        image_input.description = str(raw.get("description") or "")
                        image_input.size = str(raw.get("size") or "")
                        pending.append(image_input)
            _persist(download_failed_ids)

        batch_size = _canonical_grid_size(db_settings.vlm_grid_size)
        batches = [pending[index : index + batch_size] for index in range(0, len(pending), batch_size)]
        if batches:
            judge_workers = min(len(batches), _judge_request_concurrency(db_settings.vlm_judge_parallel_requests))
            for start in range(0, len(batches), judge_workers):
                if cancel_check is not None and cancel_check():
                    break
                group = batches[start : start + judge_workers]
                with ThreadPoolExecutor(max_workers=len(group)) as pool:
                    futures = [
                        pool.submit(
                            self._judge_image_batch,
                            items=batch,
                            taste_profile=taste_profile,
                            liked_anchors=liked_anchors,
                            disliked_anchors=disliked_anchors,
                            manual_note=manual_note,
                            raw_by_candidate_id=raw_by_candidate_id,
                            judge_model=judge_model,
                            judge_fallback_model=judge_fallback_model,
                            judge_image_detail=self.settings.ai_judge_image_detail,
                            image_max_px=db_settings.ai_judge_image_max_px,
                            pack_multiple_listing_images=getattr(db_settings, "vlm_pack_multiple_listing_images", True),
                            allow_split=True,
                            on_usage=on_usage,
                            alert_threshold=search_record.effective_alert_threshold,
                        )
                        for batch in group
                    ]
                    for judge_future in as_completed(futures):
                        batch_traces, batch_stages = judge_future.result()
                        traces.update(batch_traces)
                        stages.update(batch_stages)
                        _persist(list(batch_traces.keys()))
        if new_judged_ids is not None:
            new_judged_ids.update(
                cid for cid, _ in selected if stages.get(cid) == "vlm_judged"
            )
        return traces, stages, persisted_alert_count, persisted_queued_deliveries

    def _judge_image_batch(
        self,
        *,
        items: list[CandidateImageInput],
        taste_profile: TasteProfile,
        liked_anchors: list[LabeledExample],
        disliked_anchors: list[LabeledExample],
        manual_note: str,
        raw_by_candidate_id: dict[str, dict],
        judge_model: ResolvedAiModel,
        judge_fallback_model: ResolvedAiModel | None,
        judge_image_detail: str,
        allow_split: bool,
        image_max_px: int = 512,
        pack_multiple_listing_images: bool = True,
        on_usage: UsageCallback | None = None,
        alert_threshold: int = 95,
    ) -> tuple[dict[str, ScoreTrace], dict[str, str]]:
        """Judge one grid of candidates, recovering from partial failures.

        A whole-call failure or any omitted grid position is retried once by re-judging the
        affected items as single-image grids (``allow_split`` guards against infinite recursion).
        This stops one bad image or a transient timeout from discarding the whole batch. Any
        primary judge failure (regardless of provider) retries once with the fallback model, if one
        is configured.
        """
        traces: dict[str, ScoreTrace] = {}
        stages: dict[str, str] = {}

        def _retry_individually(failed_items: list[CandidateImageInput]) -> None:
            for item in failed_items:
                item_traces, item_stages = self._judge_image_batch(
                    items=[item],
                    taste_profile=taste_profile,
                    liked_anchors=liked_anchors,
                    disliked_anchors=disliked_anchors,
                    manual_note=manual_note,
                    raw_by_candidate_id=raw_by_candidate_id,
                    judge_model=judge_model,
                    judge_fallback_model=judge_fallback_model,
                    judge_image_detail=judge_image_detail,
                    image_max_px=image_max_px,
                    pack_multiple_listing_images=pack_multiple_listing_images,
                    allow_split=False,
                    on_usage=on_usage,
                    alert_threshold=alert_threshold,
                )
                traces.update(item_traces)
                stages.update(item_stages)

        def _mark_failed(failed_items: list[CandidateImageInput], error: str) -> None:
            for item in failed_items:
                raw = raw_by_candidate_id[item.candidate_id]
                stages[item.candidate_id] = "failed"
                traces[item.candidate_id] = build_failed_judgment_trace(
                    title=raw["title"],
                    error=error,
                    model=judge_model.model_name,
                    alert_threshold=alert_threshold,
                )

        def _discard_cached_images(affected: list[CandidateImageInput]) -> None:
            # An undecodable image reached contact-sheet assembly, which means a poisoned
            # cache file from before decode-validation. Delete it so the next cycle re-fetches.
            for item in affected:
                paths = item.cache_paths or [self._candidate_cache_path(item.candidate_id)]
                for path in paths:
                    path.unlink(missing_ok=True)

        def _judge(model: ResolvedAiModel) -> CandidateGridResult:
            return self.taste_client.judge_candidate_grid(
                taste_profile=taste_profile,
                candidates=items,
                liked_anchors=liked_anchors,
                disliked_anchors=disliked_anchors,
                manual_note=manual_note or None,
                model=model.model_name,
                reasoning_effort=model.reasoning_effort,
                image_detail=judge_image_detail,
                ai_judge_provider=model.provider,
                local_vlm_base_url=model.local_base_url,
                image_max_px=image_max_px,
                pack_multiple_listing_images=pack_multiple_listing_images,
                on_usage=on_usage,
            )

        try:
            try:
                result = _judge(judge_model)
                effective_model = judge_model.model_name
            except OpenAIIntegrationError:
                if judge_fallback_model is None:
                    raise
                result = _judge(judge_fallback_model)
                effective_model = judge_fallback_model.model_name
        except (UnidentifiedImageError, OSError) as exc:
            # build_contact_sheet (inside judge_candidate_grid) couldn't decode a cached image.
            # This is not an OpenAIIntegrationError, so without this catch it would propagate
            # out of the batch future and fail the whole scan run. Drop the poisoned cache
            # file(s) and fall back to the same per-item recovery path.
            _discard_cached_images(items)
            if allow_split and len(items) > 1:
                _retry_individually(items)
            else:
                _mark_failed(items, f"Candidate image could not be decoded for AI judging: {exc}")
            return traces, stages
        except OpenAIIntegrationError as exc:
            if allow_split and len(items) > 1:
                _retry_individually(items)
            else:
                _mark_failed(items, str(exc))
            return traces, stages

        item_ids = {item.candidate_id for item in items}
        for candidate_id, judgment in result.judgments.items():
            if candidate_id not in item_ids:
                continue
            stages[candidate_id] = "vlm_judged"
            traces[candidate_id] = build_judgment_trace(
                judgment=judgment,
                taste_profile=taste_profile,
                model=effective_model,
                batch_id=result.batch_id,
                alert_threshold=alert_threshold,
            )

        missing = [item for item in items if item.candidate_id not in traces]
        if missing:
            if allow_split and len(items) > 1:
                _retry_individually(missing)
            else:
                _mark_failed(missing, "OpenAI returned null or omitted this grid position.")
        return traces, stages

    def _run(self, search_id: str, *, mode: ScanMode, trigger: str = "worker") -> SearchRunResult:
        run_id: int | None = None
        usage_lock = Lock()
        usage_totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        raw_candidates: list[dict] = []
        score_traces: dict[str, ScoreTrace] = {}
        stages: dict[str, str] = {}
        new_judged_ids: set[str] = set()
        failures_by_reason: dict[str, int] = {}
        vinted_status = "not_attempted"
        vinted_detail: str | None = None
        cancel_requested = False

        def _check_cancel() -> bool:
            # Only a live run can be cancelled from the UI — a preview/test run never claims the
            # search, so it has nothing to cooperatively stop for.
            nonlocal cancel_requested
            if mode != "live" or cancel_requested:
                return cancel_requested
            cancel_requested = self._is_cancel_requested(search_id)
            return cancel_requested

        def _track_scan_usage(
            _operation: str,
            model: str,
            input_tokens: int,
            output_tokens: int,
            cached_input_tokens: int = 0,
        ) -> None:
            cost = compute_cost(model, input_tokens, output_tokens, cached_input_tokens)
            with usage_lock:
                usage_totals["input_tokens"] += input_tokens
                usage_totals["output_tokens"] += output_tokens
                usage_totals["cost_usd"] += cost

        def _vinted_failure_status(exc: BaseException) -> str:
            if isinstance(exc, VintedSessionError):
                return "session_error"
            if isinstance(exc, VintedSearchError):
                return "search_error"
            if isinstance(exc, VintedClientError):
                return "error"
            return vinted_status

        def _fallback_used() -> bool:
            if judge_fallback_model is None:
                return False
            return any(
                trace.model == judge_fallback_model.model_name
                for candidate_id, trace in score_traces.items()
                if stages.get(candidate_id) == "vlm_judged"
            )

        # Phase 1 — read config + taste in a short transaction. expire_on_commit=False keeps the
        # loaded settings/record usable after the session closes, so no write lock is held during I/O.
        with session_scope() as session:
            search = session.get(Search, search_id)
            if search is None:
                raise KeyError(search_id)
            db_settings = self._get_settings_state(session)
            judge_model = resolve_ai_model(session, db_settings.judge_model_id)
            judge_fallback_model = resolve_ai_model(session, db_settings.judge_fallback_model_id)
            if judge_model is None:
                raise SearchNotConfigured("No judge model is configured. Set one on the Settings page.")
            search_record = search_to_record(search, default_alert_threshold=db_settings.alert_threshold)
            search_record = search_record.model_copy(
                update={
                    "filters": ensure_category_filter_for_clothing_item(
                        search_record.filters,
                        search_record.clothing_item,
                        strict=False,
                    )
                }
            )
            # The worker holds a long-lived VintedClient; re-apply the persisted cookie so a
            # cookie pasted in the web UI (another process) takes effect on the next scan instead
            # of after a restart. The DB is the source of truth for credentials.
            self.vinted_client.sync_persisted_credentials(
                db_settings.vinted_cookie or "", db_settings.vinted_refresh_token or ""
            )
            taste_profile = self.preferences.active_taste_profile(
                session,
                clothing_item=search_record.clothing_item,
            )
            manual_note = self.preferences.get_taste_state(session).manual_note.strip()
            session_health_needs_refresh = False
            if mode == "live":
                _, session_health_needs_refresh = self._session_health_needs_refresh(
                    db_settings, region=search_record.region
                )

        run_id = create_search_run(
            search_id=search_id,
            mode=mode,
            trigger=trigger,
        )

        if mode == "live":
            self._refresh_claim(search_id)
        try:
            # Phase 2 — network I/O (Vinted fetch + image downloads + VLM judging). No DB lock held.
            if not search_record.query.strip():
                raise SearchNotConfigured(
                    f"Search '{search_record.name}' needs a Vinted query before it can be run."
                )
            session_health_json = (
                self._fetch_session_health_json(region=search_record.region)
                if session_health_needs_refresh
                else None
            )
            if session_health_json is not None:
                vinted_status = str(session_health_json.get("status") or "unknown")
                vinted_detail = str(session_health_json.get("detail") or "")[:500] or None
            raw_candidates = retry_transient(
                lambda: self.vinted_client.run_search(search_record, validate_session=False),
                label=f"Vinted search '{search_record.name}'",
            )
            vinted_status = "ok"
            fetched_count = len(raw_candidates)
            raw_candidates = _filter_blocked_brands(raw_candidates, db_settings.blocked_brands or [])
            blocked_count = fetched_count - len(raw_candidates)
            vinted_detail = (
                f"Fetched {fetched_count} candidates from Vinted "
                f"({blocked_count} blocked by brand filter, {len(raw_candidates)} considered)."
                if blocked_count
                else f"Fetched {fetched_count} candidates from Vinted."
            )
            score_traces, stages, persisted_alert_count, persisted_queued_deliveries = self._judge_candidates(
                raw_candidates,
                search_record,
                taste_profile,
                db_settings,
                judge_model=judge_model,
                judge_fallback_model=judge_fallback_model,
                mode=mode,
                manual_note=manual_note,
                on_usage=_track_scan_usage,
                new_judged_ids=new_judged_ids,
                cancel_check=_check_cancel,
            )
            if mode == "live":
                self._refresh_claim(search_id)

            judged_count = sum(1 for s in stages.values() if s == "vlm_judged")
            if cancel_requested:
                # Candidates judged so far were already persisted incrementally by
                # _persist_candidate_batch inside _judge_candidates — nothing further to save.
                if run_id is not None:
                    finish_search_run(
                        run_id=run_id,
                        status="cancelled",
                        fetched_count=len(raw_candidates),
                        judged_count=judged_count,
                        new_judged_count=len(new_judged_ids),
                        alert_count=persisted_alert_count,
                        queued_delivery_count=persisted_queued_deliveries,
                        judge_provider=judge_model.provider,
                        judge_model=judge_model.model_name,
                        fallback_used=_fallback_used(),
                        vinted_status=vinted_status,
                        vinted_detail=vinted_detail,
                        cost_usd=float(usage_totals["cost_usd"]),
                        input_tokens=int(usage_totals["input_tokens"]),
                        output_tokens=int(usage_totals["output_tokens"]),
                    )
                self._clear_cancel_request(search_id)
                return SearchRunResult(
                    search_id=search_id,
                    mode=mode,
                    fetched_candidates=len(raw_candidates),
                    alert_candidates=persisted_alert_count,
                    queued_alert_deliveries=persisted_queued_deliveries,
                    run_id=run_id,
                    summary=(
                        f"Run for '{search_record.name}' was cancelled; "
                        f"{judged_count} of {len(raw_candidates)} fetched candidates were judged and saved."
                    ),
                )

            # Count failures for the run record
            failures_by_reason = {}
            for cid, stage in stages.items():
                if stage == "failed":
                    trace = score_traces.get(cid)
                    reason = "unknown"
                    if trace:
                        reason = trace.summary[:100] if trace.summary else "unknown"
                    failures_by_reason[reason] = failures_by_reason.get(reason, 0) + 1

            # Phase 3 — finalize run stats in a short transaction. Candidates were already
            # persisted incrementally by _judge_candidates as each batch was judged.
            with session_scope() as session:
                search = session.get(Search, search_id)
                if search is None:
                    raise KeyError(search_id)
                if session_health_json is not None:
                    self._get_settings_state(session).session_health = session_health_json
                result = self._persist_run(
                    session=session,
                    search=search,
                    search_record=search_record,
                    mode=mode,
                    fetched_count=len(raw_candidates),
                    alert_count=persisted_alert_count,
                    judged_count=judged_count,
                    queued_alert_deliveries=persisted_queued_deliveries,
                )

            if run_id is not None:
                finish_search_run(
                    run_id=run_id,
                    status="completed",
                    fetched_count=result.fetched_candidates,
                    judged_count=judged_count,
                    new_judged_count=len(new_judged_ids),
                    alert_count=result.alert_candidates,
                    queued_delivery_count=result.queued_alert_deliveries,
                    failures_by_reason=failures_by_reason or None,
                    judge_provider=judge_model.provider,
                    judge_model=judge_model.model_name,
                    fallback_used=_fallback_used(),
                    vinted_status=vinted_status,
                    vinted_detail=vinted_detail,
                    cost_usd=float(usage_totals["cost_usd"]),
                    input_tokens=int(usage_totals["input_tokens"]),
                    output_tokens=int(usage_totals["output_tokens"]),
                )

            return result.model_copy(update={"run_id": run_id})
        except Exception as exc:
            if isinstance(exc, VintedClientError):
                vinted_status = _vinted_failure_status(exc)
                vinted_detail = str(exc)[:500]
            if run_id is not None:
                error_msg = str(exc)[:500]
                finish_search_run(
                    run_id=run_id,
                    status="failed",
                    fetched_count=len(raw_candidates),
                    judged_count=sum(1 for s in stages.values() if s == "vlm_judged"),
                    new_judged_count=len(new_judged_ids),
                    alert_count=sum(1 for trace in score_traces.values() if trace.decision == "alert"),
                    failures_by_reason=failures_by_reason or None,
                    judge_provider=judge_model.provider,
                    judge_model=judge_model.model_name,
                    fallback_used=_fallback_used(),
                    vinted_status=vinted_status,
                    vinted_detail=vinted_detail,
                    cost_usd=float(usage_totals["cost_usd"]),
                    input_tokens=int(usage_totals["input_tokens"]),
                    output_tokens=int(usage_totals["output_tokens"]),
                    error=error_msg,
                )
            # Only a live run holds the claim (test_run/preview never claims it), so only a
            # live run may flip run_status to "failed". A failed preview must leave a concurrent
            # worker's "running" claim untouched, or claim_for_run would let a second worker
            # reclaim and re-scan mid-run. The preview's exception still propagates to its caller.
            if mode == "live":
                self._mark_run_failed(search_id)
            errors = getattr(self, "errors", None)
            if errors is not None:
                errors.record(
                    source="search",
                    operation="run_search",
                    summary="Search run failed",
                    exception=exc,
                    details={
                        "search_id": search_id,
                        "run_id": run_id,
                        "mode": mode,
                        "vinted_status": vinted_status,
                        "vinted_detail": vinted_detail,
                        "fetched_count": len(raw_candidates),
                    },
                    related_entity_type="search_run" if run_id is not None else "search",
                    related_entity_id=run_id if run_id is not None else search_id,
                )
            raise

    def _persist_candidate_batch(
        self,
        *,
        raw_candidates: list[dict],
        search_record: SearchRecord,
        taste_profile: TasteProfile,
        score_traces: dict[str, ScoreTrace],
        stages: dict[str, str],
        mode: ScanMode,
    ) -> tuple[int, int, int]:
        """Upserts a batch of already-judged candidates immediately.

        Called incrementally from `_judge_candidates` as each memoized/failed/judged group is
        ready, so a run that is cancelled or crashes mid-scan keeps whatever it already scored
        instead of losing everything (persistence used to happen once, at the very end, in
        `_persist_run`). Upserting is idempotent, so re-persisting the same candidate later is
        harmless. Returns (alert_count, queued_alert_deliveries, judged_count) for this batch.
        """
        if not raw_candidates:
            return 0, 0, 0
        observed_at = datetime.now(UTC)
        alert_count = 0
        judged_count = 0
        queued_alert_deliveries = 0
        with session_scope() as session:
            search = session.get(Search, search_record.id)
            if search is None:
                return 0, 0, 0
            for raw in raw_candidates:
                external_item_id = raw.get("external_item_id", raw["id"])
                candidate_id = f"{search.id}:{external_item_id}"
                score_trace = score_traces.get(candidate_id)
                if score_trace is None:
                    continue
                if score_trace.decision == "alert":
                    alert_count += 1

                grading_stage = stages.get(candidate_id, "vlm_judged")
                if grading_stage == "vlm_judged":
                    judged_count += 1

                # Upsert rather than ORM get-or-create: a preview and a live worker scan can run
                # concurrently against the same search, both read "no row" for a new candidate, and
                # both INSERT — the loser's commit would IntegrityError and roll back the whole persist.
                # on_conflict_do_update resolves the collision as an update instead. Same dialect-upsert
                # convention as TelegramService.queue_delivery.
                values = {
                    "id": candidate_id,
                    "search_id": search.id,
                    "external_item_id": external_item_id,
                    "clothing_item": search_record.clothing_item,
                    "title": raw["title"],
                    "brand": raw["brand"],
                    "price_eur": raw["price_eur"],
                    "size": raw["size"],
                    "url": raw["url"],
                    "image_urls": raw["image_urls"],
                    "source_region": search.region,
                    "matched_filters": [f"{item.label}: {', '.join(item.values)}" for item in search_record.filters],
                    "matched_preferences": taste_profile.transparency_labels,
                    "features": [feature.model_dump(mode="json") for feature in raw["features"]],
                    "normalized_listing": self._normalise_listing_payload(raw, search_record),
                    "first_seen_at": raw.get("created_at") or observed_at,
                    "last_seen_at": observed_at,
                    "last_scan_mode": mode,
                    "extraction_status": "completed" if raw["features"] else "pending",
                    "extraction_error": None,
                    "score_trace": score_trace.model_dump(mode="json"),
                    "decision": score_trace.decision,
                    "final_score": score_trace.final_score,
                    "grading_stage": grading_stage,
                    # Insert-only fields, preserved on conflict:
                    "feedback": "unknown",
                    "created_at": observed_at,
                }
                stmt = sqlite_insert(Candidate).values(**values)
                # On conflict, refresh everything except identity/feedback/created_at, and keep the
                # earliest first_seen_at (mirrors the old `candidate.first_seen_at or ...`).
                preserved = {"id", "feedback", "created_at", "first_seen_at"}
                update_cols: dict[str, Any] = {key: stmt.excluded[key] for key in values if key not in preserved}
                update_cols["first_seen_at"] = func.coalesce(Candidate.first_seen_at, stmt.excluded.first_seen_at)
                session.execute(
                    stmt.on_conflict_do_update(index_elements=[Candidate.id], set_=update_cols)
                )

                errors = getattr(self, "errors", None)
                if grading_stage == "failed" and errors is not None:
                    errors.record(
                        source="candidate_judgment",
                        operation="judge_candidate",
                        summary="Candidate judgment failed after recovery attempts",
                        message=score_trace.explanation or score_trace.summary,
                        details={
                            "candidate_id": candidate_id,
                            "search_id": search.id,
                            "title": raw["title"],
                            "url": raw["url"],
                            "model": score_trace.model,
                            "mode": mode,
                            "labels": score_trace.labels,
                            "concerns": score_trace.concerns,
                        },
                        related_entity_type="candidate",
                        related_entity_id=candidate_id,
                        session=session,
                    )

                if mode == "live" and score_trace.decision == "alert":
                    # populate_existing forces a refresh from the row just written above, bypassing any
                    # stale identity-map entry, so the Telegram preview reads the persisted values.
                    candidate = session.get(Candidate, candidate_id, populate_existing=True)
                    assert candidate is not None  # just upserted above
                    queued_alert_deliveries += int(self.telegram.queue_delivery(session, candidate))
            session.flush()
        return alert_count, queued_alert_deliveries, judged_count

    def _persist_run(
        self,
        *,
        session: Session,
        search: Search,
        search_record: SearchRecord,
        mode: ScanMode,
        fetched_count: int,
        alert_count: int,
        judged_count: int,
        queued_alert_deliveries: int,
    ) -> SearchRunResult:
        """Finalizes a completed run's stats. Candidates are already persisted incrementally by
        `_persist_candidate_batch`, called from `_judge_candidates` as each batch is judged."""
        # Only a live run owns the claim lock (test_run never claims it), so only a live run
        # may release it and overwrite the user-facing "last run" stats. A web preview running
        # concurrently with a worker scan must not flip run_status to "completed".
        if mode == "live":
            search.last_run_at = datetime.now(UTC)
            search.last_found_count = alert_count
            search.last_fetched_count = fetched_count
            search.last_judged_count = judged_count
            search.run_status = "completed"
        session.flush()
        return SearchRunResult(
            search_id=search.id,
            mode=mode,
            fetched_candidates=fetched_count,
            alert_candidates=alert_count,
            queued_alert_deliveries=queued_alert_deliveries,
            summary=(
                f"{mode.capitalize()} Vinted run for '{search_record.name}' fetched {fetched_count} candidates, "
                f"produced {alert_count} alerts, and queued {queued_alert_deliveries} Telegram deliveries."
            ),
        )

    def _refresh_claim(self, search_id: str) -> None:
        try:
            with session_scope() as session:
                session.execute(
                    update(Search)
                    .where(Search.id == search_id)
                    .where(Search.run_status == "running")
                    .values(last_claimed_at=datetime.now(UTC))
                )
        except Exception:
            _logger.warning("claim refresh failed for search %s", search_id)

    @staticmethod
    def _mark_run_failed(search_id: str) -> None:
        """Record that a run raised before persisting, so claim_for_run can reclaim it next interval."""
        try:
            with session_scope() as session:
                session.execute(
                    update(Search)
                    .where(Search.id == search_id)
                    .values(run_status="failed", cancel_requested_at=None)
                )
        except Exception:
            _logger.exception("could not mark run_status=failed for search %s", search_id)

    @staticmethod
    def _is_cancel_requested(search_id: str) -> bool:
        with session_scope() as session:
            value = session.scalar(
                select(Search.cancel_requested_at).where(Search.id == search_id)
            )
            return value is not None

    @staticmethod
    def _clear_cancel_request(search_id: str) -> None:
        with session_scope() as session:
            session.execute(
                update(Search)
                .where(Search.id == search_id)
                .values(run_status="idle", cancel_requested_at=None)
            )

    def request_cancel(self, search_id: str) -> bool:
        """Asks a running search to stop, immediately if it's already stale.

        Returns False (no-op) if the search isn't currently running — there is nothing to
        cancel. Two cases:

        - Genuinely in-flight (claimed within CLAIM_STALE_THRESHOLD, same threshold the
          dashboard uses to badge a claim "stale"): sets `cancel_requested_at`. The scan thread
          is not interrupted; it polls this flag at a handful of checkpoints (before dispatching
          new image downloads / judge batches) and, on seeing it set, stops starting new work and
          returns, keeping whatever it already judged and persisted.
        - Already stale: the process that held the claim died without releasing it, so nothing
          is left to read a cooperative flag — setting one would be a silent no-op forever (this
          was the bug: the button reported success but the claim never cleared). Force-clear the
          claim immediately and close out any dangling SearchRun rows, exactly like
          claim_for_run's crash-recovery path (search_service.claim_for_run), just triggered on
          demand instead of waiting for the next scheduled scan attempt.
        """
        with session_scope() as session:
            search = session.get(Search, search_id)
            if search is None:
                raise KeyError(search_id)
            if search.run_status != "running":
                return False
            claimed_at = as_aware(search.last_claimed_at)
            is_stale = claimed_at is None or (datetime.now(UTC) - claimed_at) > CLAIM_STALE_THRESHOLD
            if is_stale:
                search.run_status = "idle"
                search.cancel_requested_at = None
            else:
                search.cancel_requested_at = datetime.now(UTC)
        if is_stale:
            self._close_orphaned_runs(search_id)
        return True

    @staticmethod
    def _get_settings_state(session: Session) -> AppSettingsState:
        model = session.get(AppSettingsState, 1)
        if model is None:
            raise RuntimeError("Settings row is missing from SQLite storage")
        return model

    @staticmethod
    def _coerce_session_health(payload: dict | None, *, region: str) -> SessionHealth:
        if payload:
            return SessionHealth.model_validate(payload)
        return build_session_health(region=region)

    @staticmethod
    def _session_health_is_stale(*, health: SessionHealth, region: str, now: datetime) -> bool:
        if health.region != region:
            return True
        if health.status == "missing":
            return False
        if health.last_validated_at is None:
            return True
        return health.last_validated_at < now - SESSION_HEALTH_REFRESH_INTERVAL

    def _session_health_needs_refresh(
        self, model: AppSettingsState, *, region: str, force: bool = False
    ) -> tuple[SessionHealth, bool]:
        """Read-only: returns (current health, whether a network refresh is due). No lock-held I/O."""
        current_health = self._coerce_session_health(model.session_health, region=model.vinted_region)
        now = datetime.now(UTC)
        needs = force or self._session_health_is_stale(health=current_health, region=region, now=now)
        return current_health, needs

    def _fetch_session_health_json(self, *, region: str, force: bool = False) -> dict:
        """Network fetch (no DB lock held) returning the JSON to persist into model.session_health."""
        return self.vinted_client.get_session_health(region=region, force=force).model_dump(mode="json")

    def get_app_settings(self) -> SettingsSnapshot:
        with session_scope() as session:
            model = self._get_settings_state(session)
            self.vinted_client.set_cookie(model.vinted_cookie or "")
            self.vinted_client.set_refresh_token(model.vinted_refresh_token or "")
            session.flush()
            return settings_to_contract(model, session)

    def get_blocked_brands(self) -> list[str]:
        with session_scope() as session:
            return list(self._get_settings_state(session).blocked_brands or [])

    def update_blocked_brands(self, brands: list[str]) -> list[str]:
        normalized = _normalize_blocked_brands(brands)
        with session_scope() as session:
            model = self._get_settings_state(session)
            model.blocked_brands = normalized
            session.flush()
            return list(model.blocked_brands)

    def get_scan_interval_seconds(self) -> int:
        """Read-only: the worker polls this each cycle so Settings-page edits take effect
        without a process restart."""
        with session_scope() as session:
            return self._get_settings_state(session).scan_interval_seconds

    def update_app_settings(self, payload: SettingsUpdate) -> SettingsSnapshot:
        # Phase 1 — read txn: resolve the effective cookie/region the health fetch will use
        # (payload fields left as None mean "unchanged", so fall back to the stored values).
        with session_scope() as session:
            current = self._get_settings_state(session)
            effective_cookie = (
                payload.vinted_cookie if payload.vinted_cookie is not None else current.vinted_cookie
            )
            effective_refresh_token = (
                payload.vinted_refresh_token
                if payload.vinted_refresh_token is not None
                else current.vinted_refresh_token
            )

        # Phase 2 — apply credentials to the client (in-memory) and fetch session health, no lock held.
        # Only force a live fetch if the cookie or region changed; unrelated field updates (e.g.
        # Telegram token) should not block on a Vinted round-trip.
        cookie_changed = payload.vinted_cookie is not None and payload.vinted_cookie != current.vinted_cookie
        region_changed = payload.vinted_region != current.vinted_region
        self.vinted_client.set_cookie(effective_cookie or "")
        self.vinted_client.set_refresh_token(effective_refresh_token or "")
        session_health_json = None
        if cookie_changed or region_changed:
            try:
                session_health_json = self._fetch_session_health_json(region=payload.vinted_region, force=True)
            except Exception as exc:
                _logger.warning("Settings saved without refreshed Vinted session health.", exc_info=True)
                errors = getattr(self, "errors", None)
                if errors is not None:
                    errors.record(
                        source="api",
                        operation="refresh_vinted_session_health",
                        summary="Vinted session health refresh failed",
                        exception=exc,
                        details={"region": payload.vinted_region},
                        related_entity_type="app_settings",
                        related_entity_id=1,
                    )

        # Phase 3 — write txn: apply all field updates and the freshly fetched health.
        with session_scope() as session:
            model = self._get_settings_state(session)
            model.vinted_region = payload.vinted_region
            if payload.vinted_cookie is not None:
                model.vinted_cookie = payload.vinted_cookie
            if payload.vinted_refresh_token is not None:
                model.vinted_refresh_token = payload.vinted_refresh_token
            if payload.telegram_bot_token is not None:
                model.telegram_bot_token = payload.telegram_bot_token.strip()
            if payload.telegram_chat_id is not None:
                model.telegram_chat_id = payload.telegram_chat_id.strip()
            if payload.telegram_webhook_url is not None:
                model.telegram_webhook_url = payload.telegram_webhook_url.strip()
            if payload.telegram_webhook_secret is not None:
                model.telegram_webhook_secret = payload.telegram_webhook_secret.strip()
            _, telegram_configured, _ = integration_configuration(
                db_cookie=model.vinted_cookie,
                telegram_bot_token=model.telegram_bot_token,
                telegram_chat_id=model.telegram_chat_id,
            )
            model.telegram_configured = telegram_configured
            if payload.judge_model_id is not None:
                model.judge_model_id = payload.judge_model_id or None
            if payload.judge_fallback_model_id is not None:
                model.judge_fallback_model_id = payload.judge_fallback_model_id or None
            if payload.learn_model_id is not None:
                model.learn_model_id = payload.learn_model_id or None
            if payload.observation_model_id is not None:
                model.observation_model_id = payload.observation_model_id or None
            if payload.vlm_grid_size is not None:
                model.vlm_grid_size = _canonical_grid_size(payload.vlm_grid_size)
            if payload.vlm_pack_multiple_listing_images is not None:
                model.vlm_pack_multiple_listing_images = payload.vlm_pack_multiple_listing_images
            if payload.vlm_judge_parallel_requests is not None:
                model.vlm_judge_parallel_requests = _judge_request_concurrency(payload.vlm_judge_parallel_requests)
            if payload.ai_judge_image_max_px is not None:
                model.ai_judge_image_max_px = max(64, min(2048, int(payload.ai_judge_image_max_px)))
            if payload.alert_threshold is not None:
                model.alert_threshold = _alert_threshold(payload.alert_threshold)
            if payload.scan_interval_seconds is not None:
                model.scan_interval_seconds = max(30, min(86400, int(payload.scan_interval_seconds)))
            if session_health_json is not None:
                model.session_health = session_health_json
            session.flush()
            return settings_to_contract(model, session)
