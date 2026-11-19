---
title: DocketScan
emoji: 📦
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# DocketScan

Reads a courier docket photo and extracts **courier name, date and tracking
number** — offline on the server, no API keys, ~5 s per docket.

## Run

```bash
./run.sh            # first run creates .venv and installs deps
# → http://localhost:8000
```

Or with Docker:

```bash
docker build -t docketscan .
docker run -p 7860:7860 -v "$PWD/data:/data" docketscan
# → http://localhost:7860
```

Open on your phone (same network) and use **Take photo** — the browser opens the
rear camera directly.

## How it works

1. **Preprocess** — EXIF-rotate, downscale to ≤2600 px, grayscale, autocontrast,
   unsharp mask, upscale small photos to ~1800 px wide.
2. **OCR** — RapidOCR (PP-OCRv4, ONNX Runtime) on CPU. Models (~16 MB) ship
   inside the wheel, so nothing is downloaded at runtime.
3. **Courier** — keyword/logo matching against `docketscan/couriers.py`, weighted
   towards the header. Shree Maruti, DTDC, Blue Dart, Delhivery, Ekart,
   XpressBees, Ecom, Professional, TrackOn, India Post, DHL, FedEx, Amazon, VRL,
   TCI, …
4. **Tracking number** — candidates scored by label proximity (`TRACKING`,
   `AWB`, `DOCKET`, `CN`…), format match, length and OCR confidence; phone
   numbers, pincodes and amounts are penalised. The winning region is re-OCR'd
   at 3×, 4.5× and 6× zoom and the readings are **voted character by
   character**. Digits that disagree are flagged, and the alternate reading is
   offered as a one-tap suggestion.
5. **Date** — normalised to ISO. Repairs OCR damage where a separator became a
   digit (`21/9124` → `2024-09-21`) by scoring every plausible D/M/Y split,
   using the separator positions that *are* visible to break ties.
6. **Deskew fallback** — if the first read is weak, the page is straightened and
   re-read once; the better result wins.

## Reading the UI

* **Green / amber / red chip** = confidence in that field.
* **Amber underline** on a digit = that digit could not be read consistently.
* **"use 24208100094364"** = the alternate reading; tap to apply.
* Nothing is written to `records.csv` until you press **Confirm & save**, so a
  misread never silently enters your records.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/scan` | multipart `files` → fields + confidences + previews |
| `POST /api/confirm` | save a (possibly corrected) record |
| `GET /api/records` | recent saved records |
| `GET /api/export.csv` | download everything |
| `GET /api/health` | liveness + whether a key is required |

```bash
curl -X POST localhost:8000/api/scan -F files=@docket.jpg
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `PORT` | `7860` (Docker) / `8000` (`run.sh`) | listen port |
| `DOCKETSCAN_DATA` | `./data` | where `records.csv` / `scans.jsonl` live |
| `DOCKETSCAN_KEY` | unset | if set, saving/listing/exporting requires this access key |

**Set `DOCKETSCAN_KEY` before hosting publicly** — otherwise anyone with the URL
can read and add docket numbers. Scanning itself stays open.

On a free Hugging Face Space the disk is wiped on rebuild: mount persistent
storage at `/data`, or export the CSV regularly.

## Measured behaviour

Sample Shree Maruti docket (1130×528, slightly tilted, blurred names):

| | |
|---|---|
| courier | Shree Maruti · 0.99 |
| date | 2024-09-21 · 0.89 (OCR read `21/9124`) |
| tracking | `24208100094304`, digit 13 flagged, suggests `…364` |
| time | ~5 s on 2 vCPU |

Across 7 degradations of the same photo (rotate ±3–7°, 60 % scale, brighter,
darker): courier 7/7, tracking correct or honestly flagged 7/7, no
confidently-wrong dates. A heavily tilted image costs ~16 s because the deskew
fallback runs.

Synthetic dockets of other couriers: DTDC `D12345678`, Blue Dart `12345678901`,
India Post `EE123456789IN` — all read correctly.

## Tuning

* New courier: add one entry to `COURIERS` in `docketscan/couriers.py`. Put the
  most specific tracking shape first — it is preferred during voting.
* Small/blurry photos: raise `max_side` in `ocr.load_image`, or `target_width`
  in `ocr.prepare`.
* Everything confirmed (including corrections) lands in `data/scans.jsonl`, the
  natural source for tuning rules later.

## Notes

* `opencv-python-headless` is deliberate: the normal wheel needs `libGL.so.1`,
  which is absent on this machine and there is no sudo.
* No `gh` CLI here; deploys use plain `git` plus a token.

## Two deployment modes

**Local (default)** — the Python app: PP-OCRv4 on CPU, ~5 s, offline, flags
doubts per digit. Best for real accounting data.

**Public/static (NIM)** — `docs/` + `worker/`: the UI is plain static files on
GitHub Pages, and a Cloudflare Worker holds the API key and calls a NVIDIA NIM
vision model three times in parallel, taking the majority reading. Free to host,
~10–20 s per docket, and the photo is sent to NVIDIA.

```
docs/index.html ──► Cloudflare Worker ──► NVIDIA NIM (vision)
 (GitHub Pages)      (key + CORS + vote)     nemotron-3-nano-omni-30b
```

Deploy the worker:

```bash
cd worker
npx wrangler login
npx wrangler secret put NVIDIA_API_KEY     # nvapi-...
npx wrangler deploy                        # prints https://<name>.<sub>.workers.dev
```

Then put that URL in `docs/config.js`:

```js
window.DOCKETSCAN_API = "https://docketscan-nim.<sub>.workers.dev";
```

and enable Pages: *Settings → Pages → Source: Deploy from a branch → main /docs*.

Saved dockets in static mode live in **browser localStorage** (no server), and
the CSV button exports them client-side.

### NIM results (measured, same docket)

| model | tracking | latency | stable |
|---|---|---|---|
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | `…304` ✓ 5/5 | 8–18 s | yes → **in use** |
| `google/gemma-4-31b-it` | `…304` ✓ 3/3 | 23–33 s, later 59–120 s | degraded (endpoint) |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | `…904` ✗ | 14 s | — |
| `google/gemma-3-12b-it` | endpoint returned empty | 0 s | down |

The worker runs **three samples in parallel** and takes the majority reading, so
one truncated reasoning reply only loses a vote. High confidence requires at
least two samples to agree; disagreeing characters become `uncertain_positions`,
exactly like the local pipeline.

Benchmark harness: `python3 scripts/nim_compare.py IMAGE --expect <number>`.
