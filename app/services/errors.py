"""Use-case failures; the HTTP interface decides how to represent them."""


class InvalidCredentials(Exception):
    """The supplied credentials do not identify an account."""


class Unauthenticated(Exception):
    """A trusted caller could not be established."""


class Forbidden(Exception):
    """The caller lacks permission for this action or resource."""


class BarberNotFound(Exception):
    """The requested barber does not exist."""


class ServiceUnavailable(Exception):
    """A required data source is temporarily unavailable."""
