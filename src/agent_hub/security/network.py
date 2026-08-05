"""Canonical network identifiers shared by trust and abuse-control boundaries."""

from ipaddress import IPv6Address, ip_address


def canonical_ip(value: str) -> str | None:
    """Return one stable IP spelling, collapsing IPv4-mapped IPv6 to IPv4."""

    if not value or "%" in value:
        return None
    try:
        parsed = ip_address(value)
    except ValueError:
        return None
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)
