"""playmaker — multi-agent orchestration CLI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("playmaker-cli")
except PackageNotFoundError:  # source checkout without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
