"""Smoke-check the actual production judge (with the new structured-block anchors) on the
4-pic held-out grid. 3 trials. Should remain in the 7-8 avg / range <=4 territory."""

from __future__ import annotations

import json
from pathlib import Path

from vsniper.core.config import Settings
from vsniper.domain.contracts import LabeledExample, ReferenceObservation, TasteProfile
from vsniper.integrations.openai.client import CandidateImageInput, OpenAITasteClient

ROOT = Path(__file__).resolve().parent.parent
PICS_DIR = ROOT / "pics_of_clothes_i_like"


def _load_taste() -> TasteProfile:
    raw = json.loads((ROOT / "scripts" / "out_taste_revised.json").read_text())
    return TasteProfile(
        summary=raw.get("core_aesthetic_summary", ""),
        taste_prompt=raw["taste_prompt"],
        core_aesthetic_summary=raw.get("core_aesthetic_summary", ""),
        likes=raw.get("likes", []),
        dislikes_or_penalties=raw.get("dislikes_or_penalties", []),
        instant_alert_examples=raw.get("instant_alert_examples", []),
        instant_reject_examples=raw.get("instant_reject_examples", []),
        scoring_rubric=raw.get("scoring_rubric", {}),
        transparency_labels=raw.get("transparency_labels", []),
    )


def _load_observations() -> list[ReferenceObservation]:
    raw = json.loads((ROOT / "scripts" / "out_observe_revised.json").read_text())["observations"]
    pics = sorted(p.name for p in PICS_DIR.glob("*.jpg"))
    return [
        ReferenceObservation(
            image_id=pics[i],
            file_name=pics[i],
            garment_type=obs.get("garment_type", ""),
            silhouette_and_cut=obs.get("silhouette_and_cut", ""),
            color_palette=obs.get("color_palette", ""),
            fabric_and_texture=obs.get("fabric_and_texture", ""),
            prints_or_patterns=obs.get("prints_or_patterns", ""),
            details_and_hardware=obs.get("details_and_hardware", ""),
            era_or_subculture=obs.get("era_or_subculture", ""),
            vibe_keywords=obs.get("vibe_keywords", []),
        )
        for i, obs in enumerate(raw[: len(pics)])
    ]


def main() -> None:
    settings = Settings()
    client = OpenAITasteClient(settings)
    taste = _load_taste()
    obs = _load_observations()
    pics = sorted(PICS_DIR.glob("*.jpg"))
    anchor_pics = pics[:4]
    held_out_pics = pics[4:8]

    liked_anchors = [
        LabeledExample(
            candidate_id=f"anchor-{p.stem}",
            verdict="like",
            score_10=9,
            title=f"{obs[i].era_or_subculture.split('/')[0].strip()} {obs[i].garment_type}".strip().title()[:80],
            brand="Vintage",
            user_comment="favourite recent find",
            observation=obs[i],
        )
        for i, p in enumerate(anchor_pics)
    ]
    candidates = [CandidateImageInput(candidate_id=p.stem, image_bytes=p.read_bytes()) for p in held_out_pics]

    all_scores: list[list[int]] = []
    for trial in range(3):
        result = client.judge_candidate_grid(
            taste_profile=taste,
            candidates=candidates,
            liked_anchors=liked_anchors,
            disliked_anchors=[],
        )
        scores: list[int] = []
        print(f"=== trial {trial + 1} ===")
        for cand in candidates:
            j = result.judgments[cand.candidate_id]
            scores.append(j.score)
            print(f"  {j.position:12s} {j.score}/10  {j.explanation}")
        all_scores.append(scores)
        avg = sum(scores) / len(scores)
        print(f"  -> avg={avg:.2f}  min={min(scores)}  range={max(scores) - min(scores)}\n")

    flat = [s for trial in all_scores for s in trial]
    print(f"=== AGGREGATE over {len(all_scores)} trials ===")
    print(f"all scores: {flat}")
    print(f"mean={sum(flat)/len(flat):.2f}  min={min(flat)}  count_below_5={sum(1 for s in flat if s < 5)}")


if __name__ == "__main__":
    main()
