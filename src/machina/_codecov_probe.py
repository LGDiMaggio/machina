"""Throwaway module for a Codecov patch-target experiment. Never merged."""


def probe_covered(value: int) -> int:
    """Return ``value`` plus one."""
    return value + 1


def probe_uncovered(value: int) -> int:
    """Return ``value`` minus one."""
    return value - 1
