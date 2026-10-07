"""Production hardening tests: doctor --strict gate and AI key validators."""
from __future__ import annotations

import argparse

import pytest

from app import cli
from app.config import Settings


def _strict_settings(**overrides):
    base = dict(
        environment="production",
        secret_key="a" * 40,
        worker_secret_pepper="b" * 20,
        secrets_encryption_key="c" * 32,
        database_url="postgresql+psycopg2://u:p@localhost:5432/db",
        cors_origins="https://app.example.com",
        groq_api_key="fake-groq-key",
        embedded_worker_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_doctor_strict_passes_with_prod_safe_config(monkeypatch, capsys):
    monkeypatch.setattr(cli, "settings", _strict_settings())
    assert cli.cmd_doctor_strict() == 0
    out = capsys.readouterr().out
    assert "PASS — production-safe" in out


def test_dev_secrets_rejected_at_settings_construction():
    """The model_validator is the primary guard; doctor --strict is the CI layer."""
    from app.config import DEV_SECRET
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        _strict_settings(secret_key=DEV_SECRET)


def test_doctor_strict_fails_on_sqlite(monkeypatch, capsys):
    monkeypatch.setattr(cli, "settings", _strict_settings(database_url="sqlite:///./x.db"))
    assert cli.cmd_doctor_strict() == 1


def test_doctor_strict_fails_on_http_cors(monkeypatch, capsys):
    monkeypatch.setattr(cli, "settings", _strict_settings(cors_origins="http://app.example.com"))
    assert cli.cmd_doctor_strict() == 1


def test_doctor_strict_fails_on_embedded_worker(monkeypatch, capsys):
    monkeypatch.setattr(cli, "settings", _strict_settings(embedded_worker_enabled=True))
    assert cli.cmd_doctor_strict() == 1


def test_empty_ai_key_rejected_at_settings_construction():
    """Empty AI key with matching provider fails at boot in production."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="GROQ_API_KEY"):
        _strict_settings(groq_api_key="")


def test_production_requires_groq_key():
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        _strict_settings(groq_api_key="")


def test_production_requires_gemini_key():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        _strict_settings(ai_provider="gemini", gemini_api_key="")


def test_secret_file_env_var_is_read(tmp_path, monkeypatch):
    """Docker-secrets style *_FILE vars feed Settings (production: secrets)."""
    secret_file = tmp_path / "secret_key.txt"
    secret_file.write_text("file-based-secret-" + "x" * 30)
    monkeypatch.setenv("SECRET_KEY_FILE", str(secret_file))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    s = Settings(environment="development")
    assert s.secret_key == "file-based-secret-" + "x" * 30


def test_secret_file_missing_raises(tmp_path, monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / "nonexistent.txt"))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError, match="unreadable file"):
        Settings(environment="development")


def test_explicit_value_wins_over_secret_file(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret_key.txt"
    secret_file.write_text("from-file-" + "x" * 30)
    monkeypatch.setenv("SECRET_KEY_FILE", str(secret_file))
    monkeypatch.setenv("SECRET_KEY", "explicit-value-" + "y" * 30)
    s = Settings(environment="development")
    assert s.secret_key == "explicit-value-" + "y" * 30


def test_api_rate_limiter_allows_then_blocks():
    from app.core.rate_limit import api_limiter

    limiter = api_limiter()
    limiter.reset()
    original = limiter.max_hits
    limiter.max_hits = 2
    try:
        assert limiter.allow("api:1.2.3.4")[0]
        assert limiter.allow("api:1.2.3.4")[0]
        allowed, retry_after = limiter.allow("api:1.2.3.4")
        assert not allowed
        assert retry_after >= 1
    finally:
        limiter.max_hits = original
        limiter.reset()
