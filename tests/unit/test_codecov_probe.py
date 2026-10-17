"""Throwaway test for a Codecov patch-target experiment. Never merged."""

from machina._codecov_probe import probe_covered


def test_probe_covered() -> None:
    assert probe_covered(1) == 2
