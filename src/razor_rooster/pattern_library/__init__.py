"""``pattern_library`` — The Bone Pile (T-PL-001).

Historical event-pattern catalogue. Computes base rates, precursor
signatures, and analogue feature spaces for operator-defined event
classes. Outputs are versioned, calibrated, and tagged so downstream
subsystems can detect mismatches.

The library is operator-extensible: each event class is a Python module
under ``classes/`` exposing a module-level ``CLASS = EventClass(...)``.
"""

from razor_rooster.pattern_library.version import LIBRARY_VERSION

__all__ = ["LIBRARY_VERSION"]
