"""Field extraction: courier name, docket date and tracking number.

Everything is heuristic + voted, and every field carries a confidence plus
optional per-character uncertainty so the UI can ask a human to confirm only
the parts that are actually doubtful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date

from .couriers import NOISE_LABELS, TRACKING_LABELS, match_courier, tracking_patterns
from .ocr import OcrLine, deskew, reocr_region, run_ocr, vote


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text.upper())


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _has_label(squashed: str, labels) -> str | None:
    for lab in labels:
        if lab in squashed:
            return lab
    return None


def _label_near(lines: list[OcrLine], idx: int, labels) -> float:
    """0..1 confidence that a tracking-style label sits on/next to this line."""
    line = lines[idx]
    if _has_label(_squash(line.text), labels):
        return 1.0
    best = 0.0
    for j, other in enumerate(lines):
        if j == idx:
            continue
        if not _has_label(_squash(other.text), labels):
            continue
        dy = abs(other.cy - line.cy)
        if dy <= 3.2 * line.height:
            # reward vertical closeness, require some horizontal overlap
            overlap = min(line.box[2], other.box[2]) - max(line.box[0], other.box[0])
            if overlap > 0 or dy <= 1.6 * line.height:
                best = max(best, 0.55 + 0.45 * (1 - dy / (3.2 * line.height)))
    return best


# --------------------------------------------------------------------------- #
# courier
# --------------------------------------------------------------------------- #

def detect_courier(lines: list[OcrLine], im_w: int, im_h: int) -> dict:
    """Courier from logo/header text, weighted towards the top of the docket."""
    scored: list[tuple[float, str, float, str]] = []
    for line in lines:
        in_header = line.cy < 0.42 * im_h
        big = line.height > 0.022 * im_h
        weight = 1.0 + (0.45 if in_header else 0.0) + (0.25 if big else 0.0)
        name, score, kw = match_courier(line.text)
        if name:
            scored.append((score * weight, name, score, kw))

    # whole-document fallback (footer URLs, small print)
    whole, whole_score, whole_kw = match_courier(" ".join(l.text for l in lines))
    if whole and not scored:
        return {"value": whole, "confidence": round(min(0.9, whole_score), 2),
                "source": "footer", "keyword": whole_kw}

    if not scored:
        return {"value": None, "confidence": 0.0, "source": None, "keyword": None}

    scored.sort(key=lambda s: s[0], reverse=True)
    _w, name, score, kw = scored[0]
    return {"value": name, "confidence": round(min(0.99, score), 2), "source": "text", "keyword": kw}


# --------------------------------------------------------------------------- #
# tracking number
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9\s\-\.]{5,40}[A-Z0-9]", re.IGNORECASE)


@dataclass
class Candidate:
    text: str
    raw: str
    line_idx: int
    box: tuple[float, float, float, float]
    score: float
    why: list[str]


def _candidates(lines: list[OcrLine], courier: str | None) -> list[Candidate]:
    pats = [re.compile(p) for p in tracking_patterns(courier)]
    out: list[Candidate] = []

    for i, line in enumerate(lines):
        squashed = _squash(line.text)
        # tokenise the *original* line so we keep positions predictable
        for m in _TOKEN_RE.finditer(line.text):
            raw = m.group(0).strip()
            token = re.sub(r"[\s\-\.]", "", raw).upper()
            ndig = sum(ch.isdigit() for ch in token)
            if ndig < 7 or len(token) < 7 or len(token) > 24:
                continue

            why: list[str] = []
            score = 0.0

            lab = _label_near(lines, i, TRACKING_LABELS)
            if lab:
                score += 0.45 * lab
                why.append(f"label:{lab}")

            if pats and any(p.match(token) for p in pats):
                score += 0.30
                why.append("format-match")

            if 10 <= ndig <= 16:
                score += 0.12
            elif 8 <= ndig <= 9:
                score += 0.06

            # mixed letter/digit text is usually a word, not a tracking number
            letter_share = 1 - ndig / len(token)
            if letter_share > 0:
                score -= 0.30 * letter_share

            score += 0.10 * line.conf

            noise = _has_label(squashed, NOISE_LABELS)
            if noise:
                score -= 0.40
                why.append(f"near:{noise}")

            if ndig == 6:  # pincode-sized
                score -= 0.15
            if token.isdigit() and len(token) == 10 and token[0] in "6789" and noise:
                score -= 0.20

            out.append(Candidate(token, raw, i, line.box, round(score, 3), why))

    # dedupe identical tokens, keep best score and the box that scored it
    best: dict[str, Candidate] = {}
    for c in out:
        if c.text not in best or c.score > best[c.text].score:
            best[c.text] = c
    return sorted(best.values(), key=lambda c: c.score, reverse=True)


def extract_tracking(lines: list[OcrLine], im, courier: str | None) -> dict:
    cands = _candidates(lines, courier)
    if not cands:
        return {"value": None, "confidence": 0.0, "uncertain_positions": [],
                "alternatives": [], "box": None}

    top = cands[0]

    # --- multi-zoom re-read of the winning region, then character vote ------
    readings: list[tuple[str, float]] = [(top.text, 0.55)]
    for zoom in (3.0, 4.5, 6.0):
        text, conf = reocr_region(im, top.box, zoom)
        if text:
            readings.append((text, conf))

    pats_for_vote = tracking_patterns(courier) or ()
    voted, agreement, per_char = vote(readings, pats_for_vote)
    if not voted:
        voted, agreement, per_char = top.text, 0.5, [0.5] * len(top.text)

    # Did voting land on something implausible? then trust the raw pass.
    pats = [re.compile(p) for p in tracking_patterns(courier)]
    if pats and not any(p.match(voted) for p in pats):
        if any(p.match(top.text) for p in pats):
            voted, agreement, per_char = top.text, 0.6, [1.0] * len(top.text)

    uncertain = [i for i, a in enumerate(per_char) if a < 1.0]

    # Offer "did you mean ...?" options for readings that differ only in the
    # doubtful positions - the common case is one digit (6 vs 0, 1 vs 7).
    suggestions: list[str] = []
    for txt, _c in readings:
        txt = re.sub(r"[^A-Za-z0-9]", "", txt).upper()
        if len(txt) != len(voted) or txt == voted:
            continue
        diffs = [i for i in range(len(txt)) if txt[i] != voted[i]]
        if diffs and set(diffs) <= set(uncertain) and txt not in suggestions:
            suggestions.append(txt)

    # confidence: score of the winner blended with vote agreement
    conf = min(0.99, 0.55 * min(1.0, max(0.0, top.score)) + 0.45 * agreement)
    if uncertain:
        # a number that is mostly doubtful is not 80% right - it is unread
        ratio = len(uncertain) / max(1, len(voted))
        conf = min(conf, max(0.15, 0.88 - 0.65 * ratio))

    alts = [c.text for c in cands[1:4] if c.text != voted]

    return {
        "value": voted,
        "raw": top.raw,
        "confidence": round(conf, 2),
        "agreement": round(agreement, 2),
        "uncertain_positions": uncertain,
        "suggestions": suggestions,
        "alternatives": alts,
        "box": [round(v, 1) for v in top.box],
        "why": top.why,
        "readings": [r[0] for r in readings],
    }


# --------------------------------------------------------------------------- #
# date
# --------------------------------------------------------------------------- #

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _mk(day: int, month: int, year: int) -> Date | None:
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    if year < 100:
        year = 2000 + year if year <= 40 else 1900 + year
    if not (2000 <= year <= 2035):
        return None
    try:
        return Date(year, month, day)
    except ValueError:
        return None


def _fmt(d: Date) -> str:
    return d.isoformat()


def parse_date_token(text: str) -> tuple[Date | None, float, str]:
    """Parse a date written in *any* of the usual docket ways.

    Handles OCR damage where a separator became a digit (e.g. real
    '21/9/24' read as '21/9124') by falling back to the digit string and
    trying every plausible day/month/year split.
    """
    raw = text.strip()

    # 1) ISO first: 2024-09-21 (otherwise the generic pattern below would
    #    happily match the '24-09-21' tail of it)
    for m in re.finditer(r"(?<!\d)(20\d{2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?!\d)", raw):
        out = _mk(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if out:
            return out, 0.9, m.group(0)

    # 2) clean separated forms: 21/9/24, 21-09-2024, 21.09.24
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})(?!\d)", raw):
        d, mo, y = (int(g) for g in m.groups())
        if d > 31 and mo <= 12:          # maybe mm/dd/yy
            d, mo = mo, d
        out = _mk(d, mo, y)
        if out:
            return out, 0.92, m.group(0)
        # a well-formed but impossible/out-of-range date: do not "repair" it
        return None, 0.0, m.group(0)

    # 3) textual month: 21 SEP 24 / SEP 21 2024
    for m in re.finditer(r"(\d{1,2})\s*([A-Za-z]{3,9})\s*,?\s*(\d{2,4})", raw):
        mon = m.group(2)[:3].upper()
        if mon in _MONTHS:
            out = _mk(int(m.group(1)), _MONTHS[mon], int(m.group(3)))
            if out:
                return out, 0.88, m.group(0)
    for m in re.finditer(r"([A-Za-z]{3,9})\s*(\d{1,2}),?\s*(\d{2,4})", raw):
        mon = m.group(1)[:3].upper()
        if mon in _MONTHS:
            out = _mk(int(m.group(2)), _MONTHS[mon], int(m.group(3)))
            if out:
                return out, 0.86, m.group(0)

    # 4) no usable separators -> digit-string reconstruction.
    #    OCR often turns a '/' into a '1' (e.g. '21/9/24' -> '21/9124').
    #    So: consider the digit string as-is and with each single digit
    #    dropped, score every plausible day/month/year split, and let the
    #    separator positions actually visible in the text break ties.
    raw_digits = _digits(raw)
    if not raw_digits:
        return None, 0.0, raw

    sep_positions: set[int] = set()
    seen = 0
    for ch in raw:
        if ch.isdigit():
            seen += 1
        elif ch in "/-. " and seen:
            sep_positions.add(seen)

    variants: list[tuple[str, float, int | None]] = [(raw_digits, 0.0, None)]
    if 4 <= len(raw_digits) <= 8:
        for i in range(len(raw_digits)):
            # '1' is the classic '/' artifact, so dropping it is cheaper than
            # dropping what is probably a real date digit.
            penalty = 0.12 if raw_digits[i] in "17" else 0.30
            variants.append((raw_digits[:i] + raw_digits[i + 1:], penalty, i))

    # A digit that appears *before* an observed separator is almost certainly a
    # real date digit (pre-printed part), so dropping one there is costly.
    guard = max(sep_positions) if sep_positions else 2

    split_table = {
        4: [(2, 1, 1), (1, 1, 2)],
        5: [(2, 1, 2), (1, 2, 2), (2, 2, 1)],
        6: [(2, 2, 2), (2, 1, 3), (1, 2, 3)],
        7: [(2, 1, 4), (2, 2, 3), (1, 2, 4)],
        8: [(2, 2, 4), (4, 2, 2)],
    }

    best: tuple[float, Date, str] | None = None
    for chunk, penalty, dropped in variants:
        if dropped is not None and dropped < guard:
            penalty += 0.35
        for a, b, c in split_table.get(len(chunk), []):
            d, mo, y = int(chunk[:a]), int(chunk[a:a + b]), int(chunk[a + b:])
            out = _mk(d, mo, y)
            if not out:
                continue
            score = 1.0 - penalty
            if a in sep_positions:
                score += 0.40          # split agrees with a real separator
            if c == 2:
                score += 0.25          # 2-digit year is the docket norm
            elif c == 4:
                score += 0.15
            if best is None or score > best[0]:
                best = (score, out, chunk)

    if best:
        score, out, chunk = best
        note = raw_digits if chunk == raw_digits else f"{raw_digits}->{chunk}"
        return out, round(min(0.85, 0.45 + 0.30 * score), 2), note
    return None, 0.0, raw


def extract_date(lines: list[OcrLine]) -> dict:
    best: tuple[float, dict] | None = None
    for i, line in enumerate(lines):
        d, conf, raw = parse_date_token(line.text)
        if not d:
            continue
        lab = _label_near(lines, i, ("DATE", "DATED", "DT"))
        score = 0.55 * conf + 0.45 * lab
        # dockets are recent; older-than-3-years or future dates are suspect
        today = Date.today()
        if abs((today - d).days) > 3 * 365:
            score *= 0.35
        payload = {"value": _fmt(d), "raw": raw, "confidence": round(min(0.99, score), 2),
                   "label_nearby": bool(lab)}
        if best is None or score > best[0]:
            best = (score, payload)
    if best is None or best[1]["confidence"] < 0.45:
        return {"value": None, "raw": None, "confidence": 0.0, "label_nearby": False}
    return best[1]


# --------------------------------------------------------------------------- #
# amount (bonus, only when clearly present)
# --------------------------------------------------------------------------- #

def extract_amount(lines: list[OcrLine]) -> dict | None:
    for i, line in enumerate(lines):
        m = re.search(r"(\d{1,5})\s*/-", line.text)
        if m:
            return {"value": int(m.group(1)), "confidence": 0.7, "raw": m.group(0)}
    return None


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #

def _estimate_angle(lines: list[OcrLine], im_w: int) -> float:
    """Skew from *wide* text lines only - short lines give noisy angles."""
    import statistics

    wide = [l.angle for l in lines
            if l.conf > 0.6 and (l.box[2] - l.box[0]) > 0.18 * im_w and abs(l.angle) < 30]
    if not wide:
        return 0.0
    return statistics.median(wide)


def _quality(r: dict) -> float:
    """How much do we trust a whole-docket read? 0..1

    Format correctness of the tracking number counts heavily: a 15-digit read
    of a 14-digit docket is worse than *no* read, even if a (wrong) date was
    also picked up.
    """
    t = r["tracking"]
    pats = tracking_patterns(r["courier"]["value"])
    fmt = -0.20                                  # unmatched shape
    if t["value"] and pats:
        for i, p in enumerate(pats):
            if re.match(p, t["value"]):
                fmt = 0.12 if i == 0 else 0.02   # specific shape > loose shape
                break

    q = 0.60 * (t["confidence"] or 0)
    q += 0.25 * (r["courier"]["confidence"] or 0)
    if r["date"]["value"]:
        q += 0.15
    return round(max(0.0, min(1.0, q + fmt)), 3)


def _extract(im, lines: list[OcrLine]) -> dict:
    w, h = im.size
    courier = detect_courier(lines, w, h)
    tracking = extract_tracking(lines, im, courier["value"])
    d = extract_date(lines)
    amount = extract_amount(lines)

    review: list[str] = []
    if tracking["confidence"] < 0.85 or tracking["uncertain_positions"]:
        review.append("tracking")
    if d["confidence"] < 0.75:
        review.append("date")
    if courier["confidence"] < 0.80:
        review.append("courier")

    return {
        "courier": courier,
        "date": d,
        "tracking": tracking,
        "amount": amount,
        "needs_review": review,
        "raw_text": "\n".join(l.text for l in lines),
        "line_count": len(lines),
    }


def scan(im, ocr_lines: list[OcrLine] | None = None) -> dict:
    import time

    t0 = time.time()
    lines = ocr_lines if ocr_lines is not None else run_ocr(im)
    best = _extract(im, lines)
    best_q = _quality(best)
    deskewed = False

    # Photographed dockets are often tilted. Retrying is expensive (another
    # full OCR pass per angle), so only do it when the first read is weak.
    if ocr_lines is None and best_q < 0.80:
        angle = _estimate_angle(lines, im.width)
        if abs(angle) < 0.6:
            from .ocr import estimate_skew

            angle = estimate_skew(im)
        for cand in {round(-angle, 2), round(angle, 2)}:
            if abs(cand) < 0.6 or abs(cand) > 15:
                continue
            rot = deskew(im, cand)
            alt = _extract(rot, run_ocr(rot))
            q = _quality(alt)
            if q >= best_q - 1e-9:          # ties: prefer the straightened copy
                best, best_q, im, lines, deskewed = alt, q, rot, [], True
                best["deskew_angle"] = cand
            if best_q >= 0.92:
                break

    best["deskewed"] = deskewed
    best["quality"] = best_q
    best["ocr_ms"] = int((time.time() - t0) * 1000)
    return best
