import pytest

from app.core.config import Settings


def test_env_prefix_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCMIND_APP_NAME", "FromEnv")
    monkeypatch.setenv("DOCMIND_ENVIRONMENT", "prod")
    monkeypatch.setenv("DOCMIND_JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("DOCMIND_ALLOWED_HOSTS", "api.example.com")

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


def test_generation_defaults_are_safe_and_offline() -> None:
    settings = Settings(_env_file=None)
    # No model is called unless an operator configures one.
    assert settings.generation_backend == "disabled"
    assert settings.generation_base_url.startswith("http://127.0.0.1:")
    assert settings.generation_context_window == 4096
    assert settings.rag_prompt_version == "grounded-qa-v2"
