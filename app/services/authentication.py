"""Verify passwords/tokens and resolve current database identity."""

from collections.abc import Callable
from dataclasses import dataclass

from pwdlib import PasswordHash
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.data.users import UserRepository
from app.domain.identity import Caller
from app.services.errors import InvalidCredentials, ServiceUnavailable, Unauthenticated
from app.services.tokens import TokenService


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    expires_in: int
    caller: Caller


class AuthenticationService:
    def __init__(self, session_factory: Callable[[], Session], tokens: TokenService):
        self.session_factory = session_factory
        self.tokens = tokens
        self.passwords = PasswordHash.recommended()
        # Unknown accounts still pay for password verification.
        self.dummy_hash = self.passwords.hash("unused dummy password for timing")

    def login(self, email: str, password: str) -> LoginResult:
        try:
            with self.session_factory() as session:
                repository = UserRepository(session)
                user = repository.by_email(email)
                password_hash = user.password_hash if user is not None else self.dummy_hash
                matches = self.passwords.verify(password, password_hash)
                if not matches or user is None:
                    raise InvalidCredentials()
                caller = repository.caller(user)
        except SQLAlchemyError:
            raise ServiceUnavailable() from None
        return LoginResult(self.tokens.issue(caller.id), self.tokens.settings.jwt_ttl_seconds, caller)

    def authenticate(self, token: str) -> Caller:
        user_id = self.tokens.verify(token)
        try:
            with self.session_factory() as session:
                repository = UserRepository(session)
                user = repository.by_id(user_id)
                if user is None:
                    raise Unauthenticated()
                return repository.caller(user)
        except SQLAlchemyError:
            raise ServiceUnavailable() from None
