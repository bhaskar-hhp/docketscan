#!/usr/bin/env python3
"""Compare NVIDIA NIM vision models on a docket photo.

Usage:
    NVIDIA_API_KEY=nvapi-... python3 scripts/nim_compare.py IMAGE [--models ...]

Sends the image to each model twice (temperature 0) and reports what it read.
Purpose: decide whether a hosted VLM can replace the local PP-OCRv4 pipeline,
which would let the UI be hosted as plain static files (no Python server).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher

from PIL import Image

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

DEFAULT_MODELS = [
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "google/gemma-4-31b-it",
    "google/gemma-3-12b-it",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
]

PROMPT = """You are a precise OCR engine reading a courier docket (shipping label).

Return ONLY minified JSON, no prose, no markdown:
{"courier":"<company name or null>","date":"<YYYY-MM-DD or null>","tracking_number":"<exact characters or null>"}

Rules:
- Transcribe the tracking number character by character exactly as printed. Never guess or complete a pattern.
- If a digit is genuinely illegible, put "?" in that position.
- The date is the docket/booking date printed on the form, not a printed "since 1985" or terms date."""


def encode(path: str, max_side: int = 1800, quality: int = 88) -> str:
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def call(model: str, b64: str, api_key: str, timeout: int = 120) -> tuple[str, float]:
    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "temperature": 0,
        "max_tokens": 1500,
    }).encode()

    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        return f"HTTP {exc.code}: {detail}", time.time() - t0
    except Exception as exc:                                  # noqa: BLE001
        return f"ERROR: {exc}", time.time() - t0

    try:
        msg = payload["choices"][0]["message"]
        # reasoning models put the answer in `content` and thinking in
        # `reasoning_content`; fall back to the latter if content is empty
        text = msg.get("content") or msg.get("reasoning_content") or ""
        finish = payload["choices"][0].get("finish_reason")
        if finish == "length":
            text += "\n[TRUNCATED: hit max_tokens]"
        return text, time.time() - t0
    except Exception:                                         # noqa: BLE001
        return f"UNEXPECTED: {json.dumps(payload)[:300]}", time.time() - t0


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--expect", default="",
                    help="known-correct tracking number, for scoring")
    args = ap.parse_args()

    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:
        print("NVIDIA_API_KEY is not set", file=sys.stderr)
        return 2

    b64 = encode(args.image)
    print(f"image: {args.image}  ({len(b64)//1024} KB base64)\n")

    for model in args.models:
        print(f"=== {model} ===")
        seen: list[str] = []
        for trial in range(args.trials):
            text, secs = call(model, b64, key)
            got = parse(text)
            tr = (got.get("tracking_number") or "").strip()
            seen.append(tr)
            print(f"  trial {trial+1}  {secs:5.1f}s  courier={got.get('courier')!r} "
                  f"date={got.get('date')!r} tracking={tr!r}")
            if not got:
                print(f"           raw: {text[:200]}")
        if args.expect and seen:
            best = max(SequenceMatcher(None, s, args.expect).ratio() for s in seen)
            exact = any(s == args.expect for s in seen)
            print(f"  -> exact match: {exact} | best similarity: {best:.2f} vs {args.expect}")
        if len(set(seen)) > 1:
            print(f"  -> unstable across trials: {seen}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
