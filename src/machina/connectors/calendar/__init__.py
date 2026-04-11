"""Calendar & scheduling connectors for production and maintenance planning."""

from machina.connectors.calendar.connector import CalendarConnector

# Short public API alias
Calendar = CalendarConnector

__all__ = [
    "Calendar",
    "CalendarConnector",
]
