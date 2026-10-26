"""Client address for rate limiting, safe behind reverse proxies.

X-Forwarded-For is client-controlled: anyone can send "X-Forwarded-For: 1.2.3.4". It is
only read when the direct peer is a configured trusted proxy (DOCMIND_TRUSTED_PROXIES,
empty by default), and then only from the right: each trusted proxy appends the address it
saw, so the rightmost entry that is NOT a trusted proxy is the real client. Entries to the
left of it were written by the client and are ignored. A malformed entry stops the walk
and the last trusted hop's view is used, never a client-written value.
"""

from collections.abc import Sequence
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from starlette.requests import Request

Network = IPv4Network | IPv6Network


def parse_networks(spec: str) -> tuple[Network, ...]:
    """Comma-separated IPs or CIDRs ("10.0.0.0/8, 172.18.0.1")."""
    return tuple(ip_network(part.strip(), strict=False) for part in spec.split(",") if part.strip())


def _trusted(address: str, trusted: Sequence[Network]) -> bool:
    try:
        ip = ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in trusted)


def client_ip(request: Request, trusted: Sequence[Network]) -> str:
    peer = request.client.host if request.client else "unknown"
    if not trusted or not _trusted(peer, trusted):
        return peer
    hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",")]
    client = peer
    for hop in reversed([h for h in hops if h]):
        try:
            ip_address(hop)
        except ValueError:
            break
        if not _trusted(hop, trusted):
            return hop
        client = hop
    return client
