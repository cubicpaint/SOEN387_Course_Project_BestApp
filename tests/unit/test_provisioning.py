from datetime import UTC, datetime, timedelta
from getpass import GetPassWarning
from types import SimpleNamespace
import warnings

import pytest

from app.services.provisioning import DemoProvisioningError, TORONTO, demo_block_windows, validate_demo_password


@pytest.mark.parametrize("now", [datetime(2026, 3, 6, 23, tzinfo=UTC), datetime(2026, 10, 30, 23, tzinfo=UTC)])
def test_demo_windows_are_future_local_half_hour_blocks_across_dst(now):
    windows = demo_block_windows(now)
    local_dates = {window.starts_at.astimezone(TORONTO).date() for window in windows}

    assert len(windows) == 112
    assert len(local_dates) == 7
    assert min(local_dates) == now.astimezone(TORONTO).date() + timedelta(days=1)
    assert len({window.starts_at for window in windows}) == 112
    assert {window.starts_at.astimezone(TORONTO).utcoffset() for window in windows} == {
        timedelta(hours=-5), timedelta(hours=-4)
    }
    for window in windows:
        local_start = window.starts_at.astimezone(TORONTO)
        assert window.starts_at > now
        assert window.ends_at - window.starts_at == timedelta(minutes=30)
        assert local_start.minute in (0, 30)
        assert 9 <= local_start.hour < 17
        assert window.starts_at.utcoffset() == timedelta(0)


def test_demo_date_uses_toronto_rather_than_utc_midnight():
    now = datetime(2026, 11, 16, 2, tzinfo=UTC)  # Still November 15 in Toronto.
    assert demo_block_windows(now)[0].starts_at.astimezone(TORONTO).date().isoformat() == "2026-11-16"


def test_demo_clock_rejects_naive_datetime():
    with pytest.raises(DemoProvisioningError, match="timezone"):
        demo_block_windows(datetime(2026, 11, 15))


@pytest.mark.parametrize("password", ["", "too-short", "your-demo-password-here", "a-long-placeholder-secret", "replace-me-please"])
def test_missing_weak_or_placeholder_demo_password_is_rejected_without_echo(password):
    with pytest.raises(DemoProvisioningError) as error:
        validate_demo_password(password)
    if password:
        assert password not in str(error.value)


def test_explicit_demo_password_is_accepted():
    validate_demo_password("a sufficiently long testing phrase")


def test_cli_refuses_echoed_password_fallback(monkeypatch, capsys):
    from scripts import provision_demo as command

    monkeypatch.setattr(command, "Settings", lambda: object())
    monkeypatch.delenv("BESTAPP_DEMO_PASSWORD", raising=False)

    def unavailable_private_input(_prompt):
        warnings.warn("Private input is unavailable", GetPassWarning)
        pytest.fail("The command allowed getpass to continue to echoed input")

    monkeypatch.setattr(command, "getpass", unavailable_private_input)
    assert command.main() == 1
    assert "BESTAPP_DEMO_PASSWORD" in capsys.readouterr().err


def test_cli_prompts_securely_when_optional_password_environment_is_empty(monkeypatch, capsys):
    from app.services.provisioning import ProvisioningSummary
    from scripts import provision_demo as command

    supplied_password = "private password supplied by prompt"
    observed_passwords = []
    monkeypatch.setenv("BESTAPP_DEMO_PASSWORD", "")
    monkeypatch.setattr(command, "Settings", lambda: object())
    monkeypatch.setattr(command, "getpass", lambda _prompt: supplied_password)
    monkeypatch.setattr(command, "create_engine_from_settings", lambda _settings: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(command, "create_session_factory", lambda _engine: object())

    def provision(_session_factory, password):
        observed_passwords.append(password)
        return ProvisioningSummary((), (), 0, 0, 0, 0, 0)

    monkeypatch.setattr(command, "provision_demo", provision)
    assert command.main() == 0
    assert observed_passwords == [supplied_password]
    output = capsys.readouterr()
    assert supplied_password not in output.out + output.err
