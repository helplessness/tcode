---
name: paper-literature-search
description: 通过统一 paper-search MCP 检索并合并 PubMed、ArXiv、Crossref、Semantic Scholar、OpenAlex 文献，通过统一 paper-download MCP 级联下载 PDF。当用户要求查文献、找 PMID/DOI、获取全文或下载论文时使用。
routing:
  domains: [literature]
  intents: [literature-search, paper-identifier-lookup, paper-download-planning]
  aliases: [查文献, 检索论文, 找论文, PMID, DOI, PubMed, arXiv, 下载论文 PDF, 下载 PDF]
  positive_examples:
    - 查找相关论文并下载 PDF
    - 根据标题定位 DOI 和 PMID
    - 在 PubMed 和 Semantic Scholar 检索候选论文
  negative_examples: [读取本地 PDF, 提取本地 PDF 表格, 合并 PDF]
  priority: 75
compatibility:
  python: "python3"
  dependencies:
    - python>=3.10
  environment: "优先使用统一 paper MCP；bundled script 仅作为检索服务不可用时的诊断回退"
---

# 文献检索技能

## 核心原则
- 检索优先调用 `paper_literature_search` plugin tool；直接调用 MCP 时只使用 `mcp_paper-search-mcp_*`。
- 需要把 PDF 保存到当前 TCode 工作区时，必须调用 `paper_literature_search(download_pdf_files=true)`：它使用 `paper-download-mcp.resolve_paper_pdf` 解析 URL，再由 TCode 在当前工作区写文件。不要直接让远端 MCP 写客户端绝对路径。
- 不直接调用或选择 PubMed、Crossref、Semantic Scholar、OpenAlex、ArXiv 的底层工具。
- 单个 provider 失败不是整个下载失败；以统一下载 MCP 返回的 `attempts` 和最终 `ok` 为准。
- 只有本地 `.pdf` 文件落地并通过 `scripts/validate_downloaded_pdf.py` 校验才算下载成功。
- 只有统一下载 MCP 已穷尽 provider 后，才允许调用 `task(subagent_type="agent-browser")` 作为最后兜底。
- 下载阶段禁止使用 `webfetch` 抓取 PDF；`webfetch` 只能用于查看网页文本，不可作为 PDF 下载工具。
- 下载阶段默认也不要用 `curl` 直接落地论文 PDF，除非用户明确要求按直链抓取并接受可能的站点拦截页风险。

## 适用场景
当用户提出以下需求时使用：
- 检索某个主题的论文
- 查找 PMID、DOI、标题、摘要、作者、年份
- 生成候选论文列表
- 指定只查某个来源，例如 PubMed 或 ArXiv
- 需要跨多个公开数据源合并、去重和补全标识符

## 工作流程
1. 明确检索问题，必要时收窄主题。
2. 选择合适来源：
   - 生物医学优先 `pubmed`
   - 预印本优先 `arxiv`
   - DOI/出版信息补充优先 `crossref`
  - 开放学术索引与 OA 候选补充可加 `openalex`
   - 泛学术召回可加 `semanticscholar`
3. 调用 `paper_literature_search` 或统一 `paper-search-mcp.search_papers`；不要手写各来源请求。
4. 统一检索 MCP 不可用时，才运行 bundled script 并用 `--output` 保存 JSON 作为诊断回退。
5. 向用户返回结构化结果：至少包含来源、标题、年份、URL，以及可用时的 PMID/DOI。
6. 若结果包含 `pmc_id`、`pmc_url`、`pdf_url_status`、`pdf_note`，应一并解释，不要把 `pdf_url` 误说成“已验证可下载”。
7. 如果用户要求落地下载，调用 `paper_literature_search(download_pdf_files=true)`；仅在统一解析和 TCode 本地下载都失败后，才委派一次浏览器子代理。

## 浏览器最终兜底
- 统一下载 MCP 失败后，当前主代理才可以调用 `task` 并设置 `subagent_type="agent-browser"`。`agent-browser` 是绑定到 `browser` 专用代理的 skill，当前主代理不得直接加载它。
- 尤其在以下情形，必须委派 `agent-browser` 子代理：
  - `pdf_url_status` 为 `candidate_unverified` 或 `unavailable`
  - `url` 是期刊落地页而不是 `.pdf` 直链
  - 用户明确反馈“链接打开后还要点 Download 才能下”
  - 需要把远程 PDF 真正落地到当前工作目录、临时目录或用户指定目录
- 不要先用 `webfetch` 代替浏览器子代理探测下载按钮；真正进入下载阶段后直接委派，不能继续用 `webfetch` 或 `curl` 伪装成下载成功。
- 推荐委派调用（将占位符替换为实际值）：
```json
{
  "description": "通过浏览器下载论文 PDF",
  "prompt": "使用 browser 工具打开 <文章落地页或候选链接>，把目标论文 PDF 下载到工作区路径 <目标.pdf>。必要时识别并点击 PDF/Download PDF 按钮，等待下载完成。不要登录、绕过验证码或付费墙。返回下载文件路径、最终 URL 和失败原因。",
  "subagent_type": "agent-browser",
  "allowed_tools": ["browser"]
}
```
- 推荐下载步骤：
  1. 当前主代理先确定文章落地页或候选链接，以及工作区内以 `.pdf` 结尾的目标输出路径。
  2. 当前主代理按上面的参数调用 `task`；不要调用 `skill(name="agent-browser")`，也不要自己调用 `browser`。
  3. 浏览器子代理使用原生 `browser` 工具打开页面、识别下载入口并把文件下载到指定目标路径，然后返回下载路径、最终 URL 与失败原因。
  4. 子代理返回后，当前主代理确认文件位于工作区；如需复制或重命名，整理到当前目录中的目标 `.pdf` 文件。
  5. 当前主代理立刻运行：`python3 .claude/skills/paper-literature-search/scripts/validate_downloaded_pdf.py <当前目录中的 .pdf 文件路径>`。
  6. 仅当校验输出 `ok: true` 时，才返回实际下载结果（文件名、本地路径、文件大小、校验结果）。
- 若站点需要登录、验证码或机构权限，必须明确告知用户这是站点访问限制，不要伪称“下载成功”。

## PDF 下载校验
- 下载后必须运行 [scripts/validate_downloaded_pdf.py](./scripts/validate_downloaded_pdf.py)。
- 校验至少覆盖以下条件：
  - 文件路径以 `.pdf` 结尾
  - 文件存在且大小大于 0
  - 前 1024 字节内出现 `%PDF-`
  - 末尾 64 KB 内出现 `startxref` 和 `%%EOF`
  - 文件头不包含 `<html`、`<!doctype`、`Access Denied`、`Just a moment`、`cf-browser-verification` 等 HTML/拦截页标记
- 任何一项不满足，都必须判定为下载失败，而不是“PDF 可能还能打开”。
- 如果 `file <path>` 或 `qpdf --check <path>` 可用，建议额外执行；但即便这些外部命令不可用，也必须先跑 skill 自带校验脚本。

## 推荐命令
```bash
cd /path/to/tcode
python3 .claude/skills/paper-literature-search/scripts/search_literature.py \
  --query "CRISPR Cas9" \
  --sources pubmed,crossref,openalex \
  --max-results 5 \
  --output /tmp/paper-search.json
```

```bash
cd /path/to/tcode
python3 .claude/skills/paper-literature-search/scripts/search_literature.py \
  --query "protein language model enzyme engineering" \
  --sources pubmed,arxiv,semanticscholar,openalex \
  --start-year 2021 \
  --max-results 8 \
  --format markdown
```

## 参数说明
- `--query`: 检索词，必填。
- `--sources`: 逗号分隔的数据源列表，可选值：`pubmed,arxiv,crossref,semanticscholar,openalex`。
- `--max-results`: 总体返回上限，默认 10。
- `--start-year`: 起始年份过滤。
- `--end-year`: 结束年份过滤。
- `--format`: `json` 或 `markdown`，默认 `json`。
- `--output`: 可选输出文件路径。

## 输出约定
JSON 输出字段包括：
- `ok`: 是否至少有一个 source 成功完成检索
- `has_results`: 是否有命中文献
- `query`: 原始查询
- `sources`: 实际检索的数据源
- `attempts`: 各 source 的执行状态
- `results`: 归一化后的结果列表

每条 `results` 记录至少包含：
- `source`
- `source_id`
- `title`
- `year`
- `authors`
- `journal`
- `doi`
- `url`
- `abstract`

可选字段：
- `pmc_id`
- `pmc_url`
- `pdf_url`
- `pdf_url_status`
- `pdf_note`

## 结果使用要求
- 若用户只要一个结果，优先返回最相关且元数据最完整的一篇。
- 若用户要求 PMID/DOI，优先选择含这两个字段的记录；缺失时明确标注。
- 若某个 source 失败，不要立即终止；优先继续使用其他 source。
- 如果所有 source 都失败，再向用户报告失败原因。
- 若 `pubmed` 结果带 `pmc_id`，优先把它当作后续全文下载的稳定标识符，而不是现场拼接 `/pdf/` 直链。
- 若 `semanticscholar` 返回 `pdf_url_status=candidate_unverified`，要明确这是候选开放获取链接，不等于已验证直链。
- 若用户继续追问“帮我下载 PDF”，默认调用 `paper_literature_search(download_pdf_files=true)`；只有统一解析和本地下载失败后才委派浏览器。
- 若下载后文件无法打开，或校验脚本报告 `html_markers_detected`、`missing_pdf_header`、`missing_startxref`、`missing_eof_marker` 等失败原因，必须判定为“下载失败/命中反爬或权限页”，不要伪称 PDF 已成功下载。

## 禁止事项
- 不要调用任何底层来源 MCP；只允许 `mcp_paper-search-mcp_*` 和 `mcp_paper-download-mcp_*`。
- 不要把 skill 退化成“给用户几个搜索建议”。要直接执行检索脚本。
- 不要只返回原始 JSON，不做整理。
- 不要把 `PMC` 网页 `/pdf/` 地址或 `Semantic Scholar openAccessPdf` 默认表述成“稳定可下载 PDF”。
- 不要为了下载全文去手写网页抓取逻辑；如需网页交互，必须通过 `task(subagent_type="agent-browser")` 委派浏览器子代理。
- 不要使用 `webfetch` 去“下载 PDF”；这经常得到 HTML 页面而不是 PDF 二进制。
- 不要只凭扩展名是 `.pdf`、URL 里带 `/pdf/`、或响应里出现 `%PDF` 就宣称文件有效；必须以校验脚本结果为准。

## 相关文件
- `.claude/skills/paper-literature-search/scripts/search_literature.py`
- `.claude/skills/paper-literature-search/scripts/validate_downloaded_pdf.py`
