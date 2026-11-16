"""Small HTTP input checks that are stricter than default request parsing."""

import json

from fastapi import Request
from fastapi.routing import APIRoute


class InputError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def validate_query(request: Request, allowed: set[str]) -> None:
    names = [name for name, _ in request.query_params.multi_items()]
    if set(names) - allowed or len(names) != len(set(names)):
        raise InputError(422, "validation_error", "Query parameters are invalid.")


def _unique_members(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON member")
        result[name] = value
    return result


def _invalid_constant(value: str):
    raise ValueError("Non-JSON number")


async def read_json(request: Request) -> object:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise InputError(415, "unsupported_media_type", "Send an application/json request.")
    try:
        return json.loads(
            await request.body(), object_pairs_hook=_unique_members, parse_constant=_invalid_constant
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InputError(400, "invalid_json", "The JSON body is malformed.") from None


class JSONRoute(APIRoute):
    """Check JSON syntax/media before FastAPI applies the declared body model."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request: Request):
            await read_json(request)
            return await handler(request)

        return validated
