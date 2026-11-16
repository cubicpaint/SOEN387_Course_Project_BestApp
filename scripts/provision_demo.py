"""Run explicitly with ``python -m scripts.provision_demo`` after migrations."""

from dataclasses import asdict
from getpass import GetPassWarning, getpass
import json
import os
import sys
import warnings

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.data.database import create_engine_from_settings, create_session_factory
from app.services.provisioning import DemoProvisioningError, provision_demo


def main() -> int:
    engine = None
    try:
        settings = Settings()
        password = os.environ.get("BESTAPP_DEMO_PASSWORD") or None
        if password is None:
            # Refuse getpass's echoed-input fallback when no interactive terminal exists.
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                password = getpass("Demo password for newly created accounts (12+ characters): ")
        engine = create_engine_from_settings(settings)
        summary = provision_demo(create_session_factory(engine), password)
        print(json.dumps(asdict(summary)))
        return 0
    except ValidationError:
        print("Invalid application configuration. Check the required environment variables in README.md.", file=sys.stderr)
        return 1
    except DemoProvisioningError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except GetPassWarning:
        print("Private password entry is unavailable. Set BESTAPP_DEMO_PASSWORD in the process environment.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Demo provisioning cancelled; no password was supplied.", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("Database setup failed. Check the environment configuration and initial migration.", file=sys.stderr)
        return 1
    except Exception:
        print("Demo provisioning failed. Check the application setup; no exception details were printed.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
