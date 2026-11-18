"""Offline courier-docket scanner."""

from .couriers import COURIERS
from .extract import scan
from .ocr import load_image, run_ocr

__all__ = ["COURIERS", "scan", "load_image", "run_ocr"]
__version__ = "1.0.0"
