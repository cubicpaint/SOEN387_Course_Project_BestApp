"""The fixed identity and service choices used by BestApp."""

from enum import StrEnum


class Role(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"


class ServiceChoice(StrEnum):
    HAIRCUT = "haircut"
    BEARD = "beard"
    BOTH = "both"
