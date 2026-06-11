"""Compatibility shims for third-party libraries.

pylibdmtx 0.1.10 imports ``distutils`` (removed from the stdlib in Python
3.12). setuptools normally restores it via an executable ``.pth`` file, but
``.pth`` processing can be disabled silently (e.g. macOS can mark the file
with the UF_HIDDEN flag, which ``site.py`` skips). Installing the shim
programmatically before the import makes pylibdmtx loadable regardless of
how ``site`` processed ``.pth`` files.
"""

from __future__ import annotations

from types import ModuleType


def ensure_distutils() -> None:
    """Make ``import distutils`` work on Python 3.12+ via setuptools."""
    try:
        import distutils  # noqa: F401
    except ImportError:
        import _distutils_hack

        _distutils_hack.add_shim()


LIB_HELP = (
    "{lib} native library failed to load ({err}). "
    "macOS: `brew install {pkg}`; if loading still fails: "
    "`export DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib`. "
    "Windows: the pip wheel bundles the DLL — reinstall the package."
)


def import_pylibdmtx() -> ModuleType:
    """Import and return ``pylibdmtx.pylibdmtx`` with the distutils shim.

    Raises an actionable :class:`RuntimeError` (install/remediation steps)
    instead of a bare ImportError when the native library fails to load.
    """
    try:
        ensure_distutils()
        from pylibdmtx import pylibdmtx
    except Exception as exc:
        raise RuntimeError(
            LIB_HELP.format(lib="libdmtx", pkg="libdmtx", err=exc)
        ) from exc

    return pylibdmtx
