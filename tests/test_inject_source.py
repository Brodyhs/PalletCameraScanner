"""CameraInjectionSource: truth time-base, reconnect truncation, idle-gap
contract, diagonal trajectory exit, config validation, plan caching.

No camera: a scripted in-memory FrameSource stands in for the watchdog-wrapped
live feed. Its ts clock deliberately does NOT equal ``frame_index / fps`` (a
live source's ts keeps advancing through stalls/outages while frame_index does
not), so truth recorded in the wrong time-base fails reconciliation loudly —
exactly the bug these tests pin.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np
import pytest

from palletscan.app import reconcile_truth
from palletscan.config import AppConfig, CameraConfig, SyntheticConfig
from palletscan.sources.base import FrameSource
from palletscan.sources.inject import CameraInjectionSource, _trajectory
from palletscan.types import Frame, MissEvent

W, H, FPS = 320, 240, 30.0
BG = 96
#: Offset between the live ts clock and frame_index/fps — models a source
#: whose clock ran (outage, stall, shared-camera reuse) before this stream.
TS0 = 120.0


class _FakeLiveInner(FrameSource):
    """Deterministic live-camera stand-in with a scriptable discontinuity."""

    def __init__(self, n: int, discontinuity_at: int | None = None) -> None:
        self._n = n
        self._disc = discontinuity_at

    @property
    def source_id(self) -> str:
        return "fake-cam"

    @property
    def nominal_fps(self) -> float:
        return FPS

    @property
    def live(self) -> bool:
        return True

    def frames(self) -> Iterator[Frame]:
        for i in range(self._n):
            yield Frame(
                image=np.full((H, W), BG, np.uint8),
                ts=TS0 + i / FPS,
                frame_index=i,
                source_id="fake-cam",
                discontinuity=(i == self._disc),
            )

    def close(self) -> None:
        pass


def _app_cfg(**cam_kw: object) -> AppConfig:
    cam: dict = dict(id="fake", name="FakeCam", width=W, height=H, fps=FPS)
    cam.update(cam_kw)
    return AppConfig(cameras=[CameraConfig(**cam)])


def _syn(**kw: object) -> SyntheticConfig:
    defaults: dict = dict(
        seed=11,
        num_passes=1,
        speed_mph_range=(10.0, 10.0),
        angle_deg_range=(0.0, 0.0),
        px_per_module_range=(2.0, 2.0),
        contrast_range=(1.0, 1.0),
        noise_sigma_range=(0.0, 0.0),
        occlusion_max_frac=0.0,
        idle_s_range=(0.1, 0.1),
        directions=["right"],
    )
    defaults.update(kw)
    return SyntheticConfig(**defaults)


def _source(
    syn: SyntheticConfig, n_frames: int = 400, disc: int | None = None
) -> CameraInjectionSource:
    return CameraInjectionSource(
        syn, _app_cfg(), exposure_s=0.001, inner=_FakeLiveInner(n_frames, disc)
    )


def _visible(frame: Frame) -> bool:
    return bool((frame.image != BG).any())


def test_never_decoded_pass_reconciles_as_miss_not_unaccounted() -> None:
    """A genuinely missed injected pass must be accounted for by a MissEvent
    ts overlap. Truth recorded in live frame_index space (which diverges from
    ts on a live camera) classified every such pass 'unaccounted'."""
    src = _source(_syn())
    frames = list(src.frames())
    assert len(src.truth) == 1
    vis = [f.ts for f in frames if _visible(f)]
    assert vis, "the injected pass never appeared on any frame"
    miss = MissEvent(
        candidate_id="cand-1",
        source_id="fake-cam",
        start_ts=vis[0],
        end_ts=vis[-1],
        first_frame=0,
        last_frame=0,
        evidence_dir="",
        evidence_frame_count=0,
        event_id="ev-1",
        wall_time_iso="",
    )
    rec = reconcile_truth(src.truth, [miss], src.nominal_fps)
    assert rec.missed == 1
    assert rec.unaccounted == []


def test_reconnect_finalizes_inflight_pass_as_truncated_truth() -> None:
    """A watchdog discontinuity mid-pass must finalize the in-flight pass
    into truth flagged truncated — its frames were already composited and
    delivered, so it must never silently vanish from the accounting."""
    disc = 20
    syn = _syn(speed_mph_range=(1.0, 1.0))  # slow: still mid-pass at frame 20
    src = _source(syn, n_frames=400, disc=disc)
    frames = list(src.frames())
    assert any(f.discontinuity for f in frames)
    assert len(src.truth) == 1
    rec = src.truth[0]
    assert rec.params.get("truncated") is True
    # bounds map back onto the ts axis and end at/before the reconnect frame
    fps = src.nominal_fps
    disc_ts = TS0 + disc / FPS
    assert rec.first_frame / fps >= TS0 - 1.0 / fps
    assert rec.last_frame / fps <= disc_ts + 1.0 / fps


def test_full_idle_gap_separates_back_to_back_passes() -> None:
    """The idle countdown must not tick while max_concurrent blocks
    launching: a pass outliving its idle window used to leave launch_in
    deeply negative, so the next pass launched with ZERO idle gap."""
    idle_s = 0.5  # 15 frames at 30 fps, well under the pass length below
    syn = _syn(
        num_passes=2,
        speed_mph_range=(2.0, 2.0),
        idle_s_range=(idle_s, idle_s),
    )
    src = _source(syn, n_frames=800)
    frames = list(src.frames())
    idle_frames = round(idle_s * FPS)
    visible = [i for i, f in enumerate(frames) if _visible(f)]
    breaks = [k for k in range(1, len(visible)) if visible[k] - visible[k - 1] > 1]
    assert len(breaks) == 1, "expected exactly two contiguous visible passes"
    gap = visible[breaks[0]] - visible[breaks[0] - 1] - 1
    assert gap >= idle_frames - 1, (
        f"only {gap} idle frame(s) between passes; contract is "
        f"~{idle_frames} (idle_frames_before)"
    )


def test_diagonal_trajectory_ends_when_first_axis_exits() -> None:
    """num_frames must be the MIN of the per-axis crossing times: a diagonal
    patch is fully gone once EITHER axis has exited the frame."""
    rng = np.random.default_rng(0)
    step = 10.0
    v = step / math.sqrt(2.0)
    *_start, nf = _trajectory("downright", 1000, 100, 40, 40, step, rng)
    assert nf == math.ceil((100 + 40) / v)  # y exits long before x
    # pure horizontal still gets its full crossing
    *_start_h, nf_h = _trajectory("right", 1000, 100, 40, 40, step, rng)
    assert nf_h == math.ceil((1000 + 40) / step)


def test_camera_config_without_locked_mode_raises_actionable_error() -> None:
    """A camera config that legitimately omits width/height/fps must produce
    an error naming the missing fields, not an opaque TypeError from None
    geometry deep inside rendering."""
    app_cfg = _app_cfg(width=None, height=None, fps=None)
    with pytest.raises(ValueError, match="width/height/fps"):
        CameraInjectionSource(_syn(), app_cfg, inner=_FakeLiveInner(1))


def test_pass_zero_planned_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """_plan renders the full degraded patch; peeking at pass 0's idle gap at
    generator start must reuse the plan, not render-and-discard it."""
    calls: list[int] = []
    orig = CameraInjectionSource._plan

    def counting(self: CameraInjectionSource, i: int):  # noqa: ANN202
        calls.append(i)
        return orig(self, i)

    monkeypatch.setattr(CameraInjectionSource, "_plan", counting)
    src = _source(_syn())
    list(src.frames())
    assert calls.count(0) == 1
