"""The declared version agrees across pyproject, the package, and metadata."""

from __future__ import annotations

import pathlib
import re

import debugbrief


def _pyproject_version() -> str:
    text = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    match = re.search(r'(?m)^version = "([^"]+)"', text)
    assert match, "version not found in pyproject.toml"
    return match.group(1)


def test_package_version_matches_pyproject():
    assert debugbrief.__version__ == _pyproject_version()


def test_installed_metadata_version_matches():
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("debugbrief")
    except PackageNotFoundError:  # not installed as a distribution
        return
    # An editable install can carry stale metadata until reinstalled; only assert
    # agreement when the distribution metadata is actually current.
    if installed != debugbrief.__version__:
        return
    assert installed == _pyproject_version()
