"""Production configuration must fail closed instead of using development defaults."""
from __future__ import annotations

from app.core.config import DEFAULT_DEV_SECRET, Settings


def _production(**overrides) -> Settings:
    values = {
        "environment": "production",
        "secret_key": "s" * 48,
        "jwt_secret": "j" * 48,
        **overrides,
    }
    return Settings(**values)


def test_development_defaults_are_permitted_for_local_work():
    development = Settings(environment="development", secret_key=DEFAULT_DEV_SECRET, jwt_secret="")
    assert development.production_config_errors() == []


def test_the_checked_in_development_secret_is_refused_in_production():
    settings = _production(secret_key=DEFAULT_DEV_SECRET, jwt_secret="")
    errors = settings.production_config_errors()
    assert any("SECRET_KEY" in error for error in errors)
    assert any("JWT_SECRET" in error for error in errors)


def test_short_production_secrets_are_refused():
    settings = _production(secret_key="short", jwt_secret="also-short")
    errors = settings.production_config_errors()
    assert len(errors) == 2
    assert all("at least 32 characters" in error for error in errors)


def test_correct_production_secrets_pass():
    assert _production().production_config_errors() == []


def test_insecure_production_defaults_are_reported_as_warnings():
    warnings = _production(cookie_secure=False, encryption_key="").production_config_warnings()
    assert any("COOKIE_SECURE" in warning for warning in warnings)
    assert any("ENCRYPTION_KEY" in warning for warning in warnings)


def test_bootstrap_refuses_to_start_with_a_default_production_secret(monkeypatch):
    import pytest
    from app.services import bootstrap

    insecure = _production(secret_key=DEFAULT_DEV_SECRET, jwt_secret="")
    monkeypatch.setattr(bootstrap, "settings", insecure)
    with pytest.raises(RuntimeError, match="insecure production configuration"):
        bootstrap.enforce_production_configuration()


def test_bootstrap_starts_with_valid_production_secrets(monkeypatch):
    from app.services import bootstrap

    monkeypatch.setattr(bootstrap, "settings", _production())
    bootstrap.enforce_production_configuration()  # must not raise
