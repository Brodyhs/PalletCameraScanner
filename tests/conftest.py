"""Shared fixtures. Fails loudly (never skips) if native decoder libs are missing."""

from __future__ import annotations

from pathlib import Path

import pytest

from palletscan.config import AppConfig, SyntheticConfig, apply_overrides


def pytest_configure(config: pytest.Config) -> None:
    """Verify zbar/libdmtx native libraries load before running anything.

    A silent skip here would let the suite go green without exercising the
    decoders — the opposite of the account-for-everything posture.
    """
    problems = []
    try:
        from pyzbar import pyzbar  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment failure path
        problems.append(f"pyzbar/zbar failed to load: {exc}")
    try:
        from palletscan._compat import import_pylibdmtx

        import_pylibdmtx()
    except Exception as exc:  # pragma: no cover - environment failure path
        problems.append(f"pylibdmtx/libdmtx failed to load: {exc}")
    if problems:
        raise pytest.UsageError(
            "Native decoder libraries unavailable:\n  "
            + "\n  ".join(problems)
            + "\nmacOS: brew install zbar libdmtx; if loading still fails: "
            "export DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib"
        )


@pytest.fixture()
def fast_synth_config(tmp_path: Path) -> AppConfig:
    """Small, deterministic synthetic config with outputs under tmp_path."""
    cfg = AppConfig().model_copy(
        update={
            "synthetic": SyntheticConfig(
                width=640,
                height=360,
                fps=30.0,
                seed=1234,
                num_passes=3,
                speed_mph_range=(3.0, 5.0),
                angle_deg_range=(0.0, 10.0),
                contrast_range=(0.8, 1.0),
                noise_sigma_range=(1.0, 3.0),
                occlusion_max_frac=0.0,
                idle_s_range=(0.4, 0.6),
            ),
        }
    )
    return apply_overrides(cfg, data_dir=tmp_path)
