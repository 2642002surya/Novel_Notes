"""Network fetching for novel pages, hardened against SSRF.

The bot fetches URLs that any server member can post, so it must never be
usable to probe internal addresses (localhost, cloud metadata endpoints, LAN).
Protection is layered:

* the connector's resolver only ever returns globally routable addresses, so a
  hostname that resolves (or rebinds) to a private IP cannot be connected to;
* IP-literal hosts (which skip DNS) are checked explicitly;
* redirects are followed manually so every hop is re-validated;
* response size and time are capped.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

logger = logging.getLogger("NovelGuard.Fetcher")

USER_AGENT = (
    "Mozilla/5.0 (compatible; NovelGuardBot/2.0; +https://github.com/) "
    "AppleWebKit/537.36 (KHTML, like Gecko)"
)
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
TOTAL_TIMEOUT = 12


class BlockedAddressError(OSError):
    """Raised when a URL points at a non-public address."""


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class PublicOnlyResolver(AbstractResolver):
    """Resolve through the system resolver but drop non-public addresses."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
        results = []
        for fam, _type, _proto, _canon, sockaddr in infos:
            address = sockaddr[0]
            if not _is_public(address):
                continue
            results.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": fam,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise BlockedAddressError(f"{host} does not resolve to a public address")
        return results

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None


def validate_url(url: str) -> str:
    """Reject anything that is not a plain http(s) URL to a public host."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedAddressError("unsupported URL")
    if parts.username or parts.password:
        raise BlockedAddressError("credentials in URL are not allowed")
    host = parts.hostname
    if host.lower() in {"localhost", "localhost.localdomain"} or host.endswith(
        (".local", ".internal", ".localhost")
    ):
        raise BlockedAddressError("local hostnames are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return url  # a hostname: the resolver enforces the rest
    if not _is_public(host):
        raise BlockedAddressError("private addresses are not allowed")
    return url


class PageFetcher:
    """Owns one aiohttp session and fetches HTML with all safety limits."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                resolver=PublicOnlyResolver(), limit=10, ttl_dns_cache=300
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=TOTAL_TIMEOUT),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "en",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def fetch_html(self, url: str) -> tuple[str, str]:
        """Return (final_url, html). Raises on any failure; callers catch broadly."""
        session = self._get_session()
        current = validate_url(url)
        for _hop in range(MAX_REDIRECTS + 1):
            async with session.get(current, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise aiohttp.ClientError("redirect without Location")
                    current = validate_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message="unexpected status",
                    )
                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and "html" not in content_type and "xml" not in content_type:
                    raise aiohttp.ClientError(f"not an HTML page ({content_type})")
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) >= MAX_BYTES:
                        break
                charset = response.charset or "utf-8"
                try:
                    text = bytes(body).decode(charset, errors="replace")
                except LookupError:
                    text = bytes(body).decode("utf-8", errors="replace")
                return current, text
        raise aiohttp.ClientError("too many redirects")
