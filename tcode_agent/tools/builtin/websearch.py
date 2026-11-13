"""WebSearch tool backed by SearXNG or Brave Search."""

import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ... import websearch_settings

"""

docker run -it --rm -d -p 8888:8080 -v /srv/tcode/searxng/etc/:/etc/searxng/ -v /srv/tcode/searxng/data/:/var/cache/searxng/ -e "BASE_URL=http://localhost:8888/" -e "INSTANCE_NAME=tcode-searxng" docker.io/searxng/searxng:latest

"""


DEFAULT_SEARXNG_BASE = "http://127.0.0.1:8888"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 60
MAX_RESULTS = 20
MAX_CANDIDATES = 50
AUTO_SUPPLEMENTAL_CANDIDATES = 10
AVAILABLE_CATEGORIES = ("general", "videos", "images", "science", "news")


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


DEFAULT_RESULT_COUNT = _env_int("TCODE_WEBSEARCH_DEFAULT_COUNT", 10)
DEFAULT_CANDIDATE_COUNT = _env_int("TCODE_WEBSEARCH_DEFAULT_CANDIDATE_COUNT", 20)
DEFAULT_AUTO_SUPPLEMENTAL_COUNT = _env_int(
    "TCODE_WEBSEARCH_AUTO_SUPPLEMENTAL_COUNT", AUTO_SUPPLEMENTAL_CANDIDATES
)


class WebSearchParameters(ParameterSchema):
    """Parameters for websearch tool."""

    query: str
    count: int | None = DEFAULT_RESULT_COUNT
    candidate_count: int | None = DEFAULT_CANDIDATE_COUNT
    categories: str | None = "auto"
    language: str | None = "auto"
    time_range: str | None = ""
    timeout: int | None = DEFAULT_TIMEOUT


class WebSearchTool(Tool):
    """Search the web with SearXNG and return concise result list."""

    @property
    def id(self) -> str:
        return "websearch"

    @property
    def description(self) -> str:
        return """Search the web using the configured search provider.
Returns ranked results with title, URL and snippet.
With SearXNG, if categories is omitted or set to auto, the tool infers the primary category
from the query and uses a hybrid plan across general/videos/images/science/news.
Brave supports general web search only; use auto or general.
Use this when the task needs search engine retrieval by keywords."""

    @property
    def parameters(self) -> type:
        return WebSearchParameters

    def _resolve_base_url(self, context: ToolContext) -> str:
        if context.extra:
            configured = str(context.extra.get("searxng_base_url") or "").strip()
            if configured:
                return configured.rstrip("/")
        env_configured = str(os.environ.get("TCODE_SEARXNG_BASE_URL") or "").strip()
        if env_configured:
            return env_configured.rstrip("/")
        return DEFAULT_SEARXNG_BASE

    def _tokenize_query(self, query: str) -> list[str]:
        tokens = [item.strip().lower() for item in re.findall(r"[\w\u4e00-\u9fff]+", query)]
        return [token for token in tokens if token]

    def _normalize_categories(self, raw: str | None) -> list[str]:
        if not raw:
            return ["auto"]

        aliases = {
            "auto": "auto",
            "综合": "general",
            "general": "general",
            "web": "general",
            "视频": "videos",
            "video": "videos",
            "videos": "videos",
            "图片": "images",
            "图像": "images",
            "image": "images",
            "images": "images",
            "科学": "science",
            "科研": "science",
            "论文": "science",
            "science": "science",
            "news": "news",
            "新闻": "news",
            "资讯": "news",
        }

        parts = [
            item.strip().lower()
            for item in re.split(r"[,，/|+\s]+", str(raw or ""))
            if item.strip()
        ]
        normalized: list[str] = []
        for part in parts:
            mapped = aliases.get(part, part)
            if mapped == "auto":
                return ["auto"]
            if mapped in AVAILABLE_CATEGORIES and mapped not in normalized:
                normalized.append(mapped)
        return normalized or ["auto"]

    def _infer_primary_category(self, query: str) -> str:
        text = query.strip().lower()
        scores = {name: 0 for name in AVAILABLE_CATEGORIES}

        def boost(category: str, keywords: list[str], amount: int) -> None:
            for kw in keywords:
                if kw in text:
                    scores[category] += amount

        boost("news", ["新闻", "快讯", "动态", "update", "updates", "news", "headline", "breaking"], 6)
        if "最新" in text:
            scores["news"] += 2
        boost("images", ["图片", "图像", "配图", "结构图", "照片", "image", "images", "figure", "figures", "gallery"], 6)
        boost("videos", ["视频", "演示", "教程", "youtube", "bilibili", "watch", "video", "videos", "demo"], 6)
        boost(
            "science",
            [
                "科学",
                "科研",
                "研究",
                "进展",
                "研究进展",
                "科学进展",
                "药理",
                "机制",
                "活性",
                "靶点",
                "分子机制",
                "综述",
                "临床前",
                "体内",
                "体外",
                "论文",
                "paper",
                "papers",
                "research",
                "study",
                "studies",
                "pubmed",
                "arxiv",
                "biorxiv",
                "science",
                "nature",
                "doi",
                "journal",
                "preprint",
                "预印本",
            ],
            7,
        )
        boost("general", ["官网", "教程", "文档", "guide", "docs", "documentation"], 2)

        # 化合物/蛋白/药物类问题，如果同时带有科研表达，优先偏向 science。
        if any(token in text for token in ["素", "酸", "酶", "蛋白", "分子", "化合物", "药物", "daphnetin"]):
            if any(token in text for token in ["科学", "研究", "进展", "药理", "机制", "论文", "综述"]):
                scores["science"] += 8

        best = max(scores.items(), key=lambda item: item[1])
        return best[0] if best[1] > 0 else "general"

    def _build_category_plan(
        self,
        *,
        requested_categories: list[str],
        primary_candidates: int,
    ) -> tuple[list[tuple[str, int]], str]:
        if requested_categories and requested_categories != ["auto"]:
            if len(requested_categories) == 1:
                return [(requested_categories[0], primary_candidates)], requested_categories[0]

            total = max(primary_candidates, len(requested_categories))
            base = total // len(requested_categories)
            remainder = total % len(requested_categories)
            plan: list[tuple[str, int]] = []
            for idx, category in enumerate(requested_categories):
                limit = base + (1 if idx < remainder else 0)
                if limit > 0:
                    plan.append((category, limit))
            return plan, requested_categories[0]

        primary = self._infer_primary_category(requested_categories[0] if requested_categories and requested_categories[0] != "auto" else "")
        return [(primary, primary_candidates)], primary

    def _build_auto_category_plan(self, query: str, primary_candidates: int) -> tuple[list[tuple[str, int]], str]:
        primary = self._infer_primary_category(query)
        others = [item for item in AVAILABLE_CATEGORIES if item != primary]
        supplemental_total = max(0, min(MAX_CANDIDATES - primary_candidates, DEFAULT_AUTO_SUPPLEMENTAL_COUNT))
        plan: list[tuple[str, int]] = [(primary, primary_candidates)]
        if supplemental_total > 0 and others:
            base = supplemental_total // len(others)
            remainder = supplemental_total % len(others)
            for idx, category in enumerate(others):
                limit = base + (1 if idx < remainder else 0)
                if limit > 0:
                    plan.append((category, limit))
        return plan, primary

    def _score_result(
        self,
        query: str,
        query_tokens: list[str],
        item: dict[str, Any],
        position: int,
        primary_category: str,
    ) -> float:
        title = str(item.get("title") or "").strip().lower()
        content = str(item.get("content") or "").strip().lower()
        url = str(item.get("url") or "").strip().lower()
        domain = urlparse(url).netloc.lower()
        full_query = query.strip().lower()
        result_category = str(item.get("_category") or "").strip().lower()

        score = 0.0
        if full_query:
            if full_query in title:
                score += 60
            if full_query in content:
                score += 30
            if full_query in url:
                score += 20

        for token in query_tokens:
            if token in title:
                score += 8
            if token in content:
                score += 3
            if token and token in domain:
                score += 2

        if result_category == primary_category:
            score += 10
        elif result_category:
            score += 2

        # Keep original ranking as a soft tiebreaker.
        score += max(0.0, 5.0 - position * 0.1)
        return score

    async def _fetch_page(
        self,
        *,
        session: aiohttp.ClientSession,
        endpoint: str,
        params: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
        page: int,
        probe: bool = False,
    ) -> list[dict[str, Any]]:
        page_params = dict(params)
        page_params["pageno"] = page
        try:
            async with session.get(
                endpoint,
                params=page_params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                **({"allow_redirects": False} if probe else {}),
            ) as response:
                if probe and response.status != 200:
                    reason = {
                        401: "SearXNG 拒绝访问，请检查实例的认证设置。",
                        403: "SearXNG 拒绝搜索，请检查是否启用了 JSON 输出或访问限制。",
                        429: "SearXNG 请求过于频繁，请稍后再试。",
                    }.get(response.status, f"SearXNG 搜索返回 HTTP {response.status}。")
                    raise RuntimeError(reason)
                if not response.ok:
                    raise RuntimeError(f"SearXNG request failed: HTTP {response.status}")
                payload = await response.json(content_type=None)
        except Exception as exc:
            if probe:
                if isinstance(exc, TimeoutError):
                    raise RuntimeError("SearXNG 搜索请求超时。") from None
                if isinstance(exc, ValueError):
                    raise RuntimeError("SearXNG 未返回有效 JSON，请检查 JSON 输出设置。") from None
                if not isinstance(exc, RuntimeError):
                    raise RuntimeError("无法连接 SearXNG，请检查实例地址和后端网络。") from None
                raise
            raise RuntimeError(str(exc)) from exc

        if probe and (not isinstance(payload, dict) or not isinstance(payload.get("results"), list)):
            raise RuntimeError("SearXNG 响应缺少有效的搜索结果列表。")
        if not isinstance(payload, dict):
            return []
        results = payload.get("results", [])
        if not isinstance(results, list):
            return []
        return [item for item in results if isinstance(item, dict)]

    async def _search_brave(
        self, parsed: WebSearchParameters, query: str, count: int, timeout: int, key: str
    ) -> ToolResult:
        def fail(message: str) -> ToolResult:
            return ToolResult(title="websearch", output="", error=message)

        if not key:
            return fail("Brave Search requires TCODE_BRAVE_SEARCH_API_KEY.")
        categories = re.split(r"[,，/|+\s]+", (parsed.categories or "auto").strip().lower())
        if any(category not in {"", "auto", "general", "web", "综合"} for category in categories):
            return fail("Brave Search only supports general web search; use auto or general.")
        params = {"q": query, "count": count, "text_decorations": "false"}
        language = (parsed.language or "auto").strip().lower()
        if language not in {"", "auto", "all"}:
            params["search_lang"] = {"zh-cn": "zh-hans", "zh-tw": "zh-hant"}.get(
                language, language if language in {"zh-hans", "zh-hant"} else language.split("-")[0]
            )
        if parsed.time_range:
            freshness = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
            if parsed.time_range not in freshness:
                return fail("Brave Search time_range must be day, week, month or year.")
            params["freshness"] = freshness[parsed.time_range]
        # ponytail: one ranked page per call; add pagination only if needed.
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    BRAVE_ENDPOINT, params=params,
                    headers={"Accept": "application/json", "X-Subscription-Token": key},
                    timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        return fail("Brave Search authentication or access denied; check your key and plan.")
                    if response.status == 429:
                        return fail("Brave Search rate limit or quota exceeded.")
                    if response.status != 200:
                        return fail(f"Brave Search request failed: HTTP {response.status}.")
                    payload = await response.json(content_type=None)
        except TimeoutError:
            return fail("Brave Search request timed out.")
        except (aiohttp.ClientError, ValueError):
            return fail("Brave Search connection failed or returned an invalid response.")

        web = payload.get("web", {}) if isinstance(payload, dict) else None
        items = web.get("results", []) if isinstance(web, dict) else None
        if not isinstance(items, list):
            return fail("Brave Search returned an invalid response.")
        results = [
            {"title": item.get("title"), "url": item["url"], "content": item.get("description")}
            for item in items if isinstance(item, dict) and item.get("url")
        ]
        return self._format_results(query, count, results, len(results), count,
                                    [("general", count)], "general", BRAVE_ENDPOINT, False)

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = WebSearchParameters(**args)
        query = parsed.query.strip()
        if not query:
            return ToolResult(title="websearch", output="", error="query is required")

        count = max(1, min(MAX_RESULTS, int(parsed.count or DEFAULT_RESULT_COUNT)))
        primary_candidate_count = max(
            count,
            min(MAX_CANDIDATES, int(parsed.candidate_count or max(count, DEFAULT_CANDIDATE_COUNT))),
        )
        timeout = max(1, min(MAX_TIMEOUT, int(parsed.timeout or DEFAULT_TIMEOUT)))
        try:
            settings = websearch_settings.read_settings()
        except (OSError, ValueError):
            return ToolResult(title="websearch", output="", error="Cannot read web search settings.")
        provider = websearch_settings.provider(settings)
        if provider == "brave":
            return await self._search_brave(parsed, query, count, timeout, websearch_settings.brave_key(settings))
        if provider != "searxng":
            return ToolResult(title="websearch", output="", error="Select searxng or brave in TCODE_WEBSEARCH_PROVIDER.")
        base_url = (settings.get("searxng_url") or self._resolve_base_url(context)).rstrip("/")
        endpoint = urljoin(base_url + "/", "search")
        requested_categories = self._normalize_categories(parsed.categories)
        if requested_categories == ["auto"]:
            category_plan, primary_category = self._build_auto_category_plan(query, primary_candidate_count)
        else:
            category_plan, primary_category = self._build_category_plan(
                requested_categories=requested_categories,
                primary_candidates=primary_candidate_count,
            )

        headers = {
            "Accept": "application/json",
            "User-Agent": "tcode-agent-websearch/1.0",
        }

        try:
            async with aiohttp.ClientSession() as session:
                results: list[dict[str, Any]] = []
                seen_keys: set[str] = set()
                for category, target_limit in category_plan:
                    params = {
                        "q": query,
                        "format": "json",
                        "categories": category,
                        "language": parsed.language or "auto",
                        # Some SearXNG deployments may honor one of these knobs; unsupported keys are ignored.
                        "count": target_limit,
                        "limit": target_limit,
                    }
                    if parsed.time_range:
                        params["time_range"] = parsed.time_range

                    max_pages = max(1, min(5, (target_limit + max(1, count) - 1) // max(1, count)))
                    collected_for_category = 0
                    for page in range(1, max_pages + 1):
                        page_results = await self._fetch_page(
                            session=session,
                            endpoint=endpoint,
                            params=params,
                            headers=headers,
                            timeout=timeout,
                            page=page,
                        )
                        if not page_results:
                            break
                        new_items = 0
                        for item in page_results:
                            url = str(item.get("url") or "").strip()
                            title = str(item.get("title") or "").strip()
                            key = url or title
                            if not key or key in seen_keys:
                                continue
                            seen_keys.add(key)
                            results.append({**item, "_category": category})
                            new_items += 1
                            collected_for_category += 1
                            if collected_for_category >= target_limit:
                                break
                        if collected_for_category >= target_limit or new_items == 0:
                            break
        except Exception as exc:
            return ToolResult(
                title="websearch",
                output="",
                error=f"SearXNG request error: {exc}",
            )

        query_tokens = self._tokenize_query(query)
        ranked = sorted(
            [
                {
                    **item,
                    "_score": self._score_result(query, query_tokens, item, idx, primary_category),
                    "_position": idx,
                }
                for idx, item in enumerate(results)
            ],
            key=lambda item: (float(item.get("_score") or 0.0), -int(item.get("_position") or 0)),
            reverse=True,
        )

        return self._format_results(query, count, ranked, len(results), primary_candidate_count,
                                    category_plan, primary_category, endpoint, True)

    def _format_results(
        self, query: str, count: int, ranked: list[dict[str, Any]], fetched_count: int,
        primary_candidate_count: int, category_plan: list[tuple[str, int]],
        primary_category: str, endpoint: str, reranked: bool,
    ) -> ToolResult:
        if not ranked:
            return ToolResult(
                title=f"Search: {query}", output=f"No results for query: {query}",
                metadata={"query": query, "count": 0, "endpoint": endpoint},
            )
        plan_text = ", ".join(f"{name}:{limit}" for name, limit in category_plan)
        lines = [
            f"Search results for: {query}",
            f"Primary category: {primary_category}",
            f"Search plan: {plan_text}",
            "",
        ]
        selected = ranked[:count]
        for idx, item in enumerate(selected, 1):
            title = str(item.get("title") or "(no title)").strip()
            url = str(item.get("url") or "").strip()
            content = str(item.get("content") or "").strip()
            category = str(item.get("_category") or primary_category).strip()
            lines.append(f"{idx}. [{category}] {title}")
            if url:
                lines.append(f"   {url}")
            if content:
                snippet = content.replace("\n", " ").strip()
                if len(snippet) > 240:
                    snippet = snippet[:240] + "..."
                lines.append(f"   {snippet}")
            lines.append("")

        return ToolResult(
            title=f"Search: {query}",
            output="\n".join(lines).strip(),
            metadata={
                "query": query,
                "count": len(selected),
                "candidate_count": primary_candidate_count,
                "category_plan": [{"category": name, "limit": limit} for name, limit in category_plan],
                "primary_category": primary_category,
                "fetched_count": fetched_count,
                "endpoint": endpoint,
                "reranked": reranked,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
