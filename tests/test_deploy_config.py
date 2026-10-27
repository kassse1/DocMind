"""Security invariants of the deployment files, checked without Docker.

These guard the decisions documented in docs/production-deployment.md: PostgreSQL never
published, every port on loopback, hardened app containers, the BFF never trusting
client-supplied X-Forwarded-For, and the release check creating no Docker volumes.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
APP_SERVICES = ("migrate", "api", "worker", "web")


class ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts Compose's !override / !reset merge tags."""


def _tagged(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    ComposeLoader.add_constructor(_tag, _tagged)


def load(name: str) -> dict:
    return yaml.load((DEPLOY / name).read_text(encoding="utf-8"), Loader=ComposeLoader)


@pytest.fixture(scope="module")
def base() -> dict:
    return load("compose.yaml")


def published(service: dict) -> list[str]:
    return [str(p) for p in service.get("ports", [])]


def test_postgres_is_never_published_and_only_on_the_internal_network(base):
    postgres = base["services"]["postgres"]
    assert "ports" not in postgres
    assert postgres["networks"] == ["db"]
    assert base["networks"]["db"]["internal"] is True
    assert base["networks"]["app"]["internal"] is True


COMPOSE_FILES = ["compose.yaml", "compose.smoke.yaml", "compose.release-check.yaml"]


@pytest.mark.parametrize("file", COMPOSE_FILES)
def test_every_published_port_is_bound_to_loopback(file):
    services = load(file)["services"]
    ports = [port for service in services.values() for port in published(service)]
    assert ports, f"{file} publishes nothing"
    for port in ports:
        assert port.startswith("127.0.0.1:"), f"{file}: {port} is not loopback-only"


def test_app_containers_are_hardened(base):
    for name in APP_SERVICES:
        service = base["services"][name]
        assert service["read_only"] is True, name
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name
        assert not service.get("privileged"), name
        assert "cap_add" not in service, name


def test_no_container_gets_the_docker_socket(base):
    for name, service in base["services"].items():
        for volume in service.get("volumes", []):
            assert "docker.sock" not in str(volume), name


def test_web_uses_the_internal_api_url_and_never_forwards_client_addresses(base):
    web = base["services"]["web"]
    env = web["environment"]
    assert env["DOCMIND_API_URL"] == "http://api:8000"
    assert env["DOCMIND_BFF_FORWARD_CLIENT_IP"] == "false"
    # Not on the database network, and no secrets of the backend.
    assert "db" not in web["networks"]
    assert not any(key.startswith(("POSTGRES_", "DOCMIND_JWT")) for key in env)
    assert web["depends_on"]["api"]["condition"] == "service_healthy"


def test_api_accepts_the_internal_host_and_trusts_no_proxy_by_default(base):
    env = base["x-app"]["environment"]
    assert env["DOCMIND_ALLOWED_HOSTS"].startswith("api,")
    assert env["DOCMIND_TRUSTED_PROXIES"] == "${DOCMIND_TRUSTED_PROXIES:-}"
    assert "app" in base["services"]["api"]["networks"]
    assert "app" not in base["services"]["worker"]["networks"]


def test_migrations_run_once_before_api_and_worker(base):
    services = base["services"]
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["migrate"]["restart"] == "no"
    for name in ("api", "worker"):
        condition = services[name]["depends_on"]["migrate"]["condition"]
        assert condition == "service_completed_successfully", name


def test_ollama_is_opt_in(base):
    assert base["services"]["ollama"]["profiles"] == ["ollama"]
    assert "ports" not in base["services"]["ollama"]


def test_release_check_creates_no_docker_volumes():
    services = load("compose.release-check.yaml")["services"]
    assert services["postgres"]["volumes"] == []
    assert any(t.startswith("/var/lib/postgresql/data") for t in services["postgres"]["tmpfs"])
    for name in ("migrate", "api", "worker"):
        for volume in services[name]["volumes"]:
            source = str(volume).split(":", 1)[0]
            # Only host paths supplied for the run, never a named volume.
            assert source.startswith("${"), f"{name}: {volume}"


def test_env_template_has_no_secret_values():
    values = dict(
        line.split("=", 1)
        for line in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert values["POSTGRES_PASSWORD"] == ""
    assert values["DOCMIND_JWT_SECRET_KEY"] == ""
    assert values["DOCMIND_ENVIRONMENT"] == "prod"
    assert values["DOCMIND_TRUSTED_PROXIES"] == ""


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "frontend/Dockerfile"])
def test_images_use_digest_pinned_bases_and_a_non_root_user(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    bases = re.findall(r"^ARG \w+_IMAGE=(\S+)$", text, flags=re.MULTILINE)
    assert bases and all(re.search(r"@sha256:[0-9a-f]{64}$", b) for b in bases), bases
    users = re.findall(r"^USER (\S+)$", text, flags=re.MULTILINE)
    assert users and users[-1] not in ("root", "0", "0:0"), users


def test_build_contexts_exclude_secrets_and_local_state():
    backend = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    frontend = (ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", ".env.*", "deploy/.env", "*.dump", ".git"):
        assert entry in backend, entry
    for entry in (".env", ".env.*", "node_modules", ".next"):
        assert entry in frontend, entry
