"""Thin wrappers over pyzbar, pylibdmtx, and zxing-cpp.

Normalizes the libraries' result shapes into :class:`RawDecode`, applies
libdmtx's native timeout, and turns dylib-loading failures into actionable
errors instead of bare ImportErrors. ``ZxingDecoder`` (the optional
``decode.engine: zxing`` path) reads both QR and Data Matrix in a single call
and is markedly more robust on blurry / low-contrast / angled codes.
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


def _import_zxingcpp() -> ModuleType:
    try:
        import zxingcpp

        return zxingcpp
    except Exception as exc:
        raise RuntimeError(
            "decode.engine: zxing needs the zxing-cpp package — install it with "
            "`pip install zxing-cpp` (or `pip install -e \".[zxing]\"`). "
            f"Import failed: {exc}"
        ) from exc


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


class ZxingDecoder:
    """QR + Data Matrix via zxing-cpp (the ``decode.engine: zxing`` path).

    One ``read_barcodes`` call covers both symbologies; restricted to QR +
    Data Matrix so it never burns time on 1D/PDF417 false positives. Payloads
    go through :func:`decode_payload` (raw bytes -> str) so the lossless
    UTF-8/Latin-1 contract holds exactly as for the other decoders.
    """

    name = "zxing"

    def __init__(self) -> None:
        self._zxing = _import_zxingcpp()
        self._bf = self._zxing.BarcodeFormat
        self._fmt = {
            Symbology.QR: self._bf.QRCode,
            Symbology.DATAMATRIX: self._bf.DataMatrix,
        }

    def decode(
        self, gray: np.ndarray, symbologies: tuple[Symbology, ...] | None = None
    ) -> list[RawDecode]:
        syms = symbologies or (Symbology.QR, Symbology.DATAMATRIX)
        formats = [self._fmt[s] for s in syms if s in self._fmt]
        if not formats:
            return []
        results = self._zxing.read_barcodes(
            np.ascontiguousarray(gray), formats=formats
        )
        out: list[RawDecode] = []
        for b in results:
            if not b.valid:
                continue
            if b.format == self._bf.QRCode:
                sym = Symbology.QR
            elif b.format == self._bf.DataMatrix:
                sym = Symbology.DATAMATRIX
            else:
                continue
            p = b.position
            xs = (p.top_left.x, p.top_right.x, p.bottom_right.x, p.bottom_left.x)
            ys = (p.top_left.y, p.top_right.y, p.bottom_right.y, p.bottom_left.y)
            x0, y0 = min(xs), min(ys)
            out.append(
                RawDecode(
                    payload=decode_payload(bytes(b.bytes)),
                    symbology=sym,
                    roi=Roi(int(x0), int(y0), int(max(xs) - x0), int(max(ys) - y0)),
                )
            )
        return out
