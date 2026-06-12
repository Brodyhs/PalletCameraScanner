#!/usr/bin/env python3
"""Generate the bundled selftest assets (run once; the PNGs are committed).

Provenance (spec §3, no runtime downloads): the images are produced by the
repo's own pure render functions — ``qrcode`` for the QR symbol and
pylibdmtx's encoder for the Data Matrix — at a generous 8 px/module on a
white quiet-zone background. ``palletscan selftest`` sweeps them across a
synthetic frame and pushes them through the *full* pipeline (motion →
decode → tracker → bus → evidence), so the assets prove the deployed
station can decode, not just that the files exist.

Regenerate only if the payloads or render functions change:

    python tools/make_selftest_assets.py
"""

from __future__ import annotations

from pathlib import Path

import cv2

from palletscan.sources.render import render_datamatrix, render_qr

ASSETS_DIR = Path(__file__).resolve().parents[1] / "palletscan" / "assets"

QR_PAYLOAD = "PALLETSCAN-SELFTEST-QR"
DM_PAYLOAD = "PALLETSCAN-SELFTEST-DM"
PX_PER_MODULE = 8.0


def main() -> int:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    qr = render_qr(QR_PAYLOAD, px_per_module=PX_PER_MODULE)
    dm = render_datamatrix(DM_PAYLOAD, px_per_module=PX_PER_MODULE)
    for name, sym in (("selftest_qr.png", qr), ("selftest_dm.png", dm)):
        path = ASSETS_DIR / name
        if not cv2.imwrite(str(path), sym.image):
            raise SystemExit(f"failed to write {path}")
        print(
            f"wrote {path} ({sym.image.shape[1]}x{sym.image.shape[0]}, "
            f"{sym.modules} modules @ {sym.px_per_module:g} px/module)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
