"""Live-API experimentation harness for iterating taste-model prompts.

Not used at runtime — invoke directly with `uv run --project backend python scripts/iter_prompts.py <stage>`.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PICS_DIR = ROOT / "pics_of_clothes_i_like"

# Load env file manually so we don't need the FastAPI Settings stack here.
ENV: dict[str, str] = {}
for raw in (ROOT / ".env").read_text().splitlines():
    if "=" in raw and not raw.lstrip().startswith("#"):
        k, v = raw.split("=", 1)
        ENV[k.strip()] = v.strip()
API_KEY = ENV["AI_API_KEY"]
JUDGE_MODEL = ENV.get("AI_JUDGE_MODEL", "gpt-5.4-mini")
LEARN_MODEL = ENV.get("AI_LEARN_MODEL", "gpt-5.5")

OPENAI_URL = "https://api.openai.com/v1/responses"
OBSERVE_MAX_OUTPUT_TOKENS = 12000
TASTE_MAX_OUTPUT_TOKENS = 16000


def image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def call(
    *,
    model: str,
    content: list[dict],
    schema_name: str,
    schema: dict,
    effort: str = "medium",
    max_output_tokens: int = 6000,
) -> dict:
    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
    }
    r = httpx.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if not r.is_success:
        raise SystemExit(f"OpenAI {r.status_code}: {r.text[:1000]}")
    data = r.json()
    text = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") in {"output_text", "text"}:
                    text += part.get("text", "")
    usage = data.get("usage", {})
    return {"text": text, "usage": usage, "parsed": json.loads(text) if text else None}


STRING_ARRAY = {"type": "array", "items": {"type": "string"}}


# ===== Variant A: current production prompts (verbatim from openai/client.py) =====

CURRENT_OBSERVATIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observations"],
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["image", "visible_items", "liked_signals", "possible_dislikes_or_uncertainties"],
                "properties": {
                    "image": {"type": "string"},
                    "visible_items": {"type": "string"},
                    "liked_signals": STRING_ARRAY,
                    "possible_dislikes_or_uncertainties": STRING_ARRAY,
                },
            },
        }
    },
}

CURRENT_OBSERVE_PROMPT = (
    "You are helping build a transparent clothing taste model. For each attached image, describe "
    "the garment and infer concrete liked taste signals. Return JSON array only. Each item must have "
    "image, visible_items, liked_signals, possible_dislikes_or_uncertainties. Keep fields concise."
)

CURRENT_TASTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["taste_prompt", "likes", "dislikes_or_penalties", "scoring_rubric", "transparency_labels"],
    "properties": {
        "taste_prompt": {"type": "string"},
        "likes": STRING_ARRAY,
        "dislikes_or_penalties": STRING_ARRAY,
        "scoring_rubric": {
            "type": "object",
            "additionalProperties": False,
            "required": ["1-2", "3-4", "5-6", "7-8", "9-10"],
            "properties": {
                "1-2": {"type": "string"},
                "3-4": {"type": "string"},
                "5-6": {"type": "string"},
                "7-8": {"type": "string"},
                "9-10": {"type": "string"},
            },
        },
        "transparency_labels": STRING_ARRAY,
    },
}


def current_build_taste_prompt(evidence: dict) -> str:
    return (
        "You are designing a persistent taste profile prompt for a Vinted clothing recommender. "
        "Given the evidence below, produce compact JSON with keys: taste_prompt, likes, "
        "dislikes_or_penalties, scoring_rubric, transparency_labels. Avoid overfitting to exact items. "
        "The taste_prompt will later be used to judge unseen clothing images without resending references. "
        f"Evidence JSON: {json.dumps(evidence, ensure_ascii=False)}"
    )


# ===== Variant B: revised prompts (iteration) =====

REVISED_OBSERVATIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observations"],
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "image",
                    "garment_type",
                    "silhouette_and_cut",
                    "color_palette",
                    "fabric_and_texture",
                    "prints_or_patterns",
                    "details_and_hardware",
                    "era_or_subculture",
                    "vibe_keywords",
                    "things_to_avoid_if_oversampled",
                ],
                "properties": {
                    "image": {"type": "string"},
                    "garment_type": {"type": "string"},
                    "silhouette_and_cut": {"type": "string"},
                    "color_palette": {"type": "string"},
                    "fabric_and_texture": {"type": "string"},
                    "prints_or_patterns": {"type": "string"},
                    "details_and_hardware": {"type": "string"},
                    "era_or_subculture": {"type": "string"},
                    "vibe_keywords": STRING_ARRAY,
                    "things_to_avoid_if_oversampled": STRING_ARRAY,
                },
            },
        }
    },
}

REVISED_OBSERVE_PROMPT = (
    "You are a fashion-savvy analyst building a personal taste model for a user from photos of clothes they own and like. "
    "These are flat-lay photos taken on the floor. Ignore floor, lighting and wrinkles.\n\n"
    "For EACH attached image, fill the structured fields. Be concrete and concise (each text field <= 25 words, "
    "no marketing fluff). Cover these dimensions:\n"
    "- garment_type: what it is (e.g. 'cargo trousers', 'baja pullover hoodie').\n"
    "- silhouette_and_cut: fit, rise, leg shape, length, volume (e.g. 'loose straight-leg, low-mid rise, full length').\n"
    "- color_palette: 1-3 dominant colors + saturation/finish (e.g. 'saturated tomato red, washed').\n"
    "- fabric_and_texture: material guess + hand-feel cues (e.g. 'mid-weight cotton twill, slightly faded').\n"
    "- prints_or_patterns: motifs/scale/composition or 'solid'.\n"
    "- details_and_hardware: pockets, zips, drawstrings, contrast stitching, branding visibility, etc.\n"
    "- era_or_subculture: closest aesthetic reference (e.g. 'late-90s/Y2K skate', '70s ethnic/boho', 'Mexican baja / surfwear').\n"
    "- vibe_keywords: 3-6 short tags that another stylist would search by.\n"
    "- things_to_avoid_if_oversampled: traits that this single piece has but which would feel 'too samey' if every recommendation looked exactly like this.\n\n"
    "Do NOT invent details you cannot see. Return ONE entry per attached image, in attachment order."
)


REVISED_TASTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "taste_prompt",
        "core_aesthetic_summary",
        "likes",
        "dislikes_or_penalties",
        "instant_alert_examples",
        "instant_reject_examples",
        "scoring_rubric",
        "transparency_labels",
    ],
    "properties": {
        "taste_prompt": {"type": "string"},
        "core_aesthetic_summary": {"type": "string"},
        "likes": STRING_ARRAY,
        "dislikes_or_penalties": STRING_ARRAY,
        "instant_alert_examples": STRING_ARRAY,
        "instant_reject_examples": STRING_ARRAY,
        "scoring_rubric": {
            "type": "object",
            "additionalProperties": False,
            "required": ["1-2", "3-4", "5-6", "7-8", "9-10"],
            "properties": {
                "1-2": {"type": "string"},
                "3-4": {"type": "string"},
                "5-6": {"type": "string"},
                "7-8": {"type": "string"},
                "9-10": {"type": "string"},
            },
        },
        "transparency_labels": STRING_ARRAY,
    },
}


def revised_build_taste_prompt(evidence: dict) -> str:
    return (
        "You are designing the single source-of-truth TASTE PROFILE that a smaller, cheaper vision model will later use to "
        "score unseen Vinted listings 1-100 — WITHOUT seeing the reference photos again. Your output therefore has to be "
        "fully self-contained, calibrated, and free of references to specific reference images.\n\n"
        "Hard rules:\n"
        "1. Generalise to underlying aesthetic codes (silhouettes, palettes, eras, subcultures, fabrics). Do NOT lock in exact "
        "items like 'red cargo pants'; instead capture what makes them appealing ('saturated primary-color workwear trousers "
        "with utility pockets').\n"
        "2. The judge model will receive ONLY `taste_prompt` plus a candidate image. Make `taste_prompt` a tight, evocative "
        "paragraph (150-300 words) written in the second person ('You like ...'). It MUST mention: dominant aesthetics, "
        "preferred silhouettes, palette ranges, fabric/era cues, AND explicit penalties (what to dock points for).\n"
        "3. `instant_alert_examples` and `instant_reject_examples`: 3-5 short hypothetical garment descriptions each, the kind "
        "of one-line listing title that should auto-score 9-10 vs 1-2.\n"
        "4. `scoring_rubric`: each bucket is one sentence describing what such a candidate looks like. Anchor with concrete "
        "vocabulary the judge can recognise from an image alone.\n"
        "5. `likes` / `dislikes_or_penalties`: 5-12 bullets each, atomic and image-recognisable ("
        "'visible cargo pockets', 'fast-fashion logo prints'). Avoid abstract feelings.\n"
        "6. `transparency_labels`: 6-12 short user-facing chips summarising the taste.\n"
        "7. Stay descriptive, not prescriptive — the user is eclectic, never close the door on adjacent subcultures unless the "
        "evidence is explicit.\n\n"
        f"Evidence JSON (per-image observations + free-form notes + thumbs-up/down history):\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
    )


# ===== Drivers =====


def stage_observe(variant: str) -> None:
    pics = sorted(PICS_DIR.glob("*.jpg"))
    content: list[dict] = []
    if variant == "current":
        content.append({"type": "input_text", "text": CURRENT_OBSERVE_PROMPT})
        schema = CURRENT_OBSERVATIONS_SCHEMA
    else:
        content.append({"type": "input_text", "text": REVISED_OBSERVE_PROMPT})
        schema = REVISED_OBSERVATIONS_SCHEMA
    for p in pics:
        content.append({"type": "input_text", "text": f"Image id: {p.stem}; filename: {p.name}"})
        content.append({"type": "input_image", "image_url": image_data_url(p), "detail": "low"})

    res = call(
        model=LEARN_MODEL,
        content=content,
        schema_name="reference_observations",
        schema=schema,
        effort="medium",
        max_output_tokens=OBSERVE_MAX_OUTPUT_TOKENS,
    )
    out_path = ROOT / "scripts" / f"out_observe_{variant}.json"
    out_path.write_text(json.dumps(res["parsed"], indent=2, ensure_ascii=False))
    print(f"wrote {out_path}")
    print(f"usage: {res['usage']}")


def stage_taste(variant: str) -> None:
    observations_path = ROOT / "scripts" / f"out_observe_{variant}.json"
    if not observations_path.exists():
        raise SystemExit(f"missing {observations_path} — run observe stage first")
    obs = json.loads(observations_path.read_text())["observations"]
    evidence = {
        "reference_observations": obs,
        "notes": [],
        "liked_feedback": [],
        "disliked_feedback": [],
        "previous_taste_prompt": None,
    }
    if variant == "current":
        prompt = current_build_taste_prompt(evidence)
        schema = CURRENT_TASTE_SCHEMA
    else:
        prompt = revised_build_taste_prompt(evidence)
        schema = REVISED_TASTE_SCHEMA
    res = call(
        model=LEARN_MODEL,
        content=[{"type": "input_text", "text": prompt}],
        schema_name="taste_profile",
        schema=schema,
        effort="medium",
        max_output_tokens=TASTE_MAX_OUTPUT_TOKENS,
    )
    out_path = ROOT / "scripts" / f"out_taste_{variant}.json"
    out_path.write_text(json.dumps(res["parsed"], indent=2, ensure_ascii=False))
    print(f"wrote {out_path}")
    print(f"usage: {res['usage']}")
    print("---- taste_prompt ----")
    print(res["parsed"].get("taste_prompt", ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["observe", "taste"])
    parser.add_argument("--variant", choices=["current", "revised"], default="current")
    args = parser.parse_args()
    if args.stage == "observe":
        stage_observe(args.variant)
    else:
        stage_taste(args.variant)
