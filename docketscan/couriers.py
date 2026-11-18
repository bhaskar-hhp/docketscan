"""Courier registry: recognisable names, logo keywords and tracking-number shapes.

`tracking` regexes are used only for *scoring* candidates, never for hard
rejection, so an unknown/new courier format still produces a best guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Courier:
    name: str
    keywords: tuple[str, ...]        # matched against OCR text, spaces removed, upper-cased
    tracking: tuple[str, ...] = ()   # scoring patterns
    example: str = ""


COURIERS: tuple[Courier, ...] = (
    Courier(
        "Shree Maruti",
        ("SHREEMARUTI", "SHREEMARUTICOURIER", "SMCS", "SMCSPE", "MARUTICOURIERS",
         "TRACKYOURSHIPMENTONWWWSHREEMARUTICOM"),
        (r"^\d{14}$", r"^\d{12,16}$"),
        "24208100094364",
    ),
    Courier("DTDC", ("DTDC", "DTDCPL", "DTDCCOURIER"), (r"^[A-Z]\d{8,9}$", r"^\d{9,11}$"), "D12345678"),
    Courier("Blue Dart", ("BLUEDART", "BLUEDARTEXPRESS", "BDT"), (r"^\d{11}$", r"^\d{9,11}$"), "12345678901"),
    Courier("Delhivery", ("DELHIVERY", "DELHIVERYLTD"), (r"^\d{11,14}$",), "1234567890123"),
    Courier("Ekart", ("EKART", "FLIPKARTEKART"), (r"^FMPC\d+$", r"^\d{13,16}$"), "FMPC1234567890"),
    Courier("XpressBees", ("XPRESSBEES", "XPRESSBEE"), (r"^\d{12,15}$",), "123456789012"),
    Courier("Ecom Express", ("ECOMEXPRESS", "ECOM"), (r"^\d{12,15}$",), "123456789012"),
    Courier("Professional Couriers", ("PROFESSIONALCOURIERS", "TPC", "PROFESSIONAL"), (r"^\d{10,13}$",), "1234567890"),
    Courier("TrackOn", ("TRACKON", "TRACKONCOURIER"), (r"^\d{8,11}$",), "12345678"),
    Courier("Gati", ("GATI", "GATIKWE"), (r"^\d{10,13}$",), "1234567890"),
    Courier("Safexpress", ("SAFEXPRESS", "SAFEX"), (r"^\d{10,13}$",), "1234567890"),
    Courier("Shadowfax", ("SHADOWFAX", "SHADOWFAXCOM"), (r"^\d{12,14}$",), "123456789012"),
    Courier("India Post", ("INDIAPOST", "SPEEDPOST", "INDIA POST"), (r"^[A-Z]{2}\d{9}IN$",), "EE123456789IN"),
    Courier("DHL", ("DHL", "DHLECOMMERCE"), (r"^\d{10}$", r"^\d{10,11}$"), "1234567890"),
    Courier("FedEx", ("FEDEX", "FEDERALEXPRESS"), (r"^\d{12}$", r"^\d{12,15}$"), "123456789012"),
    Courier("Aramex", ("ARAMEX",), (r"^\d{8,11}$",), "12345678"),
    Courier("Amazon", ("AMAZON", "AMZN"), (r"^TBA\d+$", r"^\d{12,15}$"), "TBA1234567890"),
    Courier("VRL", ("VRL", "VRLLOGISTICS"), (r"^\d{6,12}$",), "123456"),
    Courier("TCI", ("TCI", "TCIFREIGHT", "TRANSINDIA"), (r"^\d{6,12}$",), "123456"),
    Courier("Rivigo", ("RIVIGO",), (r"^\d{10,14}$",), "1234567890"),
    Courier("Smartr", ("SMARTR", "SMARTRLOGISTICS"), (r"^\d{10,14}$",), "1234567890"),
    Courier("DotEx", ("DOTEX",), (r"^\d{10,14}$",), "1234567890"),
    Courier("Maruti Courier", ("MARUTICOURIER",), (r"^\d{10,14}$"), "1234567890"),
)

TRACKING_LABELS = (
    "TRACKING", "TRACKNO", "TRACK", "DOCKET", "DOCKETNO", "AWB", "AWBNO", "AWBNUMBER",
    "CONSIGNMENT", "CONSIGNMENTNO", "CNNO", "CN", "LRNO", "LR", "WAYBILL", "BOOKING",
    "BILLNO", "TICKETNO", "REFNO", "REFERENCE", "NO",
)

# Words that mean a number nearby is probably NOT the tracking number.
NOISE_LABELS = ("MOB", "MOBILE", "PHONE", "TEL", "GST", "GSTIN", "TIN", "PAN",
                "PIN", "PINCODE", "AMOUNT", "TOTAL", "VALUE", "DATE", "TIME")


def _clean(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _partial_ratio(needle: str, hay: str) -> float:
    """Best difflib ratio of needle against any same-length window of hay."""
    from difflib import SequenceMatcher

    if not needle or not hay:
        return 0.0
    if needle in hay:
        return 1.0
    n = len(needle)
    if len(hay) < n:
        return SequenceMatcher(None, needle, hay).ratio() * 0.9
    best = 0.0
    sm = SequenceMatcher(None, autojunk=False)
    for i in range(len(hay) - n + 1):
        sm.set_seq2(hay[i:i + n])
        best = max(best, sm.ratio())
        if best > 0.99:
            break
    return best


def match_courier(text: str) -> tuple[str | None, float, str | None]:
    """Return (courier_name, confidence 0-1, matched_keyword)."""
    hay = _clean(text)
    best: tuple[float, str | None, str | None] = (0.0, None, None)
    for c in COURIERS:
        for kw in c.keywords:
            score = _partial_ratio(kw, hay)
            if score > best[0]:
                best = (score, c.name, kw)
    if best[0] < 0.80:
        return None, round(best[0], 2), None
    return best[1], round(min(best[0], 0.99), 2), best[2]


def tracking_patterns(name: str | None) -> tuple[str, ...]:
    if not name:
        return ()
    for c in COURIERS:
        if c.name == name:
            return c.tracking
    return ()
