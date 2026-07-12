"""Validate the text-only prefilter end-to-end against the user's known-loved pieces.

Workflow:
1. Reuse `scripts/out_observe_revised.json` (structured observations of pics_of_clothes_i_like/)
   and `scripts/out_taste_revised.json` (the synthesized taste profile) from earlier rounds.
2. Synthesize plausible Vinted listing metadata (title + brand + size + condition + description)
   for each loved piece directly from its observation — these are stand-ins for "an actual seller
   listed exactly this item." They should all come back 'yes' or 'maybe'.
3. Add hand-crafted obvious-miss listings (mall-brand basics, athleisure, logo gear, formalwear).
   These should all come back 'no'.
4. Run the production prefilter (`OpenAITasteClient.prefilter_candidates_by_title`) with the
   configured batch size and report a per-item table + headline accuracy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from vsniper.core.config import Settings
from vsniper.domain.contracts import TasteProfile
from vsniper.integrations.openai.client import OpenAITasteClient, PrefilterCandidate

ROOT = Path(__file__).resolve().parent.parent

ENV: dict[str, str] = {}
for raw in (ROOT / ".env").read_text().splitlines():
    if "=" in raw and not raw.lstrip().startswith("#"):
        k, v = raw.split("=", 1)
        ENV.setdefault(k.strip(), v.strip())
        os.environ.setdefault(k.strip(), v.strip())


def _good_listings_from_observations() -> list[PrefilterCandidate]:
    """Turn each saved observation into a plausible Vinted listing PrefilterCandidate."""
    obs_path = ROOT / "scripts" / "out_observe_revised.json"
    observations = json.loads(obs_path.read_text())["observations"]

    items: list[PrefilterCandidate] = []
    for index, obs in enumerate(observations):
        garment = obs.get("garment_type", "vintage garment")
        palette = obs.get("color_palette", "")
        pattern = obs.get("prints_or_patterns", "")
        details = obs.get("details_and_hardware", "")
        era = obs.get("era_or_subculture", "vintage")
        fabric = obs.get("fabric_and_texture", "")
        keywords = ", ".join(obs.get("vibe_keywords", [])[:4])
        title_bits = [era.split("/")[0].strip(), palette.split(",")[0].strip(), garment]
        title = " ".join(bit for bit in title_bits if bit)[:70].strip().title()
        description_lines = [
            f"{garment.capitalize()} in {palette}." if palette else garment.capitalize() + ".",
            f"Cut: {obs.get('silhouette_and_cut', '')}." if obs.get("silhouette_and_cut") else "",
            f"Fabric: {fabric}." if fabric else "",
            f"Print: {pattern}." if pattern and pattern.lower() != "solid" else "",
            f"Details: {details}." if details else "",
            f"Vibe: {keywords}." if keywords else "",
            "",
            "Hand-picked from a thrift haul. Smoke-free home. Ask if you need more pics!",
        ]
        items.append(
            PrefilterCandidate(
                candidate_id=f"good-{index:02d}",
                title=title or "Vintage piece",
                brand=("Vintage" if "vintage" in era.lower() or "90s" in era.lower() else "Unbranded"),
                size="M",
                condition="Very good",
                description="\n".join(description_lines),
            )
        )
    return items


def _obvious_miss_listings() -> list[PrefilterCandidate]:
    return [
        PrefilterCandidate(
            candidate_id="miss-01",
            title="Nike Tech Fleece tracksuit set",
            brand="Nike",
            size="M",
            condition="Like new",
            description="Athleisure poly tracksuit. Smooth synthetic fleece. Large Nike swoosh on chest and leg.",
        ),
        PrefilterCandidate(
            candidate_id="miss-02",
            title="Zara fitted office blazer",
            brand="Zara",
            size="38",
            condition="New with tags",
            description="Polished black fitted office blazer with notch lapel. Suitable for corporate workwear.",
        ),
        PrefilterCandidate(
            candidate_id="miss-03",
            title="Shein bodycon mini dress with rhinestones",
            brand="Shein",
            size="S",
            condition="Good",
            description="Shiny stretch bodycon mini dress. Cut-outs at the waist. Rhinestones along the neckline. Clubwear.",
        ),
        PrefilterCandidate(
            candidate_id="miss-04",
            title="Louis Vuitton monogram print T-shirt",
            brand="Louis Vuitton",
            size="M",
            condition="Very good",
            description="Authentic LV. All-over LV monogram print. Logo branding everywhere.",
        ),
        PrefilterCandidate(
            candidate_id="miss-05",
            title="H&M plain white cotton T-shirt",
            brand="H&M",
            size="M",
            condition="Good",
            description="Basic plain white tee. No graphic. Standard fit. Wardrobe staple.",
        ),
        PrefilterCandidate(
            candidate_id="miss-06",
            title="Lululemon Align high-rise leggings",
            brand="Lululemon",
            size="6",
            condition="Like new",
            description="Performance leggings. Buttery soft Nulu fabric. Yoga / gym athleisure.",
        ),
        PrefilterCandidate(
            candidate_id="miss-07",
            title="Mango satin slip cocktail dress",
            brand="Mango",
            size="M",
            condition="Good",
            description="Delicate satin slip dress with thin straps. Cocktail / formal event wear.",
        ),
        PrefilterCandidate(
            candidate_id="miss-08",
            title="Levi's 711 skinny jeans",
            brand="Levi's",
            size="W28 L30",
            condition="Good",
            description="Standard mid-blue wash. Skinny fit, mid rise. Five-pocket. No utility detail.",
        ),
    ]


def main() -> None:
    settings = Settings()
    client = OpenAITasteClient(settings)
    taste_data = json.loads((ROOT / "scripts" / "out_taste_revised.json").read_text())
    taste_profile = TasteProfile(
        summary=taste_data.get("core_aesthetic_summary", ""),
        taste_prompt=taste_data["taste_prompt"],
        core_aesthetic_summary=taste_data.get("core_aesthetic_summary", ""),
        likes=taste_data.get("likes", []),
        dislikes_or_penalties=taste_data.get("dislikes_or_penalties", []),
        instant_alert_examples=taste_data.get("instant_alert_examples", []),
        instant_reject_examples=taste_data.get("instant_reject_examples", []),
        scoring_rubric=taste_data.get("scoring_rubric", {}),
        transparency_labels=taste_data.get("transparency_labels", []),
    )

    goods = _good_listings_from_observations()
    misses = _obvious_miss_listings()
    candidates = goods + misses

    results = client.prefilter_candidates_by_title(taste_profile=taste_profile, candidates=candidates)

    print(f"Batch size in use: {settings.ai_prefilter_batch_size}")
    print(f"Total candidates: {len(candidates)} (goods={len(goods)}, misses={len(misses)})\n")

    print("=== GOOD LISTINGS (should be yes/maybe) ===")
    good_correct = 0
    for item in goods:
        verdict = results[item.candidate_id]
        ok = verdict.verdict in {"yes", "maybe"}
        good_correct += int(ok)
        marker = "✓" if ok else "✗"
        print(f"  {marker} {item.candidate_id}  [{verdict.verdict:5s}]  {item.title}")
        if verdict.reason:
            print(f"        reason: {verdict.reason}")

    print("\n=== MISS LISTINGS (should be no) ===")
    miss_correct = 0
    for item in misses:
        verdict = results[item.candidate_id]
        ok = verdict.verdict == "no"
        miss_correct += int(ok)
        marker = "✓" if ok else "✗"
        print(f"  {marker} {item.candidate_id}  [{verdict.verdict:5s}]  {item.title}")
        if verdict.reason:
            print(f"        reason: {verdict.reason}")

    total = good_correct + miss_correct
    print(
        f"\nAccuracy: {total}/{len(candidates)} "
        f"(goods kept: {good_correct}/{len(goods)}, misses rejected: {miss_correct}/{len(misses)})"
    )

    out_path = ROOT / "scripts" / "out_prefilter_run.json"
    out_path.write_text(
        json.dumps(
            {
                "batch_size": settings.ai_prefilter_batch_size,
                "candidates": [item.model_dump() for item in candidates],
                "verdicts": {cid: r.model_dump() for cid, r in results.items()},
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
