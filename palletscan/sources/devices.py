"""Device enumeration by stable name, never bare index (spec §2).

Indexes shuffle on reboot/replug; names do not. **Windows** (the factory
target): pygrabber lists DirectShow filter names; we pair list order with
``cv2.CAP_DSHOW`` index order (a documented assumption, verified on
arrival day — ARRIVAL_CHECKLIST step 1). **macOS** (dev, best-effort):
``system_profiler SPCameraDataType`` names paired with
``cv2.CAP_AVFOUNDATION``; profiler-order-vs-index-order is *not*
guaranteed. When a platform yields no names at all, enumeration returns
an empty list (loudly) and ``cameras[].fallback_index`` is the escape
hatch.

A :class:`DeviceInfo` index is only valid under the backend it was
enumerated with — never mix a DSHOW-derived index with MSMF.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass

import cv2

from palletscan.config import Backend

log = logging.getLogger(__name__)

_PROFILER_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One enumerated camera. ``backend`` is the cv2 ``CAP_*`` flag under
    which ``index`` is valid."""

    name: str
    index: int
    backend: int


def backend_flag(backend: Backend, platform: str = sys.platform) -> int:
    """Map a config Backend to the cv2 capture-API flag (AUTO is per-OS)."""
    if backend is Backend.DSHOW:
        return int(cv2.CAP_DSHOW)
    if backend is Backend.MSMF:
        return int(cv2.CAP_MSMF)
    if backend is Backend.AVFOUNDATION:
        return int(cv2.CAP_AVFOUNDATION)
    if platform == "win32":
        return int(cv2.CAP_DSHOW)
    if platform == "darwin":
        return int(cv2.CAP_AVFOUNDATION)
    return int(cv2.CAP_ANY)


def devices_from_names(names: list[str], backend: int) -> list[DeviceInfo]:
    """Pair enumeration-order names with capture indexes for one backend."""
    return [DeviceInfo(name=n, index=i, backend=backend) for i, n in enumerate(names)]


def parse_system_profiler(text: str) -> list[str]:
    """Camera names from ``system_profiler SPCameraDataType -json`` output."""
    data = json.loads(text)
    cams = data.get("SPCameraDataType", [])
    return [c["_name"] for c in cams if isinstance(c, dict) and "_name" in c]


def _list_windows() -> list[DeviceInfo]:
    """DirectShow filter names via pygrabber (win32-only dependency)."""
    from pygrabber.dshow_graph import FilterGraph

    names = FilterGraph().get_input_devices()
    return devices_from_names(list(names), int(cv2.CAP_DSHOW))


def _list_macos(run=None) -> list[DeviceInfo]:
    if run is None:  # resolved at call time so tests can patch subprocess.run
        run = subprocess.run
    proc = run(
        ["system_profiler", "SPCameraDataType", "-json"],
        capture_output=True,
        timeout=_PROFILER_TIMEOUT_S,
        check=True,
    )
    names = parse_system_profiler(proc.stdout)
    return devices_from_names(names, int(cv2.CAP_AVFOUNDATION))


def list_devices(platform: str = sys.platform) -> list[DeviceInfo]:
    """Enumerate cameras by name. Returns ``[]`` (loudly) when the platform
    gives no names — callers fall back to ``cameras[].fallback_index``."""
    try:
        if platform == "win32":
            return _list_windows()
        if platform == "darwin":
            return _list_macos()
        log.warning("no device-name enumeration on platform %r", platform)
        return []
    except Exception:
        log.warning(
            "device enumeration failed on %r; falling back to bare indices "
            "(set cameras[].fallback_index)",
            platform,
            exc_info=True,
        )
        return []


def find_device(devices: list[DeviceInfo], name: str) -> DeviceInfo:
    """Case-insensitive substring match that must hit **exactly one** device.

    Ambiguity is as fatal as absence: opening "a" camera when two match
    would silently run the wrong experiment arm.
    """
    needle = name.lower()
    matches = [d for d in devices if needle in d.name.lower()]
    if len(matches) == 1:
        return matches[0]
    found = [d.name for d in devices]
    if not matches:
        raise ValueError(f"no camera matching {name!r}; enumerated: {found}")
    raise ValueError(
        f"camera name {name!r} is ambiguous: matches "
        f"{[d.name for d in matches]} (enumerated: {found})"
    )
