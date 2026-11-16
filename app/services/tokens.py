"""Issue and verify this application's short-lived signed identity tokens."""

from collections.abc import Callable
from datetime import UTC, datetime
import re

import jwt

from app.config import AuthSettings
from app.services.errors import Unauthenticated


USER_ID = re.compile(r"usr_[A-Za-z0-9_-]+", re.ASCII)


class TokenService:
    def __init__(self, settings: AuthSettings, clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)

    def issue(self, user_id: str) -> str:
        if not isinstance(user_id, str) or len(user_id) > 64 or USER_ID.fullmatch(user_id) is None:
            raise ValueError("A token requires a valid stored user identity.")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("The token clock must include a timezone.")
        issued_at = int(now.timestamp())
        return jwt.encode(
            {
                "sub": user_id,
                "iss": self.settings.jwt_issuer,
                "aud": self.settings.jwt_audience,
                "iat": issued_at,
                "exp": issued_at + self.settings.jwt_ttl_seconds,
            },
            self.settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )

    def verify(self, token: str) -> str:
        try:
            claims = jwt.decode(
                token,
                self.settings.jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                issuer=self.settings.jwt_issuer,
                audience=self.settings.jwt_audience,
                options={"require": ["sub", "iss", "aud", "iat", "exp"], "strict_aud": True},
                leeway=0,
            )
            subject = claims["sub"]
            if (
                not isinstance(subject, str)
                or len(subject) > 64
                or USER_ID.fullmatch(subject) is None
                or type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= 3600
            ):
                raise jwt.InvalidTokenError("Invalid identity or lifetime.")
        except (jwt.InvalidTokenError, TypeError, ValueError, OverflowError):
            raise Unauthenticated() from None
        return subject
