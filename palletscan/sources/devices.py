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
import re
import subprocess
import sys
from dataclasses import dataclass

import cv2

from palletscan.config import Backend

log = logging.getLogger(__name__)

_PROFILER_TIMEOUT_S = 2.0

#: USB DevicePath VID/PID fragment, e.g. ``usb#vid_2560&pid_c128&...``.
#: Hex, case-insensitive on BOTH the ``vid_``/``pid_`` prefix and the hex
#: digits (Windows mixes case); PID may be absent on composite devices.
_VID_RE = re.compile(r"vid_([0-9a-f]{4})", re.IGNORECASE)
_PID_RE = re.compile(r"pid_([0-9a-f]{4})", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class IdentityInfo:
    """A device's *stable* hardware identity, read from the DirectShow
    property bag on Windows. Used by the MSMF identity guard to detect a
    silent wrong-camera swap (replug reorder, OBS Virtual Camera grabbing
    a slot) that the first-frame shape gate cannot catch at the same
    resolution.

    All fields beyond ``friendly_name`` are best-effort: ``device_path``
    (and the ``vid``/``pid`` parsed from it) is present on real USB UVC
    devices but can be absent on virtual/composite devices, and the whole
    structure is unavailable on platforms with no property-bag enumeration
    (macOS/AVFoundation). Absence is "unverifiable", never "mismatch".
    """

    friendly_name: str
    device_path: str | None = None
    vid: str | None = None
    pid: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One enumerated camera. ``backend`` is the cv2 ``CAP_*`` flag under
    which ``index`` is valid. ``identity`` carries the stable hardware
    fingerprint when the platform can read it (Windows); it stays ``None``
    on macOS/AVFoundation and for every test/name-only fake, so callers
    that never opted into identity checking are byte-for-byte unaffected."""

    name: str
    index: int
    backend: int
    identity: IdentityInfo | None = None


def parse_vid_pid(device_path: str | None) -> tuple[str | None, str | None]:
    """Extract ``(vid, pid)`` (lowercase 4-hex strings) from a Windows
    DevicePath, or ``(None, None)`` when the path is missing or carries no
    VID. PID alone may be absent even when VID is present (composite
    devices); each is returned independently."""
    if not device_path:
        return None, None
    vid_m = _VID_RE.search(device_path)
    pid_m = _PID_RE.search(device_path)
    vid = vid_m.group(1).lower() if vid_m else None
    pid = pid_m.group(1).lower() if pid_m else None
    return vid, pid


def backend_flag(backend: Backend, platform: str = sys.platform) -> int:
    """Map a config Backend to the cv2 capture-API flag (AUTO is per-OS)."""
    if backend is Backend.DSHOW:
        return int(cv2.CAP_DSHOW)
    if backend is Backend.PYGRABBER:
        # pygrabber IS DirectShow and enumerates in the same order as our DSHOW
        # name enumeration, so resolve names against the DSHOW index. The flag
        # is informational here — PyGrabberCapture ignores it.
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
    """Pair enumeration-order names with capture indexes for one backend.

    ``identity`` is left ``None`` — this is the name-only path used by the
    macOS branch and every test fake, so the identity guard treats those
    devices as "unverifiable" rather than asserting anything about them."""
    return [DeviceInfo(name=n, index=i, backend=backend) for i, n in enumerate(names)]


def devices_from_monikers(
    monikers: list, backend: int, *, read_str=None
) -> list[DeviceInfo]:
    """Windows path: pair DirectShow filter monikers with capture indexes,
    reading BOTH ``FriendlyName`` and ``DevicePath`` from each moniker's
    property bag (the same ``BindToStorage``/``IPropertyBag.Read`` pattern
    pygrabber's ``get_moniker_name`` uses, extended to also pull the path).

    ``read_str`` is the seam tests inject: ``f(moniker, prop) -> str`` so
    the COM property-bag read is faked without hardware. The default reads
    the real bag. ``DevicePath`` is absent on some virtual/composite
    devices, so its read is wrapped and degrades to ``None`` — a device
    with a friendly name but no path is enumerable, just not VID/PID-
    fingerprintable. A moniker whose ``FriendlyName`` read itself raises is
    SKIPPED (logged), never allowed to abort the whole enumeration: one
    broken property bag must not disable name resolution and the identity
    guard for every healthy device. Skipping never compacts the indexes —
    the index is the enumeration POSITION, which is what pairs with the
    ``cv2.CAP_DSHOW`` open order.
    """
    if read_str is None:
        read_str = _read_moniker_prop
    out: list[DeviceInfo] = []
    for i, moniker in enumerate(monikers):
        try:
            friendly = read_str(moniker, "FriendlyName")
        except Exception as exc:
            log.warning(
                "skipping device at enumeration index %d: FriendlyName "
                "read failed: %r",
                i,
                exc,
            )
            continue
        try:
            device_path = read_str(moniker, "DevicePath")
        except Exception:
            device_path = None  # absent on virtual/composite devices
        vid, pid = parse_vid_pid(device_path)
        out.append(
            DeviceInfo(
                name=friendly,
                index=i,
                backend=backend,
                identity=IdentityInfo(
                    friendly_name=friendly,
                    device_path=device_path,
                    vid=vid,
                    pid=pid,
                ),
            )
        )
    return out


def _read_moniker_prop(moniker, prop: str) -> str:
    """Read one string property from a DirectShow moniker's property bag.

    Mirrors pygrabber's ``get_moniker_name`` (BindToStorage ->
    QueryInterface(IPropertyBag) -> Read), generalized to any property
    name so we can pull ``DevicePath`` alongside ``FriendlyName``."""
    from comtypes.persist import IPropertyBag

    bag = moniker.BindToStorage(0, 0, IPropertyBag._iid_).QueryInterface(
        IPropertyBag
    )
    return bag.Read(prop, pErrorLog=None)


def parse_system_profiler(text: str) -> list[str]:
    """Camera names from ``system_profiler SPCameraDataType -json`` output."""
    data = json.loads(text)
    cams = data.get("SPCameraDataType", [])
    return [c["_name"] for c in cams if isinstance(c, dict) and "_name" in c]


def _enumerate_windows_monikers() -> list:
    """Iterate the DirectShow VideoInputDevice category and return the raw
    monikers in enumeration order — the same enumeration pygrabber's
    ``get_available_filters`` walks, but kept as monikers so we can read
    BOTH FriendlyName and DevicePath off each one (it discards them after
    pulling the name)."""
    from comtypes import GUID
    from pygrabber.dshow_core import ICreateDevEnum
    from pygrabber.dshow_ids import DeviceCategories, clsids

    sys_enum = client_create_object(clsids.CLSID_SystemDeviceEnum, ICreateDevEnum)
    filter_enum = sys_enum.CreateClassEnumerator(
        GUID(DeviceCategories.VideoInputDevice), dwFlags=0
    )
    monikers: list = []
    try:
        moniker, count = filter_enum.Next(1)
    except ValueError:
        return monikers  # CreateClassEnumerator yields None when no devices
    while count > 0:
        monikers.append(moniker)
        moniker, count = filter_enum.Next(1)
    return monikers


def client_create_object(clsid, interface):
    """Thin wrapper around comtypes.client.CreateObject (own function so
    the COM creation is trivially patchable in a Windows-only test)."""
    from comtypes import client

    return client.CreateObject(clsid, interface=interface)


#: HRESULT: the calling thread is already CoInitialized under a DIFFERENT
#: apartment model. COM is usable on it as-is, but the call took no
#: reference, so it must NOT be paired with a CoUninitialize.
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106


def _co_initialize_thread() -> bool:
    """Initialize COM on the calling thread for the DirectShow enumeration.

    comtypes auto-initializes only the thread that first imports it, so
    ``list_devices()`` from any other thread — the watchdog consumer thread
    that runs every ``CameraSource.reopen()`` — raised CO_E_NOTINITIALIZED,
    swallowed to ``[]``: name resolution and the identity guard silently
    disabled on exactly the reconnect path they were built for.

    Returns True when this call owes a paired ``CoUninitialize``: S_OK and
    S_FALSE (already initialized in this mode) both add a reference.
    RPC_E_CHANGED_MODE returns False — the thread already runs a different
    apartment model, usable as-is, and uninitializing would release a
    reference this call never took.
    """
    import comtypes

    try:
        comtypes.CoInitializeEx()  # honors sys.coinit_flags like comtypes
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == _RPC_E_CHANGED_MODE:
            return False
        raise


def _co_uninitialize_thread() -> None:
    import comtypes

    comtypes.CoUninitialize()


def _list_windows() -> list[DeviceInfo]:
    """DirectShow filter names + identities via the property bag (win32-only
    dependency). Enumeration order is paired with ``cv2.CAP_DSHOW`` indexes,
    the documented arrival-day assumption; FriendlyName backs name
    resolution, DevicePath (VID/PID) backs the identity guard.

    The calling thread is CoInitialized for the duration (balanced), and
    the moniker refs are dropped BEFORE the paired CoUninitialize so their
    COM Release() runs inside the initialized window."""
    owes_uninit = _co_initialize_thread()
    try:
        monikers = _enumerate_windows_monikers()
        try:
            return devices_from_monikers(monikers, int(cv2.CAP_DSHOW))
        finally:
            del monikers
    finally:
        if owes_uninit:
            _co_uninitialize_thread()


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
    except Exception as exc:
        # Name the actual exception IN the message (not just exc_info,
        # which structured/console formatters may drop): a swallowed
        # enumeration failure disables name resolution and the identity
        # guard, and must be diagnosable from a single log line.
        log.warning(
            "device enumeration failed on %r: %r — falling back to bare "
            "indices (set cameras[].fallback_index)",
            platform,
            exc,
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


def identity_for_name(
    devices: list[DeviceInfo], name: str
) -> IdentityInfo | None:
    """Resolve the EXPECTED identity for a configured camera name.

    IMPORTANT NUANCE (read before trusting this for MSMF): the index a name
    resolves to here is the DSHOW *enumeration* index, and under MSMF the
    capture index != the DSHOW enumeration index. We therefore canNOT
    fingerprint "the device OpenCV actually opened under MSMF" directly. What
    this returns is the identity of "what the configured name resolves to in
    the live DSHOW enumeration" — and the guard then asserts that enumeration
    hasn't *drifted* from the calibrated fingerprint. That is an INDIRECT
    proof (the enumeration is consistent with calibration), not a direct read
    of the opened MSMF handle. Do not overclaim it as the latter.

    Returns ``None`` when the name is absent/ambiguous (the caller already
    surfaces those via :func:`find_device`) or when the matched device
    carries no identity (macOS, or a DevicePath that could not be read).
    """
    needle = name.lower()
    matches = [d for d in devices if needle in d.name.lower()]
    if len(matches) != 1:
        return None
    return matches[0].identity
