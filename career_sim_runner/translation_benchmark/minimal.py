"""Compatibility import for archived dictionary evidence/tests; not the active CLI."""
import sys
from .archive import minimal as _legacy
sys.modules[__name__] = _legacy
