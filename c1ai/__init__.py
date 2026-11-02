"""Capture One AI package identity, captured when this process imports it."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('capture-one-ai-rebuild')
except PackageNotFoundError:
    __version__ = 'source-uninstalled'
