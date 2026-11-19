"""DocketScan - web app for reading courier dockets.

Run:  ./run.sh          (or: .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000)
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from docketscan import load_image, scan

BASE = Path(__file__).parent
DATA = Path(os.environ.get("DOCKETSCAN_DATA", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
RECORDS_CSV = DATA / "records.csv"
RECORDS_JSONL = DATA / "scans.jsonl"

# Optional shared secret. Set DOCKETSCAN_KEY when hosting publicly so that
# saving/listing docket numbers is not open to anyone with the URL.
APP_KEY = os.environ.get("DOCKETSCAN_KEY", "").strip()

CSV_HEADER = ["saved_at", "source_file", "courier", "date", "tracking",
              "tracking_confidence", "needs_review", "corrected", "ocr_ms"]

_OCR_LOCK = threading.Lock()   # ONNX session is not thread-safe
_CSV_LOCK = threading.Lock()

app = FastAPI(title="DocketScan", version="1.0.0")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _thumb(im, max_side: int = 420, quality: int = 72) -> str:
    t = im.copy()
    t.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    t.convert("RGB").save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _crop_preview(im, box, zoom: float = 3.0, quality: int = 80) -> str | None:
    if not box:
        return None
    w, h = im.size
    pad = 10
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    c = im.crop((int(x0), int(y0), int(x1), int(y1)))
    c = c.resize((int(c.width * zoom), int(c.height * zoom)))
    buf = io.BytesIO()
    c.convert("RGB").save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _save_record(row: dict) -> None:
    with _CSV_LOCK:
        new_file = not RECORDS_CSV.exists()
        with RECORDS_CSV.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
            if new_file:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in CSV_HEADER})
        with RECORDS_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "engine": "rapidocr-onnxruntime", "version": app.version,
            "auth_required": bool(APP_KEY)}


def _require_key(key: str | None) -> None:
    if APP_KEY and (key or "").strip() != APP_KEY:
        raise HTTPException(401, "invalid or missing X-Docket-Key")


@app.post("/api/scan")
async def scan_endpoint(files: list[UploadFile] = File(...)) -> JSONResponse:
    if not files:
        raise HTTPException(400, "no files uploaded")

    results = []
    for up in files:
        raw = await up.read()
        if not raw:
            results.append({"filename": up.filename, "ok": False, "error": "empty file"})
            continue
        t0 = time.time()
        try:
            im = load_image(raw)
        except Exception as exc:                     # noqa: BLE001
            results.append({"filename": up.filename, "ok": False,
                            "error": f"cannot read image: {exc}"})
            continue
        try:
            with _OCR_LOCK:
                r = scan(im)
        except Exception as exc:                     # noqa: BLE001
            results.append({"filename": up.filename, "ok": False,
                            "error": f"ocr failed: {exc}"})
            continue

        results.append({
            "filename": up.filename,
            "ok": True,
            "image_size": [im.width, im.height],
            "thumbnail": _thumb(im),
            "tracking_preview": _crop_preview(im, r["tracking"].get("box")),
            **r,
            "elapsed_ms": int((time.time() - t0) * 1000),
        })

    return JSONResponse({"ok": True, "count": len(results), "results": results})


class ConfirmBody(BaseModel):
    source_file: str | None = None
    courier: str | None = None
    date: str | None = None
    tracking: str | None = None
    tracking_confidence: float | None = Field(default=None, ge=0, le=1)
    needs_review: list[str] = Field(default_factory=list)
    corrected: bool = False
    ocr_ms: int | None = None


@app.post("/api/confirm")
def confirm(body: ConfirmBody, x_docket_key: str | None = Header(default=None)) -> dict:
    _require_key(x_docket_key)
    row = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "source_file": body.source_file or "",
        "courier": body.courier or "",
        "date": body.date or "",
        "tracking": body.tracking or "",
        "tracking_confidence": body.tracking_confidence if body.tracking_confidence is not None else "",
        "needs_review": ",".join(body.needs_review),
        "corrected": "yes" if body.corrected else "no",
        "ocr_ms": body.ocr_ms if body.ocr_ms is not None else "",
    }
    _save_record(row)
    return {"ok": True, "record": row}


@app.get("/api/records")
def records(limit: int = 25, x_docket_key: str | None = Header(default=None)) -> dict:
    _require_key(x_docket_key)
    if not RECORDS_CSV.exists():
        return {"ok": True, "records": []}
    with _CSV_LOCK:
        with RECORDS_CSV.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    return {"ok": True, "records": rows[-limit:][::-1]}


@app.delete("/api/records")
def clear_records(x_docket_key: str | None = Header(default=None)) -> dict:
    """Delete every saved docket. Irreversible; the UI confirms first."""
    _require_key(x_docket_key)
    with _CSV_LOCK:
        if RECORDS_CSV.exists():
            RECORDS_CSV.unlink()
    return {"ok": True, "cleared": True}


@app.get("/api/export.csv")
def export_csv(x_docket_key: str | None = Header(default=None)):
    _require_key(x_docket_key)
    if not RECORDS_CSV.exists():
        raise HTTPException(404, "no records yet")
    return FileResponse(RECORDS_CSV, media_type="text/csv", filename="dockets.csv")


# --------------------------------------------------------------------------- #
# static UI
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE / "static" / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
