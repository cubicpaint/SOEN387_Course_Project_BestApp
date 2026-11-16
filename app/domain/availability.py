"""Public barber display data."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BarberSummary:
    id: str
    display_name: str
