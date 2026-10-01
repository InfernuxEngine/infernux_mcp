"""Executable paths preserve launcher identity, unlike project identities."""

from __future__ import annotations

import os


def python_launcher_path(value: str | os.PathLike[str]) -> str:
    """Make an executable path absolute without dereferencing venv symlinks.

    CPython discovers pyvenv.cfg relative to the invoked launcher. Resolving a
    venv's python symlink selects the base interpreter and loses its packages.
    This is not used for project roots, artifact access, or security identities.
    """
    if not value:
        return ""
    return os.path.abspath(os.path.normpath(os.fspath(value)))
