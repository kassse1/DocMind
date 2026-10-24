import pytest

from app.core.config import Settings


def test_env_prefix_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCMIND_APP_NAME", "FromEnv")
    monkeypatch.setenv("DOCMIND_ENVIRONMENT", "prod")

    settings = Settings(_env_file=None)

    assert settings.app_name == "FromEnv"
    assert settings.environment == "prod"


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "ShouldNotApply")

    assert Settings(_env_file=None).app_name == "DocMind"


def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCMIND_ENVIRONMENT", "staging")

    with pytest.raises(ValueError):
        Settings(_env_file=None)
