"""Compatibility utilities for different Python versions."""

import sys
from datetime import timezone

if sys.version_info >= (3, 11):
    from datetime import UTC
    from enum import StrEnum
else:
    # Backport for Python 3.10
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """An Enum that inherits from str for Python 3.10 compatibility."""

        def _generate_next_value_(name: str, start: int, count: int, last_values: list[str]) -> str:
            return name.lower()

        def __str__(self) -> str:
            """Return the value of the enum member."""
            return self.value

    # UTC was added in Python 3.11
    UTC = timezone.utc  # type: ignore[assignment]


__all__ = ["StrEnum", "UTC"]
