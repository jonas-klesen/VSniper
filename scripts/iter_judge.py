"""Quickly compare judge-prompt variants by scoring a 4-grid of the user's own reference photos.

Should score ~9-10 for all four, since they are the reference set. If they don't, the judge
prompt or taste_prompt is too generic / mis-calibrated.
"""

from __future__ import annotations

import argparse
import base64
import json
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
PICS_DIR = ROOT / "pics_of_clothes_i_like"

ENV: dict[str, str] = {}
for raw in (ROOT / ".env").read_text().splitlines():
    if "=" in raw and not raw.lstrip().startswith("#"):
        k, v = raw.split("=", 1)
        ENV[k.strip()] = v.strip()
API_KEY = ENV["AI_API_KEY"]
JUDGE_MODEL = ENV.get("AI_JUDGE_MODEL", "gpt-5.4-mini")

OPENAI_URL = "https://api.openai.com/v1/responses"

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
        "top_left": {"anyOf": [JUDGMENT_SCHEMA, {"type": "null"}]},
        "top_right": {"anyOf": [JUDGMENT_SCHEMA, {"type": "null"}]},
        "bottom_left": {"anyOf": [JUDGMENT_SCHEMA, {"type": "null"}]},
        "bottom_right": {"anyOf": [JUDGMENT_SCHEMA, {"type": "null"}]},
    },
}


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


def build_grid(paths: list[Path], titles: list[str]) -> bytes:
    tile = 512
    gutter = 24
    label_h = 56
    width = tile * 2 + gutter * 3
    height = (tile + label_h) * 2 + gutter * 3
    canvas = Image.new("RGB", (width, height), "white")
    font = _try_font(28)
    positions = ["Top-Left", "Top-Right", "Bottom-Left", "Bottom-Right"]
    for i, (p, title) in enumerate(zip(paths, titles)):
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
        draw.text((x + 12, y + 12), f"{positions[i]}  —  {title[:48]}", fill=(255, 255, 255), font=font)
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue()


def call(payload: dict) -> dict:
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
    return {"text": text, "usage": data.get("usage", {}), "parsed": json.loads(text)}


def build_payload(prompt_text: str, image_bytes: bytes) -> dict:
    url = f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"
    return {
        "model": JUDGE_MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt_text},
                    {"type": "input_image", "image_url": url, "detail": "low"},
                ],
            }
        ],
        "max_output_tokens": 2000,
        "reasoning": {"effort": "low"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "grid_judgments",
                "strict": True,
                "schema": GRID_SCHEMA,
            }
        },
    }


CURRENT_PROMPT_TEMPLATE = (
    "Here is a description of my taste: {taste}\n"
    "Please look at the image, which contains a 4-item grid of clothes. Separately evaluate only the visible "
    "quadrants that contain clothes. Give each a score from 1-100 with a one-sentence explanation of how it fits "
    "my taste. Return JSON with keys top_left, top_right, bottom_left, bottom_right. Each present value must have "
    "score, explanation, labels, and concerns. Use null for empty quadrants."
)


REVISED_PROMPT_TEMPLATE = (
    "Here is a description of MY personal clothing taste — treat this as the only ground truth.\n\n"
    "<taste>\n{taste}\n</taste>\n\n"
    "Use this calibrated 1-100 rubric (do not invent your own):\n"
    "{rubric}\n\n"
    "Anchors — items I would score 90-100:\n{alerts}\n"
    "Anchors — items I would score 1-10:\n{rejects}\n\n"
    "The attached image is a 2x2 grid. Each quadrant contains a separate Vinted candidate, with its position and "
    "listing title burned into a dark banner at the top. Ignore the floor / lighting / wrinkles.\n"
    "For EACH quadrant that contains a garment, return a judgment with:\n"
    "- score (1-100, integer, anchored to the rubric above)\n"
    "- explanation: one sentence that names the SPECIFIC visual cues that pushed the score up or down\n"
    "- labels: 1-4 short tags drawn from my taste vocabulary that this candidate matches\n"
    "- concerns: 0-3 short tags for anything that should dock points or warrants caution\n\n"
    "Use null for empty quadrants. Be honest — if something looks fast-fashion or off-aesthetic, score it low even if "
    "it has one nice trait. Calibrate against the anchors, not within the grid (do not normalise scores across the four)."
)


def render_taste_block(taste: dict) -> tuple[str, str, str, str]:
    rubric_lines = "\n".join(f"- {bucket}: {desc}" for bucket, desc in taste["scoring_rubric"].items())
    alerts = "\n".join(f"- {item}" for item in taste.get("instant_alert_examples", [])[:5])
    rejects = "\n".join(f"- {item}" for item in taste.get("instant_reject_examples", [])[:5])
    return taste["taste_prompt"], rubric_lines, alerts, rejects


def main(variant: str, batch: int) -> None:
    all_pics = sorted(PICS_DIR.glob("*.jpg"))
    pics = all_pics[batch * 4 : batch * 4 + 4]
    titles = [p.stem for p in pics]
    image = build_grid(pics, titles)
    (ROOT / "scripts" / f"grid_{variant}_b{batch}.jpg").write_bytes(image)

    if variant == "current":
        taste = json.loads((ROOT / "scripts" / "out_taste_current.json").read_text())
        prompt = CURRENT_PROMPT_TEMPLATE.format(taste=taste["taste_prompt"])
    else:
        taste = json.loads((ROOT / "scripts" / "out_taste_revised.json").read_text())
        t, rubric, alerts, rejects = render_taste_block(taste)
        prompt = REVISED_PROMPT_TEMPLATE.format(taste=t, rubric=rubric, alerts=alerts, rejects=rejects)

    res = call(build_payload(prompt, image))
    out_path = ROOT / "scripts" / f"out_judge_{variant}_b{batch}.json"
    out_path.write_text(json.dumps(res["parsed"], indent=2, ensure_ascii=False))
    print(f"wrote {out_path}")
    print(f"usage: {res['usage']}")
    for pos in ("top_left", "top_right", "bottom_left", "bottom_right"):
        j = res["parsed"].get(pos)
        if j:
            print(f"{pos}: {j['score']}/10 — {j['explanation']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--variant", choices=["current", "revised"], required=True)
    p.add_argument("--batch", type=int, default=0, help="0=pics 1-4, 1=pics 5-8")
    args = p.parse_args()
    main(args.variant, args.batch)
