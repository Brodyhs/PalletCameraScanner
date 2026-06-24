"""Camera/watchdog config models, the source.camera selector, YAML upsert."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from palletscan.config import (
    AppConfig,
    Backend,
    CameraConfig,
    CameraIdentity,
    CameraSettings,
    load_config,
    resolve_camera,
    upsert_camera_yaml,
)


def _cam(id: str = "cam-color", name: str = "See3CAM_24CUG", **kw) -> CameraConfig:
    return CameraConfig(id=id, name=name, **kw)


# -- models -----------------------------------------------------------------


def test_defaults_are_additive() -> None:
    cfg = AppConfig()
    assert cfg.cameras == []
    assert cfg.source.camera is None
    assert cfg.watchdog.stall_timeout_s == 2.0
    assert cfg.watchdog.retry.base_s == 0.5
    assert cfg.watchdog.retry.cap_s == 15.0
    assert cfg.watchdog.max_outage_s is None
    assert cfg.watchdog.max_zombie_readers == 3


def test_full_camera_yaml_parses(tmp_path: Path) -> None:
    p = tmp_path / "cams.yaml"
    p.write_text(
        """
source: {type: camera, camera: cam-mono}
cameras:
  - id: cam-color
    name: "See3CAM_24CUG"
    backend: dshow
    fourcc: UYVY
    width: 1920
    height: 1200
    fps: 120.0
    settings: {exposure_auto: false, exposure: -6, gain: 10}
  - id: cam-mono
    name: "See3CAM_37CUGM"
    fourcc: GREY
    fps: 72.0
    convert_rgb: false
watchdog:
  stall_timeout_s: 1.5
  retry: {base_s: 0.25, cap_s: 8.0}
  max_outage_s: 600.0
  max_zombie_readers: 2
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.source.type == "camera"
    color, mono = cfg.cameras
    assert color.backend is Backend.DSHOW
    assert color.settings.exposure_auto is False
    assert color.settings.exposure == -6
    assert color.settings.brightness is None  # untouched control
    assert mono.backend is Backend.AUTO
    assert mono.convert_rgb is False
    assert mono.width is None  # leave device default
    assert cfg.watchdog.max_outage_s == 600.0
    assert resolve_camera(cfg).id == "cam-mono"


def test_duplicate_camera_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        AppConfig(cameras=[_cam(), _cam(name="See3CAM_37CUGM")])


@pytest.mark.parametrize(
    "field, value",
    [
        ("fourcc", "TOOLONG"),
        ("fourcc", "AB"),
        ("fps", 0.0),
        ("fps", float("nan")),
        ("width", 0),
        ("height", -1),
        ("read_fail_limit", 0),
        ("connect_verify_s", -1.0),
        ("fallback_index", -1),
        ("name", "  "),
    ],
)
def test_camera_field_validators(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _cam(**{field: value})


def test_camera_identity_default_is_dormant_warn() -> None:
    # The identity guard is additive and dormant by default: a plain camera
    # gets policy='warn' with no pinned fingerprint (today's behavior).
    cam = _cam()
    assert cam.identity.policy == "warn"
    assert cam.identity.expected_vid_pid is None
    assert cam.identity.expected_device_path is None


def test_camera_identity_vid_pid_normalized_and_validated() -> None:
    ident = CameraIdentity(expected_vid_pid="2560:C128")
    assert ident.expected_vid_pid == "2560:c128"  # lowercased
    assert CameraIdentity(expected_vid_pid=None).expected_vid_pid is None
    for bad in ["2560-c128", "256:c128", "zzzz:c128", "2560c128", "2560:c12"]:
        with pytest.raises(ValidationError):
            CameraIdentity(expected_vid_pid=bad)


def test_camera_identity_strict_policy_parses_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "cams.yaml"
    p.write_text(
        """
cameras:
  - id: cam-color
    name: "See3CAM_24CUG"
    backend: msmf
    fallback_index: 0
    identity:
      policy: strict
      expected_vid_pid: "2560:c128"
      expected_device_path: "usb#vid_2560&pid_c128&mi_00#x"
""",
        encoding="utf-8",
    )
    cam = load_config(p).cameras[0]
    assert cam.identity.policy == "strict"
    assert cam.identity.expected_vid_pid == "2560:c128"
    assert cam.identity.expected_device_path == "usb#vid_2560&pid_c128&mi_00#x"


def test_camera_identity_rejects_unknown_policy() -> None:
    with pytest.raises(ValidationError):
        CameraIdentity(policy="paranoid")


def test_watchdog_validators() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"watchdog": {"stall_timeout_s": 0.0}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"watchdog": {"max_zombie_readers": 0}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"watchdog": {"max_outage_s": -5.0}})


# -- selector ----------------------------------------------------------------


def test_resolve_camera_requires_an_entry() -> None:
    with pytest.raises(ValueError, match="at least one"):
        resolve_camera(AppConfig())


def test_resolve_camera_single_entry_is_default() -> None:
    cfg = AppConfig(cameras=[_cam()])
    assert resolve_camera(cfg) is cfg.cameras[0]


def test_resolve_camera_multiple_entries_require_selector() -> None:
    cams = [_cam(), _cam(id="cam-mono", name="See3CAM_37CUGM")]
    cfg = AppConfig(cameras=cams)
    with pytest.raises(ValueError, match="cam-color.*cam-mono"):
        resolve_camera(cfg)
    chosen = resolve_camera(
        AppConfig.model_validate(
            {
                "source": {"camera": "cam-mono"},
                "cameras": [c.model_dump() for c in cams],
            }
        )
    )
    assert chosen.id == "cam-mono"


def test_resolve_camera_unknown_id_lists_configured() -> None:
    cfg = AppConfig.model_validate(
        {"source": {"camera": "nope"}, "cameras": [_cam().model_dump()]}
    )
    with pytest.raises(ValueError, match="'nope'.*cam-color"):
        resolve_camera(cfg)


# -- upsert -------------------------------------------------------------------


def test_upsert_round_trip_preserves_other_entries_and_sections(
    tmp_path: Path,
) -> None:
    p = tmp_path / "station.yaml"
    p.write_text(
        """
logging: {level: DEBUG}
dedup: {window_s: 9.0}
cameras:
  - id: cam-color
    name: "See3CAM_24CUG"
    fps: 30.0
  - id: cam-mono
    name: "See3CAM_37CUGM"
""",
        encoding="utf-8",
    )
    updated = _cam(
        fps=120.0,
        fourcc="UYVY",
        width=1920,
        height=1200,
        backend=Backend.DSHOW,
        settings=CameraSettings(exposure_auto=False, exposure=-6.0, gain=10.0),
    )
    upsert_camera_yaml(p, updated)

    cfg = load_config(p)  # the merged file must load cleanly
    assert [c.id for c in cfg.cameras] == ["cam-color", "cam-mono"]
    assert cfg.cameras[0].fps == 120.0
    assert cfg.cameras[0].settings.exposure == -6.0
    assert cfg.cameras[1].name == "See3CAM_37CUGM"  # untouched neighbor
    assert cfg.logging.level == "DEBUG"  # untouched section
    assert cfg.dedup.window_s == 9.0

    text = p.read_text(encoding="utf-8")
    assert text.startswith("# updated by palletscan calibrate ")
    # Key order of the original document survives the round trip.
    assert text.index("logging:") < text.index("dedup:") < text.index("cameras:")
    baks = list(tmp_path.glob("station.yaml.*.bak"))
    assert len(baks) == 1
    assert "DEBUG" in baks[0].read_text(encoding="utf-8")


def test_upsert_appends_new_entry_and_creates_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "new.yaml"
    upsert_camera_yaml(p, _cam())
    upsert_camera_yaml(p, _cam(id="cam-mono", name="See3CAM_37CUGM"))
    cfg = load_config(p)
    assert [c.id for c in cfg.cameras] == ["cam-color", "cam-mono"]
    # First write had nothing to back up; the second backs up the first.
    assert len(list(tmp_path.glob("new.yaml.*.bak"))) == 1


def test_upsert_validates_before_writing(tmp_path: Path) -> None:
    p = tmp_path / "broken.yaml"
    original = "motion: {algorithm: bogus}\n"
    p.write_text(original, encoding="utf-8")
    with pytest.raises(ValidationError):
        upsert_camera_yaml(p, _cam())
    # A save that cannot produce a loadable config touches nothing.
    assert p.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.bak")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_upsert_rejects_non_mapping_root(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        upsert_camera_yaml(p, _cam())
