"""Render functions: decodability, ratio math, determinism."""

from __future__ import annotations

import numpy as np
import pytest

from palletscan.pipeline.decoders import PylibdmtxDecoder, PyzbarDecoder
from palletscan.sources.render import (
    add_noise,
    apply_contrast,
    lighting_gradient,
    motion_blur,
    occlude,
    perspective_warp,
    render_datamatrix,
    render_qr,
)

PAYLOAD = "PLT-000123"


def test_clean_qr_decodes_at_ecc_q() -> None:
    sym = render_qr(PAYLOAD, 4.0)
    results = PyzbarDecoder().decode(sym.image)
    assert [r.payload for r in results] == [PAYLOAD]
    # ECC Q on this payload: version 1 QR is 21 modules
    assert sym.modules == 21


def test_clean_datamatrix_decodes() -> None:
    sym = render_datamatrix(PAYLOAD, 4.0)
    results = PylibdmtxDecoder().decode(sym.image, timeout_ms=500)
    assert [r.payload for r in results] == [PAYLOAD]


@pytest.mark.parametrize("requested", [3.0, 4.5, 6.0])
def test_px_per_module_matches_requested(requested: float) -> None:
    for sym in (render_qr(PAYLOAD, requested), render_datamatrix(PAYLOAD, requested)):
        assert sym.px_per_module == pytest.approx(requested, rel=0.05)
        expected_side = round(sym.modules * requested)
        quiet = (sym.image.shape[0] - expected_side) // 2
        assert quiet > 0, "quiet zone must surround the symbol"


def test_motion_blur_kernel_length_equals_displacement() -> None:
    """The blur kernel spreads an impulse over exactly the displacement px."""
    img = np.zeros((9, 64), np.uint8)
    img[:, 32] = 255
    length = 6
    out = motion_blur(img, float(length))
    spread = np.count_nonzero(out[4, :] > 10)
    assert spread == length


def test_motion_blur_short_lengths_noop() -> None:
    img = np.random.default_rng(0).integers(0, 255, (20, 20), np.uint8)
    assert np.array_equal(motion_blur(img, 1.4), img)


def test_warp_at_35_degrees_keeps_symbols_decodable() -> None:
    qr = render_qr(PAYLOAD, 4.0)
    warped, mask = perspective_warp(qr.image, 35.0, background=255)
    assert [r.payload for r in PyzbarDecoder().decode(warped)] == [PAYLOAD]
    dm = render_datamatrix(PAYLOAD, 4.0)
    warped_dm, _ = perspective_warp(dm.image, 35.0, background=255)
    assert [
        r.payload for r in PylibdmtxDecoder().decode(warped_dm, timeout_ms=500)
    ] == [PAYLOAD]
    # mask covers the foreshortened quad, less than the full rectangle
    assert 0 < np.count_nonzero(mask) < mask.size


def test_blur_at_design_margin_operating_point_decodes() -> None:
    """10 mph at ~1 ms exposure -> <=0.9 module of blur; must stay readable
    (possibly only via preprocessing, here plain at the small-ppm end)."""
    sym = render_qr(PAYLOAD, 3.0)
    blurred = motion_blur(sym.image, 0.9 * 3.0)
    assert [r.payload for r in PyzbarDecoder().decode(blurred)] == [PAYLOAD]


def test_degradations_preserve_shape_and_dtype() -> None:
    rng = np.random.default_rng(7)
    img = render_qr(PAYLOAD, 4.0).image
    for out in (
        apply_contrast(img, 0.5),
        lighting_gradient(img, 25.0, 30.0),
        add_noise(img, 5.0, rng),
        occlude(img, 0.1, rng),
    ):
        assert out.shape == img.shape
        assert out.dtype == np.uint8


def test_noise_and_occlusion_deterministic_per_seed() -> None:
    img = render_qr(PAYLOAD, 4.0).image
    a = add_noise(img, 5.0, np.random.default_rng(42))
    b = add_noise(img, 5.0, np.random.default_rng(42))
    c = add_noise(img, 5.0, np.random.default_rng(43))
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
    oa = occlude(img, 0.12, np.random.default_rng(42))
    ob = occlude(img, 0.12, np.random.default_rng(42))
    assert np.array_equal(oa, ob)


def test_occlusion_covers_requested_fraction() -> None:
    img = np.full((100, 200), 255, np.uint8)
    out = occlude(img, 0.15, np.random.default_rng(0), value=0)
    covered = np.count_nonzero(out == 0) / out.size
    assert covered == pytest.approx(0.15, abs=0.01)
