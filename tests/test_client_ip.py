"""Client address extraction behind proxies, and rate-limit spec parsing."""

import pytest
from starlette.requests import Request

from app.core.client_ip import client_ip, parse_networks
from app.services.rate_limit import Limit

PROXIES = parse_networks("10.0.0.0/8, 172.18.0.2")


def request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


def test_without_trusted_proxies_the_peer_is_the_client_and_xff_is_ignored() -> None:
    assert client_ip(request("203.0.113.9", "1.2.3.4"), ()) == "203.0.113.9"


def test_spoofed_xff_from_an_untrusted_peer_is_ignored() -> None:
    assert client_ip(request("203.0.113.9", "10.0.0.1, 1.2.3.4"), PROXIES) == "203.0.113.9"


def test_trusted_proxy_rightmost_untrusted_hop_is_the_client() -> None:
    # The client wrote "6.6.6.6" itself; the proxy appended the real address.
    assert client_ip(request("172.18.0.2", "6.6.6.6, 198.51.100.7"), PROXIES) == "198.51.100.7"


def test_chain_of_trusted_proxies_is_skipped() -> None:
    assert client_ip(
        request("10.0.0.5", "198.51.100.7, 10.1.1.1, 10.2.2.2"), PROXIES
    ) == "198.51.100.7"


@pytest.mark.parametrize(
    "forwarded", ["", "not-an-ip", "198.51.100.7, garbage", "1.2.3.4\r\nX-Evil: 1"]
)
def test_missing_or_malformed_xff_never_yields_a_client_written_value(forwarded) -> None:
    ip = client_ip(request("172.18.0.2", forwarded), PROXIES)
    assert ip in {"172.18.0.2"} | ({"198.51.100.7"} if "198.51" in forwarded else set())


def test_ipv6_proxies() -> None:
    proxies = parse_networks("fd00::/8")
    assert client_ip(request("fd00::1", "2001:db8::5"), proxies) == "2001:db8::5"


@pytest.mark.parametrize("spec", ["0/60", "5/0", "abc", "5", "-1/60"])
def test_invalid_limit_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        Limit.parse(spec)


def test_limit_spec() -> None:
    assert Limit.parse("20/60") == Limit(20, 60)
