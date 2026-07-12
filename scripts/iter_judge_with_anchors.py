"""Held-out eval for the judge prompt now that we pass real labeled anchors (with observations).

Splits the user's 9 reference photos into:
- ANCHOR set: 4 pics turned into LabeledExample objects (score 9, verdict=like) with their
  saved structured observations.
- HELD-OUT set: the remaining 5 pics rendered into a 2x2 grid (one position empty).

Then runs three judge-prompt variants against the held-out grid:
  - 'no_anchors'        — control: use the taste prompt + synthetic anchors only
  - 'current_anchors'   — production: anchors rendered as a single comma-joined line
  - 'structured_anchors' — proposed: anchors as multi-field blocks plus an explicit
                            "match the visual signature of these" instruction.

Every held-out item is a known user-liked piece, so the ideal outcome is high & tight scores.
We report avg / min / range per variant.
"""

from __future__ import annotations

import argparse
import base64
import json
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from vsniper.core.config import Settings
from vsniper.domain.contracts import LabeledExample, ReferenceObservation, TasteProfile
from vsniper.integrations.openai.client import (
    CandidateImageInput,
    OpenAITasteClient,
)

ROOT = Path(__file__).resolve().parent.parent
PICS_DIR = ROOT / "pics_of_clothes_i_like"


def _load_settings() -> Settings:
    return Settings()


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


def _load_observations() -> dict[str, ReferenceObservation]:
    raw = json.loads((ROOT / "scripts" / "out_observe_revised.json").read_text())["observations"]
    pics = sorted(p.name for p in PICS_DIR.glob("*.jpg"))
    out: dict[str, ReferenceObservation] = {}
    for index, obs in enumerate(raw):
        if index >= len(pics):
            break
        out[pics[index]] = ReferenceObservation(
            image_id=pics[index],
            file_name=pics[index],
            garment_type=obs.get("garment_type", ""),
            silhouette_and_cut=obs.get("silhouette_and_cut", ""),
            color_palette=obs.get("color_palette", ""),
            fabric_and_texture=obs.get("fabric_and_texture", ""),
            prints_or_patterns=obs.get("prints_or_patterns", ""),
            details_and_hardware=obs.get("details_and_hardware", ""),
            era_or_subculture=obs.get("era_or_subculture", ""),
            vibe_keywords=obs.get("vibe_keywords", []),
        )
    return out


def _try_font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _build_grid_image(paths: list[Path]) -> bytes:
    tile = 512
    gutter = 24
    label_h = 56
    width = tile * 2 + gutter * 3
    height = (tile + label_h) * 2 + gutter * 3
    canvas = Image.new("RGB", (width, height), "white")
    font = _try_font(28)
    positions = ["Top-Left", "Top-Right", "Bottom-Left", "Bottom-Right"]
    for i, p in enumerate(paths[:4]):
        col, row = i % 2, i // 2
        x = gutter + col * (tile + gutter)
        y = gutter + row * (tile + label_h + gutter)
        with Image.open(p) as opened:
            img = ImageOps.exif_transpose(opened).convert("RGB")
            img.thumbnail((tile, tile), Image.Resampling.LANCZOS)
            backdrop = Image.new("RGB", (tile, tile), "white")
            backdrop.paste(img, ((tile - img.width) // 2, (tile - img.height) // 2))
            canvas.paste(backdrop, (x, y + label_h))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([x, y, x + tile, y + label_h], fill=(20, 20, 20))
        draw.text((x + 12, y + 12), positions[i], fill=(255, 255, 255), font=font)
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue()


# ---- Prompt variants ----

STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
JUDGMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "explanation", "labels", "concerns"],
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 100},
        "explanation": {"type": "string"},
        "labels": STRING_ARRAY,
        "concerns": STRING_ARRAY,
    },
}
GRID_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["top_left", "top_right", "bottom_left", "bottom_right"],
    "properties": {
        pos: {"anyOf": [JUDGMENT_SCHEMA, {"type": "null"}]}
        for pos in ("top_left", "top_right", "bottom_left", "bottom_right")
    },
}


def _format_anchor_inline(example: LabeledExample) -> str:
    cues: list[str] = []
    if example.observation:
        for field in (
            example.observation.garment_type,
            example.observation.color_palette,
            example.observation.prints_or_patterns,
            example.observation.era_or_subculture,
        ):
            if field:
                cues.append(field)
    if example.user_comment:
        cues.append(f"user said: {example.user_comment}")
    cue_text = " — " + "; ".join(cues) if cues else ""
    return f"{example.title} (score {example.score_10}){cue_text}"


def _format_anchor_block(example: LabeledExample) -> str:
    """Multi-line, field-by-field anchor format. The judge can pattern-match each visual cue."""
    obs = example.observation
    lines = [f"• {example.title} — verdict={example.verdict}, score {example.score_10}/10"]
    if obs:
        if obs.garment_type:
            lines.append(f"    garment: {obs.garment_type}")
        if obs.silhouette_and_cut:
            lines.append(f"    silhouette: {obs.silhouette_and_cut}")
        if obs.color_palette:
            lines.append(f"    palette: {obs.color_palette}")
        if obs.fabric_and_texture:
            lines.append(f"    fabric: {obs.fabric_and_texture}")
        if obs.prints_or_patterns and obs.prints_or_patterns.lower() != "solid":
            lines.append(f"    pattern: {obs.prints_or_patterns}")
        if obs.details_and_hardware:
            lines.append(f"    details: {obs.details_and_hardware}")
        if obs.era_or_subculture:
            lines.append(f"    era: {obs.era_or_subculture}")
        if obs.vibe_keywords:
            lines.append(f"    vibe: {', '.join(obs.vibe_keywords[:5])}")
    if example.user_comment:
        lines.append(f"    user note: {example.user_comment}")
    return "\n".join(lines)


def build_prompt(
    *,
    variant: str,
    taste_profile: TasteProfile,
    liked_anchors: list[LabeledExample],
    disliked_anchors: list[LabeledExample],
) -> str:
    rubric_lines = (
        "\n".join(f"- {bucket}: {desc}" for bucket, desc in (taste_profile.scoring_rubric or {}).items())
        or "- (no rubric configured — use your own calibrated 1-100 judgement)"
    )
    if variant == "no_anchors":
        alert_lines = (
            "\n".join(f"- {item}" for item in taste_profile.instant_alert_examples[:5]) or "- (none)"
        )
        reject_lines = (
            "\n".join(f"- {item}" for item in taste_profile.instant_reject_examples[:5]) or "- (none)"
        )
        return (
            "Here is a description of MY personal clothing taste — treat this as the only ground truth.\n\n"
            f"<taste>\n{taste_profile.taste_prompt}\n</taste>\n\n"
            "Use this calibrated 1-100 rubric (do not invent your own):\n"
            f"{rubric_lines}\n\n"
            f"Anchors — items I would score 90-100:\n{alert_lines}\n"
            f"Anchors — items I would score 1-10:\n{reject_lines}\n\n"
            "The attached image is a 2x2 grid. Each quadrant contains a separate Vinted candidate, with its "
            "position burned into a dark banner at the top. Ignore floor / lighting / wrinkles.\n"
            "For EACH quadrant that actually contains a garment, return a judgment with:\n"
            "- score (integer 1-100, anchored to the rubric and anchors above)\n"
            "- explanation: one sentence naming the SPECIFIC visual cues that pushed the score up or down\n"
            "- labels: 1-4 short tags from my taste vocabulary that this candidate matches\n"
            "- concerns: 0-3 short tags for anything that should dock points\n"
            "Use null for empty quadrants. Calibrate against the anchors, not within the grid (do not "
            "normalise across the four). Use the full 1-100 range — do not flatten everything to 50-70."
        )

    if variant == "current_anchors":
        alert_lines = "\n".join(f"- {_format_anchor_inline(ex)}" for ex in liked_anchors[:5])
        reject_lines = "\n".join(f"- {_format_anchor_inline(ex)}" for ex in disliked_anchors[:5]) or "- (none yet)"
        return (
            "Here is a description of MY personal clothing taste — treat this as the only ground truth.\n\n"
            f"<taste>\n{taste_profile.taste_prompt}\n</taste>\n\n"
            "Use this calibrated 1-100 rubric (do not invent your own):\n"
            f"{rubric_lines}\n\n"
            f"Anchors — REAL items I scored 90-100 (highest priority calibration):\n{alert_lines}\n"
            f"Anchors — REAL items I scored 1-10 (highest priority calibration):\n{reject_lines}\n\n"
            "The attached image is a 2x2 grid. Each quadrant contains a separate Vinted candidate, with its "
            "position burned into a dark banner at the top. Ignore floor / lighting / wrinkles.\n"
            "For EACH quadrant that actually contains a garment, return a judgment with:\n"
            "- score (integer 1-100, anchored to the rubric and anchors above)\n"
            "- explanation: one sentence naming the SPECIFIC visual cues that pushed the score up or down\n"
            "- labels: 1-4 short tags from my taste vocabulary that this candidate matches\n"
            "- concerns: 0-3 short tags for anything that should dock points\n"
            "Use null for empty quadrants. Calibrate against the anchors, not within the grid (do not "
            "normalise across the four). Use the full 1-100 range — do not flatten everything to 50-70."
        )

    if variant == "structured_anchors":
        alert_blocks = "\n".join(_format_anchor_block(ex) for ex in liked_anchors[:5]) or "(none yet)"
        reject_blocks = "\n".join(_format_anchor_block(ex) for ex in disliked_anchors[:5]) or "(none yet)"
        return (
            "Here is a description of MY personal clothing taste — treat this as the only ground truth.\n\n"
            f"<taste>\n{taste_profile.taste_prompt}\n</taste>\n\n"
            "Use this calibrated 1-100 rubric (do not invent your own):\n"
            f"{rubric_lines}\n\n"
            "CALIBRATION ANCHORS — real past judgements with the SAME structured visual cues you should be "
            "looking for in the candidate image. Treat these as the highest-priority source of truth. If a "
            "candidate's visible features overlap with the liked-anchor signatures, score it in the same band; "
            "if they overlap with the disliked-anchor signatures, dock accordingly.\n\n"
            f"Liked anchors (score 90-100):\n{alert_blocks}\n\n"
            f"Disliked anchors (score 1-10):\n{reject_blocks}\n\n"
            "The attached image is a 2x2 grid. Each quadrant contains a separate Vinted candidate, with its "
            "position burned into a dark banner at the top. Ignore floor / lighting / wrinkles.\n"
            "For EACH quadrant that contains a garment, before answering, MENTALLY check:\n"
            "  1. Which liked-anchor signatures does this candidate share (garment type, palette, pattern, era, "
            "fabric)? Count the matches.\n"
            "  2. Which disliked-anchor signatures does it share? Count those.\n"
            "Then return a judgment with:\n"
            "- score (integer 1-100, anchored to the rubric and the anchor counts above)\n"
            "- explanation: one sentence naming the SPECIFIC visual cues that pushed the score up or down, "
            "ideally referencing which liked- or disliked-anchor signature it most resembles\n"
            "- labels: 1-4 short tags from my taste vocabulary that this candidate matches\n"
            "- concerns: 0-3 short tags for anything that should dock points\n"
            "Use null for empty quadrants. Calibrate against the anchors, not within the grid. Use the full "
            "1-100 range — do not flatten everything to 50-70."
        )

    # structured_soft: structured anchor blocks but NO aggressive count-the-matches instruction.
    alert_blocks = "\n".join(_format_anchor_block(ex) for ex in liked_anchors[:5]) or "(none yet)"
    reject_blocks = "\n".join(_format_anchor_block(ex) for ex in disliked_anchors[:5]) or "(none yet)"
    return (
        "Here is a description of MY personal clothing taste — treat this as the only ground truth.\n\n"
        f"<taste>\n{taste_profile.taste_prompt}\n</taste>\n\n"
        "Use this calibrated 1-100 rubric (do not invent your own):\n"
        f"{rubric_lines}\n\n"
        "Reference anchors — REAL past judgements with their structured visual cues. Treat these as the "
        "single most reliable source of truth, BUT they do not enumerate every kind of item I love. A "
        "candidate that doesn't match a specific liked anchor can still score 90+ if it independently fits the "
        "<taste> paragraph and rubric. Use anchors to calibrate, never to narrow.\n\n"
        f"Liked anchors (each was scored 90-100):\n{alert_blocks}\n\n"
        f"Disliked anchors (each was scored 1-10):\n{reject_blocks}\n\n"
        "The attached image is a 2x2 grid. Each quadrant contains a separate Vinted candidate, with its "
        "position burned into a dark banner at the top. Ignore floor / lighting / wrinkles.\n"
        "For EACH quadrant that contains a garment, return a judgment with:\n"
        "- score (integer 1-100, anchored to the rubric and anchors)\n"
        "- explanation: one sentence naming the SPECIFIC visual cues that pushed the score up or down\n"
        "- labels: 1-4 short tags from my taste vocabulary that this candidate matches\n"
        "- concerns: 0-3 short tags for anything that should dock points\n"
        "Use null for empty quadrants. Calibrate against the anchors, not within the grid (do not "
        "normalise across the four). Use the full 1-100 range — do not flatten everything to 50-70."
    )


def main(variants: list[str], anchor_indices: list[int], held_out_indices: list[int]) -> None:
    settings = _load_settings()
    client = OpenAITasteClient(settings)
    taste = _load_taste()
    observations = _load_observations()

    pics = sorted(PICS_DIR.glob("*.jpg"))
    anchor_paths = [pics[i] for i in anchor_indices]
    held_out_paths = [pics[i] for i in held_out_indices][:4]  # one grid

    liked_anchors: list[LabeledExample] = []
    for p in anchor_paths:
        obs = observations[p.name]
        title_bits = [s for s in [obs.era_or_subculture.split("/")[0].strip(), obs.color_palette.split(",")[0].strip(), obs.garment_type] if s]
        title = " ".join(title_bits).strip().title()[:80] or p.stem
        liked_anchors.append(
            LabeledExample(
                candidate_id=f"anchor-{p.stem}",
                verdict="like",
                score_10=95,
                title=title,
                brand="Vintage",
                user_comment="favourite recent find",
                observation=obs,
            )
        )

    grid_image = _build_grid_image(held_out_paths)
    grid_path = ROOT / "scripts" / "grid_heldout.jpg"
    grid_path.write_bytes(grid_image)

    candidate_inputs = [
        CandidateImageInput(candidate_id=p.stem, image_bytes=p.read_bytes()) for p in held_out_paths
    ]

    print(f"Anchor pics: {[p.stem for p in anchor_paths]}")
    print(f"Held-out pics (in grid): {[p.stem for p in held_out_paths]}\n")

    summary: dict[str, dict[str, float | int | list[int]]] = {}
    for variant in variants:
        prompt = build_prompt(
            variant=variant,
            taste_profile=taste,
            liked_anchors=liked_anchors,
            disliked_anchors=[],
        )
        url = "data:image/jpeg;base64," + base64.b64encode(grid_image).decode()
        payload = {
            "model": settings.ai_judge_model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": url, "detail": settings.ai_judge_image_detail},
                    ],
                }
            ],
            "max_output_tokens": 2200,
            "reasoning": {"effort": settings.ai_judge_reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "grid_judgments",
                    "strict": True,
                    "schema": GRID_SCHEMA,
                }
            },
        }
        r = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=180,
        )
        if not r.is_success:
            print(f"[{variant}] OpenAI {r.status_code}: {r.text[:500]}")
            continue
        data = r.json()
        text = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") in {"output_text", "text"}:
                        text += part.get("text", "")
        parsed = json.loads(text)
        scores: list[int] = []
        print(f"=== {variant} ===")
        for pos in ("top_left", "top_right", "bottom_left", "bottom_right"):
            j = parsed.get(pos)
            if j is None:
                continue
            scores.append(int(j["score"]))
            print(f"  {pos}: {j['score']}/10 — {j['explanation']}")
        if scores:
            avg = sum(scores) / len(scores)
            summary[variant] = {
                "scores": scores,
                "avg": round(avg, 2),
                "min": min(scores),
                "range": max(scores) - min(scores),
            }
            print(f"  -> avg={avg:.2f} min={min(scores)} range={max(scores) - min(scores)}\n")

    print("=== SUMMARY ===")
    for variant, stats in summary.items():
        print(f"  {variant:22s} scores={stats['scores']}  avg={stats['avg']}  min={stats['min']}  range={stats['range']}")

    (ROOT / "scripts" / "out_judge_anchor_variants.json").write_text(
        json.dumps(summary, indent=2)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variants",
        nargs="+",
        default=["no_anchors", "current_anchors", "structured_anchors"],
    )
    parser.add_argument("--anchor-indices", nargs="+", type=int, default=[0, 1, 2, 3])
    parser.add_argument("--held-indices", nargs="+", type=int, default=[4, 5, 6, 7])
    args = parser.parse_args()
    main(args.variants, args.anchor_indices, args.held_indices)
