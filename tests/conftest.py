from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: tests must not depend on a developer's local .env.
    return Settings(_env_file=None, environment="test")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# Test-only HS256 key (>= 32 bytes). Never used outside the test suite.
TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcdef"


def bearer(app, user_id) -> dict[str, str]:
    """Authorization header with a real token signed by this app's own key."""
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id, app.state.token_settings)}"}
