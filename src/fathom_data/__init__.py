"""Fathom: a data context layer. What data exists, how it connects, and which projects use it."""
from .store import Fathom, FathomError

__all__ = ["Fathom", "FathomError"]
__version__ = "0.1.0"
