"""WebFetch tool - Fetch content from URLs (like original tcode)

Features:
- Fetch web pages and return as text/markdown/html
- Convert HTML to Markdown
- Handle images (return as attachments)
- Cloudflare bot detection workaround
- Configurable timeout and size limits
"""

import asyncio
import base64
import ipaddress
import re
import socket
from typing import Any, Optional
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp
try:
    import markdownify
except Exception:  # pragma: no cover - optional dependency fallback
    markdownify = None

import pydantic

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult


# Constants from original tcode
MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5MB
DEFAULT_TIMEOUT = 30  # 30 seconds
MAX_TIMEOUT = 120  # 2 minutes


def _validate_public_url(url: str) -> None:
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must start with http:// or https://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is invalid")

    hostname = parsed.hostname.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
        or hostname == "metadata.google.internal"
    ):
        raise ValueError("Private or local network URLs are not allowed")
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Private or local network URLs are not allowed")


class _PublicNetworkResolver(aiohttp.abc.AbstractResolver):
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host,
            port,
            family=family,
            type=socket.SOCK_STREAM,
        )
        resolved: list[dict[str, Any]] = []
        for resolved_family, socktype, protocol, _canonname, sockaddr in infos:
            address = str(sockaddr[0]).split("%", 1)[0]
            if not ipaddress.ip_address(address).is_global:
                raise OSError("Private or local network URLs are not allowed")
            resolved.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": int(sockaddr[1]),
                    "family": resolved_family,
                    "proto": protocol,
                    "flags": 0,
                }
            )
        if not resolved:
            raise OSError(f"Unable to resolve public host: {host}")
        return resolved

    async def close(self) -> None:
        return None


class WebFetchParameters(ParameterSchema):
    """Parameters for webfetch tool"""

    url: str
    format: str | None = "markdown"  # "text", "markdown", or "html"
    timeout: int | None = 30


class HTMLTextExtractor(HTMLParser):
    """Extract text from HTML, skipping script/style tags"""

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.skip_tags = {"script", "style", "noscript", "iframe", "object", "embed"}
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.skip_tags:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.skip_tags and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.skip_depth == 0:
            self.text_parts.append(data)

    def get_text(self) -> str:
        return " ".join(self.text_parts).strip()


class WebFetchTool(Tool):
    """Fetch content from a web URL with format conversion"""

    @property
    def id(self) -> str:
        return "webfetch"

    @property
    def description(self) -> str:
        return """Fetch content from a web URL. Supports text, markdown, and html formats.
Can handle images (returned as attachments) and convert HTML to Markdown.
Useful for retrieving documentation, API responses, or other web content."""

    @property
    def parameters(self) -> type:
        return WebFetchParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = WebFetchParameters(**args)
        url = parsed.url
        fmt = parsed.format or "markdown"
        timeout = min(parsed.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)

        try:
            _validate_public_url(url)
        except ValueError as exc:
            return ToolResult(
                title=url,
                output="",
                error=str(exc),
            )

        # Build Accept header based on format
        accept_headers = {
            "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
            "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
            "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1",
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": accept_headers.get(fmt, "*/*"),
            "Accept-Language": "en-US,en;q=0.9",
        }

        try:
            trace_config = aiohttp.TraceConfig()

            async def validate_request_url(
                _session: aiohttp.ClientSession,
                _context: Any,
                params: aiohttp.TraceRequestStartParams,
            ) -> None:
                _validate_public_url(str(params.url))

            trace_config.on_request_start.append(validate_request_url)
            connector = aiohttp.TCPConnector(
                resolver=_PublicNetworkResolver(),
                use_dns_cache=True,
            )
            async with aiohttp.ClientSession(
                connector=connector,
                trace_configs=[trace_config],
            ) as session:
                # First request
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    # Check for Cloudflare bot detection
                    if (
                        response.status == 403
                        and response.headers.get("cf-mitigated") == "challenge"
                    ):
                        # Retry with different User-Agent
                        headers["User-Agent"] = "tcode"
                        async with session.get(
                            url,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as response:
                            pass
                    else:
                        pass  # response already set

                    if not response.ok:
                        return ToolResult(
                            title=url,
                            output="",
                            error=f"Request failed with status code: {response.status}",
                        )

                    # Check content length
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_RESPONSE_SIZE:
                        return ToolResult(
                            title=url,
                            output="",
                            error="Response too large (exceeds 5MB limit)",
                        )

                    # Read content
                    content_bytes = await response.read()

                    if len(content_bytes) > MAX_RESPONSE_SIZE:
                        return ToolResult(
                            title=url,
                            output="",
                            error="Response too large (exceeds 5MB limit)",
                        )

                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    title = f"{url} ({content_type})"

                    # Check if image
                    is_image = content_type.startswith(
                        "image/"
                    ) and content_type not in [
                        "image/svg+xml",
                        "image/vnd.fastbidsheet",
                    ]

                    if is_image:
                        # Return as attachment
                        base64_content = base64.b64encode(content_bytes).decode("utf-8")
                        return ToolResult(
                            title=title,
                            output="Image fetched successfully",
                            metadata={
                                "contentType": content_type,
                                "size": len(content_bytes),
                            },
                            attachments=[
                                {
                                    "type": "file",
                                    "mime": content_type,
                                    "url": f"data:{content_type};base64,{base64_content}",
                                }
                            ]
                            if hasattr(context, "attachments")
                            else [],
                        )

                    # Decode text content
                    try:
                        content = content_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        content = content_bytes.decode("latin-1")

                    # Handle based on format
                    output = await self._format_content(content, content_type, fmt)

                    # Truncate if too large
                    truncated = False
                    if len(output) > 50000:
                        output = output[:50000]
                        truncated = True

                    return ToolResult(
                        title=title,
                        output=output,
                        metadata={
                            "contentType": content_type,
                            "format": fmt,
                            "truncated": truncated,
                            "originalSize": len(content),
                        },
                    )

        except asyncio.TimeoutError:
            return ToolResult(
                title=url,
                output="",
                error=f"Request timed out after {timeout} seconds",
            )
        except aiohttp.ClientError as e:
            return ToolResult(
                title=url,
                output="",
                error=f"Network error: {str(e)}",
            )
        except Exception as e:
            return ToolResult(
                title=url,
                output="",
                error=str(e),
            )

    async def _format_content(self, content: str, content_type: str, fmt: str) -> str:
        """Format content based on requested format"""

        if fmt == "html":
            return content

        if "text/html" in content_type:
            if fmt == "markdown":
                return self._html_to_markdown(content)
            elif fmt == "text":
                return self._extract_text(content)

        # Return as-is for non-HTML or matching format
        return content

    def _html_to_markdown(self, html: str) -> str:
        """Convert HTML to Markdown using markdownify"""
        if markdownify is None:
            return self._extract_text(html)
        try:
            md = markdownify.markdownify(
                html,
                heading_style="ATX",
                hr_style="HR",
                bullets="-",
                code_language="python",
            )
            return md.strip()
        except Exception:
            # Fallback to text extraction
            return self._extract_text(html)

    def _extract_text(self, html: str) -> str:
        """Extract plain text from HTML"""
        try:
            extractor = HTMLTextExtractor()
            extractor.feed(html)
            return extractor.get_text()
        except Exception:
            # Simple fallback
            text = re.sub(r"<[^>]+>", "", html)
            text = re.sub(r"\s+", " ", text).strip()
            return text

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
