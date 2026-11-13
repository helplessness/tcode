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
import json
import re
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal
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
DOCUMENT_PAGE_CHARS = 8000


@dataclass
class _SavedDocument:
    expires: float
    url: str
    content_type: str
    format: str
    content: str
    output: str
    blocks: tuple[tuple[str, str], ...] | None = None

    @property
    def page_chars(self) -> int:
        # Preserve existing inline JSON consumers within the old limit.
        return 50_000 if self.content_type.endswith(("json", "+json")) else DOCUMENT_PAGE_CHARS


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

    url: str = ""
    format: str | None = "markdown"  # "text", "markdown", or "html"
    timeout: int | None = 30
    view: Literal["sections", "evidence"] | None = None
    section: str = pydantic.Field(default="", max_length=256)
    query: str = pydantic.Field(default="", max_length=256)
    offset: int = pydantic.Field(default=0, ge=0)
    document_id: str = pydantic.Field(default="", max_length=32)


def _paper_blocks(content: str, content_type: str, markdown) -> tuple[tuple[str, str], ...]:
    if content_type.endswith("xml") or content.lstrip().startswith("<?xml"):
        # JATS can carry an external DOCTYPE; ElementTree does not load it.
        # Reject entity declarations before parsing, including internal entities.
        if re.search(r"<!ENTITY\b", content, re.I):
            raise ValueError("Document contains unsupported XML entity declarations")
        root = ET.fromstring(content)
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        article = root if root.tag == "article" else root.find(".//article")
        if article is None:
            raise ValueError("No article body found in XML")
        def walk(node, path=""):
            if node.tag in {"ref-list", "supplementary-material", "sub-article"}:
                return
            title = node.find("title") if node.tag in {"sec", "abstract", "caption"} else None
            if title is not None or node.tag in {"abstract", "caption"}:
                label = (" ".join("".join(title.itertext()).split()) if title is not None
                         else "Abstract" if node.tag == "abstract" else "Figure/Table caption")
                if label:
                    path = f"{path} / {label}" if path else label
                    yield path, ""
            if node.tag in {"article-title", "p", "tr", "th", "td"}:
                separator = " " if node.tag == "tr" else ""
                value = " ".join(separator.join(node.itertext()).split())
                if value:
                    yield (value, "") if node.tag == "article-title" else (path, value)
                return
            for child in node:
                if child is not title:
                    yield from walk(child, path)
        return tuple(walk(article))
    if "html" in content_type:
        content = markdown(content)
    headings = []
    rows = []
    for part in re.split(r"\n\s*\n|\n(?=#+\s)", content):
        if not (part := part.strip()):
            continue
        match = re.match(r"^(#{1,6})[ \t]+([^\n]+)(?:\n([\s\S]*))?$", part)
        if match:
            level = len(match[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, match[2].strip()))
            rows.append((" / ".join(label for _, label in headings), ""))
            part = (match[3] or "").strip()
        if part:
            rows.append((" / ".join(label for _, label in headings), part))
    return tuple(rows)


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

    def __init__(self):
        self._documents = OrderedDict()

    @property
    def id(self) -> str:
        return "webfetch"

    @property
    def description(self) -> str:
        return """Fetch content from a web URL. Supports text, markdown, and html formats.
Can handle images (returned as attachments) and convert HTML to Markdown.
For long papers/HTML use view="sections" to list headings, or view="evidence" with section and/or query.
Use a short literal term for query; without a query, read the selected section in order.
On a keyword miss, the tool returns the first page of the same section, clearly marked as unfiltered.
Long responses automatically return a bounded preview and document_id. Use document_id
instead of url to read more without downloading again: use offset=next_offset for the
same view, or switch to view="sections"/"evidence" with offset=0. The default view uses
character offsets; section/evidence views use row offsets. A preview is not the full source."""

    @property
    def parameters(self) -> type:
        return WebFetchParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = WebFetchParameters(**args)
        url = parsed.url
        fmt = parsed.format or "markdown"
        timeout = min(parsed.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)
        extra = context.extra or {}
        scope = (context.session_id, *(str(extra.get(name) or "") for name in ("tenant_id", "user_id", "workspace_id", "workdir")))

        if parsed.document_id:
            key = (scope, parsed.document_id)
            cached = self._documents.get(key)
            if not context.session_id or cached is None or cached.expires <= time.monotonic():
                return ToolResult(title="webfetch", output="", error="Saved web document is unavailable in this session or expired. Fetch its original URL explicitly to start a new snapshot.")
            if url and url != cached.url:
                return ToolResult(title="webfetch", output="", error="URL does not match the saved web document.")
            self._documents.move_to_end(key)
            return self._saved_view(parsed, cached, parsed.document_id)

        try:
            _validate_public_url(url)
        except ValueError as exc:
            return ToolResult(
                title=url,
                output="",
                error=str(exc),
            )

        if parsed.view and context.session_id:
            for key in reversed(self._documents):
                cached = self._documents[key]
                if key[0] == scope and cached.url == url and cached.expires > time.monotonic():
                    self._documents.move_to_end(key)
                    return self._saved_view(parsed, cached, key[1])
        if parsed.offset and not parsed.view:
            return ToolResult(title=url, output="", error="Continuing a web document requires its document_id.")

        # Build Accept header based on format
        accept_headers = {
            "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
            "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
            "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1",
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": accept_headers.get("markdown" if parsed.view else fmt, "*/*"),
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

                    output = await self._format_content(content, content_type, fmt)
                    document = _SavedDocument(time.monotonic() + 900, url, content_type, fmt, content, output)
                    document_id = ""
                    if context.session_id and (parsed.view or len(output) > document.page_chars):
                        document_id = uuid.uuid4().hex
                        self._documents[(scope, document_id)] = document
                        if len(self._documents) > 4:
                            self._documents.popitem(last=False)
                    return self._saved_view(parsed, document, document_id)

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

    def _saved_view(self, parsed: WebFetchParameters, document: _SavedDocument, document_id: str) -> ToolResult:
        notice = ""
        if parsed.view:
            try:
                if document.blocks is None:
                    document.blocks = _paper_blocks(document.content, document.content_type, self._html_to_markdown)
                result = self._document_view(parsed, document.blocks)
                if parsed.view == "evidence" and parsed.query and not result.metadata["total_rows"]:
                    parsed = parsed.model_copy(update={"query": "", "offset": 0})
                    result = self._document_view(parsed, document.blocks)
                    result.metadata["keyword_match"] = False
                    notice = "No literal keyword match; this does not establish absence. "
                    if result.metadata["total_rows"]:
                        notice += "Showing the start of the same section without keyword filtering. "
                    else:
                        notice += "No readable paragraphs in the requested section; check the sections view. "
            except (ValueError, ET.ParseError, RecursionError) as exc:
                recovery = f' Use the default view with document_id="{document_id}" to read the original text.' if document_id else ""
                return ToolResult(title=document.url, output="", error=f"Cannot extract document sections: {exc}.{recovery}")
            result.title = document.url
        else:
            output = document.output
            if parsed.offset > len(output):
                return ToolResult(title=document.url, output="", error="Offset exceeds the saved document length.")
            end = min(parsed.offset + document.page_chars, len(output))
            more = end < len(output)
            result = ToolResult(title=f"{document.url} ({document.content_type})", output=output[parsed.offset:end],
                                metadata={"contentType": document.content_type, "format": document.format,
                                          "truncated": more, "originalSize": len(document.content),
                                          "next_offset": end if more else None})
        if document_id:
            result.metadata["document_id"] = document_id
        offset = result.metadata.get("next_offset")
        if offset is not None:
            result.truncated = True
            if document_id:
                args = {"document_id": document_id, "offset": offset}
                if parsed.view:
                    args.update(view=parsed.view)
                    if parsed.view == "evidence":
                        args.update(section=parsed.section, query=parsed.query)
                unit = "rows" if parsed.view else "characters"
                notice += (f"Preview only ({unit}). Continue with {json.dumps(args, ensure_ascii=False)}. "
                          "When changing view, section or query, reset offset to 0.")
            else:
                notice += "Preview only. No saved document is available outside a session."
        elif parsed.view and parsed.offset and parsed.offset >= result.metadata["total_rows"]:
            notice = ("Offset is outside this view's rows. Raw body offsets count characters; "
                      "section and evidence offsets count rows. Reset offset to 0 for this view/filter.")
        if notice:
            result.metadata["continuation_notice"] = notice
            result.output = f"[webfetch: {notice}]\n\n" + result.output
        return result

    @staticmethod
    def _document_view(parsed: WebFetchParameters, blocks: tuple[tuple[str, str], ...]) -> ToolResult:
        rows = []
        for heading, block in blocks:
            if not block:
                if parsed.view == "sections":
                    rows.append(heading)
                continue
            if parsed.view == "evidence" and parsed.section.casefold() in heading.casefold():
                match = block.casefold().find(parsed.query.casefold())
                if match >= 0:
                    prefix = f"[{heading}]\n" if heading else ""
                    # Keep the match visible even inside a very large paragraph.
                    start = max(0, match - 400)
                    if parsed.query:
                        end = min(len(block), start + 2000)
                        rows.append(prefix + ("…" if start else "") + block[start:end] + ("…" if end < len(block) else ""))
                    else:
                        rows.extend(prefix + block[i:i+2000] for i in range(0, len(block), 2000))
        # Paginate by rows, keeping each model result bounded.
        if parsed.view == "sections":
            rows = list(dict.fromkeys(rows))
        output = []
        size = 0
        for row in rows[parsed.offset:]:
            row = row[:2400]
            if size + len(row) + 2 > 8000:
                break
            output.append(row)
            size += len(row) + 2
        next_offset = parsed.offset + len(output)
        more = next_offset < len(rows)
        body = "\n\n".join(output) or "No matching headings or evidence paragraphs."
        return ToolResult(title=parsed.url, output=body,
                          metadata={"total_rows": len(rows), "next_offset": next_offset if more else None})

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
