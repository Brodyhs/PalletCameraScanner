"""Thin wrappers over pyzbar and pylibdmtx.

Normalizes the two libraries' result shapes into :class:`RawDecode`, applies
libdmtx's native timeout, and turns dylib-loading failures into actionable
errors instead of bare ImportErrors.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

import numpy as np

from palletscan._compat import LIB_HELP, import_pylibdmtx
from palletscan.types import Roi, Symbology

#: More than one pallet face can share a motion segment, so never cap at 1;
#: the cap (with the native timeout) still bounds worst-case scan time.
_DM_MAX_COUNT = 4


def _import_pyzbar() -> ModuleType:
    try:
        from pyzbar import pyzbar

        return pyzbar
    except Exception as exc:
        raise RuntimeError(LIB_HELP.format(lib="zbar", pkg="zbar", err=exc)) from exc


@dataclass(frozen=True, slots=True)
class RawDecode:
    """A decode in the coordinate frame of the image that was passed in."""

    payload: str
    symbology: Symbology
    roi: Roi


def decode_payload(data: bytes) -> str:
    """Symbol bytes -> payload string, losslessly.

    Strict UTF-8 first; on failure, Latin-1 — ISO/IEC 16022's *default*
    byte interpretation for Data Matrix, and a total 1:1 mapping, so no
    byte sequence ever becomes U+FFFD. A replacement character here would
    poison the tracker/dedup/manifest keys downstream: the manifest input
    path is strict UTF-8 and a U+FFFD payload can never match it
    (REVIEW_SYSTEM_0c30c77 finding 14).
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


class PyzbarDecoder:
    """QR decoding via zbar (the fast path)."""

    name = "pyzbar"
    symbology = Symbology.QR

    def __init__(self) -> None:
        self._pyzbar = _import_pyzbar()
        self._symbols = [self._pyzbar.ZBarSymbol.QRCODE]

    def decode(self, gray: np.ndarray) -> list[RawDecode]:
        results = self._pyzbar.decode(
            np.ascontiguousarray(gray), symbols=self._symbols
        )
        out = []
        for r in results:
            rect = r.rect
            out.append(
                RawDecode(
                    payload=decode_payload(r.data),
                    symbology=Symbology.QR,
                    roi=Roi(rect.left, rect.top, rect.width, rect.height),
                )
            )
        return out


class PylibdmtxDecoder:
    """Data Matrix decoding via libdmtx (slow — always call with a timeout
    and on ROI crops, never full frames)."""

    name = "pylibdmtx"
    symbology = Symbology.DATAMATRIX

    def __init__(self) -> None:
        self._pylibdmtx = import_pylibdmtx()

    def decode(self, gray: np.ndarray, timeout_ms: int) -> list[RawDecode]:
        results = self._pylibdmtx.decode(
            np.ascontiguousarray(gray),
            timeout=max(1, int(timeout_ms)),
            max_count=_DM_MAX_COUNT,
        )
        h = int(gray.shape[0])
        out = []
        for r in results:
            rect = r.rect
            # libdmtx uses a bottom-left origin; flip to image coordinates.
            out.append(
                RawDecode(
                    payload=decode_payload(r.data),
                    symbology=Symbology.DATAMATRIX,
                    roi=Roi(rect.left, h - rect.top - rect.height, rect.width, rect.height),
                )
            )
        return out
