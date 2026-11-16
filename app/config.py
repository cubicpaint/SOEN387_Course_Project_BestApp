"""Environment configuration, with secrets hidden from diagnostics."""

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationError(RuntimeError):
    """Required runtime settings are missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    database_url: SecretStr = Field(alias="DATABASE_URL")
    db_connect_timeout: int = Field(default=3, ge=1, le=30, alias="DB_CONNECT_TIMEOUT")
    db_query_timeout_ms: int = Field(default=2000, ge=1, le=30000, alias="DB_QUERY_TIMEOUT_MS")
    db_pool_timeout: int = Field(default=3, ge=1, le=30, alias="DB_POOL_TIMEOUT")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            valid = (
                url.drivername == "postgresql+psycopg"
                and url.host
                and url.username
                and url.password
                and url.database
                and url.password not in {"replace_me", "CHANGE_ME", "your-password"}
                and set(url.query).issubset({"options", "sslmode"})
                and all(isinstance(item, str) for item in url.query.values())
            )
            if "sslmode" in url.query and url.query["sslmode"] not in {
                "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
            }:
                valid = False
            # Accessing port also checks that it is numeric.
            if url.port is not None and not 1 <= url.port <= 65535:
                valid = False
        except (ArgumentError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Use a complete PostgreSQL+psycopg URL with non-placeholder credentials.")
        return value


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        fields = sorted({str(item["loc"][0]) for item in error.errors(include_input=False)})
        raise ConfigurationError("Missing or invalid configuration: " + ", ".join(fields)) from None
