"""Restrict outbound requests originating from the browser model manager."""

import asyncio
import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit

import httpx


def loopback_host(host: str) -> bool:
    return host.lower() in {"localhost", "127.0.0.1", "::1"}


def local_web_binding() -> bool:
    return os.environ.get("TCODE_WEB_MODEL_LOOPBACK") == "1"


def allow_local_models() -> bool:
    from ..server.auth import auth_required
    return local_web_binding() and not auth_required()


def validate_base_url(value: str, *, allow_loopback: bool) -> str:
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
        if (len(value) > 1024 or not host or re.search(r"[\s\\%?#\x00-\x1f\x7f]", value)
                or parts.username is not None or parts.password is not None
                or (port is not None and port == 0)):
            raise ValueError()
        if loopback_host(host):
            if not allow_loopback or parts.scheme not in {"https", "http"}:
                raise ValueError()
        elif parts.scheme != "https":
            raise ValueError()
        else:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if (re.fullmatch(r"[0-9.]+", host) or "." not in host.rstrip(".")
                        or host.rstrip(".").endswith((".local", ".internal", ".localhost"))):
                    raise ValueError() from None
            else:
                if not _public(address):
                    raise ValueError()
    except ValueError:
        raise ValueError("请输入公网 HTTPS 模型地址；本机模式可用回环 HTTP 地址，不允许内网或带凭据的地址") from None
    return value


def _public(address) -> bool:
    return address.is_global and not (address.is_multicast or address.is_reserved)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, allow_loopback: bool, probe: bool, trusted_config: bool):
        super().__init__(trust_env=False)
        self.allow_loopback = allow_loopback
        self.probe = probe
        self.trusted_config = trusted_config

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        outbound = request if self.trusted_config else await self._pin(request)
        response = await super().handle_async_request(outbound)
        if self.probe:
            body = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 512 * 1024:
                        raise httpx.ReadError("测试响应超过大小限制", request=request)
                    body.extend(chunk)
            finally:
                await response.aclose()
            headers = {k: v for k, v in response.headers.items() if k not in {"content-encoding", "content-length"}}
            return httpx.Response(response.status_code, headers=headers, content=bytes(body), request=request)
        return response

    async def _pin(self, request: httpx.Request) -> httpx.Request:
        host = request.url.host
        try:
            validate_base_url(str(request.url.copy_with(query=None, fragment=None)), allow_loopback=self.allow_loopback)
            local = loopback_host(host)
            port = request.url.port or (443 if request.url.scheme == "https" else 80)
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5,
            )
            addresses = list(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in infos))
            if not addresses or any(not (ip.is_loopback if local else _public(ip)) for ip in addresses):
                raise ValueError()
        except (OSError, ValueError, TimeoutError):
            raise httpx.ConnectError("模型地址解析失败或指向未允许的网络", request=request) from None
        # Connect to the vetted address, without a second DNS lookup, but keep
        # the configured HTTP Host and TLS certificate/SNI name. Use a separate
        # pool per origin so different hostnames on one CDN IP cannot share TLS.
        headers = httpx.Headers(request.headers)
        headers["host"] = request.url.netloc.decode("ascii")
        address = next((ip for ip in addresses if ip.version == 4), addresses[0])
        return httpx.Request(
            request.method, request.url.copy_with(host=str(address)),
            headers=headers,
            stream=request.stream, extensions={**request.extensions, "sni_hostname": host},
        )


def guarded_client(*, allow_loopback: bool, probe: bool = False, trusted_config: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_PinnedTransport(allow_loopback=allow_loopback, probe=probe, trusted_config=trusted_config),
        trust_env=False, follow_redirects=False,
    )
