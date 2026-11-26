"""Pisa Core: the always-on personal assistant hub."""

from importlib.metadata import PackageNotFoundError, version

# The version lives once, in core/pyproject.toml; the installed package tells it. Run from a bare checkout
# (not installed), it says so instead of guessing a number.
try:
    __version__ = version("pisa-assistant")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
