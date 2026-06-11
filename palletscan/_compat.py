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


def import_pylibdmtx() -> ModuleType:
    """Import and return ``pylibdmtx.pylibdmtx`` with the distutils shim."""
    ensure_distutils()
    from pylibdmtx import pylibdmtx

    return pylibdmtx
