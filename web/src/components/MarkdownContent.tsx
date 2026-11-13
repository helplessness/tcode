import DOMPurify from 'dompurify';
import { desktop } from '../desktop';
import { isLiteralSmilesLink } from '../utils/smilesMarkdown';
import { marked } from 'marked';
import { For, Show, createEffect, createMemo, createSignal } from 'solid-js';
import { Portal } from 'solid-js/web';
import { sessionStore } from '../stores/session';
import ImageLightbox, {
  ZoomableImage,
  type LightboxImageSource,
  usesSingleTapImagePreview,
} from './ImageLightbox';
import {
  evidenceKindLabel,
  graphEdgeTypeLabel,
  graphNodeTypeLabel,
  readableSourceLabel,
  sanitizeEvidenceProse,
  internalEvidenceTokenLabel,
  shortDisplayPath,
  stringifyEvidenceItem,
} from '../utils/evidenceDisplay';
import {
  autoEmbedStandaloneImagePaths,
  isImageFileRef,
  isLocalFileRef,
  normalizeInlineFilePath,
  toInlineHref,
} from '../utils/inlineFiles';
import { openCitation } from '../utils/citations';

type MarkdownRole = 'assistant' | 'user' | 'system' | 'tool' | string;

type MarkdownContentProps = {
  content: string;
  role?: MarkdownRole;
  class?: string;
  renderKey?: string;
  onEvidenceClick?: (evidenceId: string) => void;
  allowSessionActions?: boolean;
};

type EvidenceDetail = {
  evidence_id?: string;
  kind?: string;
  title?: string;
  content?: string;
  source_type?: string;
  source_id?: string;
  source_path?: string;
  locator?: string;
  claims?: unknown[];
  entities?: unknown[];
  citations?: unknown[];
  confidence?: string | number;
  workflow_run_id?: string;
  stage_id?: string;
  source_agent?: string;
  artifact_refs?: Record<string, unknown>;
  images?: EvidenceImage[];
};

type EvidenceImage = {
  index: number;
  kind?: string;
  label?: string;
  name?: string;
};

type EvidenceGraphNode = {
  node_id?: string;
  label?: string;
  type?: string;
  [key: string]: unknown;
};

type EvidenceGraphEdge = {
  source?: string;
  target?: string;
  type?: string;
  [key: string]: unknown;
};

type EvidenceGraph = {
  nodes?: EvidenceGraphNode[];
  edges?: EvidenceGraphEdge[];
  node_count?: number;
  edge_count?: number;
  conflict_count?: number;
  start_node_id?: string;
};

type EvidenceModalState = {
  open: boolean;
  loading: boolean;
  error: string;
  evidence?: EvidenceDetail;
  graph?: EvidenceGraph;
  doctor?: {
    status?: string;
    issue_count?: number;
    counts?: Record<string, unknown>;
    issues?: Array<Record<string, unknown>>;
  };
  imageQuery?: string;
};

type LocalFileMenuState = {
  path: string;
  x: number;
  y: number;
};

const CODE_FONT_FAMILY = 'JetBrains Mono, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace';
const MATH_DELIMITER_PATTERN = /(^|[^\\])(?:\$\$|\\\(|\\\[|\$[^$\s])/m;
type HighlightJsApi = typeof import('highlight.js/lib/core').default;
type HighlightLanguageModule = typeof import('highlight.js/lib/languages/plaintext');

let highlightJsPromise: Promise<HighlightJsApi> | null = null;
let mathRendererPromise: Promise<typeof import('katex/contrib/auto-render').default> | null = null;
let mermaidPromise: Promise<typeof import('mermaid').default> | null = null;
let mermaidConfigured = false;
let mermaidRenderSequence = 0;
const EVIDENCE_MARKER_PATTERN = /\[(evidence|knowledge):([A-Za-z0-9_.:-]+)\]/g;

const escapeHtml = (value: string) =>
  value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

const classifyAssistantLine = (line: string) => {
  const trimmed = line.trim();
  if (!trimmed) return '';

  if (/^(结论|结果摘要|结果|summary|final answer|最终回答)\s*[:：]/i.test(trimmed)) {
    return 'msg-callout-summary';
  }
  if (/^(下一步|建议|recommended next step|next step|action items?)\s*[:：]/i.test(trimmed)) {
    return 'msg-callout-next';
  }
  if (/^(风险|注意|警告|阻碍|失败原因|error|warning|blockers?)\s*[:：]/i.test(trimmed)) {
    return 'msg-callout-warning';
  }
  if (/^(状态|status)\s*[:：]/i.test(trimmed)) {
    return 'msg-callout-status';
  }
  if (/^[✅✔].*/.test(trimmed)) {
    return 'msg-callout-success';
  }
  if (/^[❌✖].*/.test(trimmed)) {
    return 'msg-callout-danger';
  }
  if (/^[⚠].*/.test(trimmed)) {
    return 'msg-callout-warning';
  }
  if (/^[ℹ].*/.test(trimmed)) {
    return 'msg-callout-status';
  }
  return '';
};

const highlightAssistantCallouts = (content: string) => {
  let inFence = false;

  return content
    .split('\n')
    .map((line) => {
      if (/^```/.test(line.trim())) {
        inFence = !inFence;
        return line;
      }
      if (inFence) return line;

      const cls = classifyAssistantLine(line);
      if (!cls) return line;
      return `<div class="msg-callout ${cls}">${escapeHtml(line)}</div>`;
    })
    .join('\n');
};

const buildMarkdownRenderer = (workspaceId: string, role: MarkdownRole) => {
  const renderer = new marked.Renderer();
  const displayLocalPath = (path: string) => path.startsWith('/') ? path : `/workspace/${path}`;

  renderer.codespan = function ({ text }) {
    const raw = String(text || '').trim();
    const internalLabel = role !== 'user' ? internalEvidenceTokenLabel(raw) : undefined;
    if (internalLabel) return `<code>${escapeHtml(internalLabel)}</code>`;
    if (!isLocalFileRef(raw)) return `<code>${escapeHtml(String(text || ''))}</code>`;
    const localPath = normalizeInlineFilePath(raw);
    return `<code class="markdown-local-file" role="button" tabindex="0" data-local-path="${escapeHtml(localPath)}" title="${escapeHtml(displayLocalPath(localPath))}">${escapeHtml(String(text || ''))}</code>`;
  };

  renderer.link = function (token) {
    const { href, title, tokens } = token;
    if (isLiteralSmilesLink(token)) return escapeHtml(token.raw);
    const rawTarget = String(href || '').trim();
    const body = this.parser.parseInline(tokens);
    if (!rawTarget) return body;

    const localPath = isLocalFileRef(rawTarget) ? normalizeInlineFilePath(rawTarget) : '';
    const safeHref = escapeHtml(toInlineHref(rawTarget, workspaceId));
    const titleAttr = (title || localPath) ? ` title="${escapeHtml(title || displayLocalPath(localPath))}"` : '';
    const pathAttr = localPath ? ` data-local-path="${escapeHtml(localPath)}"` : '';
    const externalAttr = localPath ? '' : ' target="_blank" rel="noreferrer"';
    if (localPath && isImageFileRef(localPath)) {
      const safeAlt = escapeHtml(String(tokens.map((token) => token.raw || '').join('') || '图片'));
      return `<a class="markdown-inline-image-link" href="${safeHref}"${titleAttr}${pathAttr}><img class="markdown-inline-image" src="${safeHref}" alt="${safeAlt}" loading="lazy" /></a>`;
    }
    return `<a href="${safeHref}"${titleAttr}${pathAttr}${externalAttr}>${body}</a>`;
  };

  renderer.image = function ({ href, title, text }) {
    const rawTarget = String(href || '').trim();
    if (!rawTarget) return '';

    const localPath = isLocalFileRef(rawTarget) ? normalizeInlineFilePath(rawTarget) : '';
    const src = escapeHtml(toInlineHref(rawTarget, workspaceId));
    const safeAlt = escapeHtml(String(text || '图表证据'));
    const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
    const pathAttr = localPath ? ` data-local-path="${escapeHtml(localPath)}"` : '';
    return `<img class="markdown-inline-image" src="${src}" alt="${safeAlt}" loading="lazy"${titleAttr}${pathAttr} />`;
  };

  renderer.table = function (token) {
    const header = token.header
      .map((cell) => this.tablecell(cell))
      .join('');
    const body = token.rows
      .map((row) => this.tablerow({ text: row.map((cell) => this.tablecell(cell)).join('') }))
      .join('');

    const longTableClass = token.rows.length > 6 ? ' markdown-table-wrapper-long' : '';
    const keyboardScroll = token.rows.length > 6
      ? ' tabindex="0" role="region" aria-label="可滚动表格"'
      : '';
    return `<div class="markdown-table-wrapper${longTableClass}"${keyboardScroll}><table><thead>${this.tablerow({ text: header })}</thead><tbody>${body}</tbody></table></div>`;
  };

  return renderer;
};

const loadHighlightJs = async () => {
  if (!highlightJsPromise) {
    // 完整 highlight.js 会把约 190 种语言全部下载到浏览器。这里只注册
    // TCode 对话中常见的语言，未知语言仍按安全的纯文本代码块展示。
    highlightJsPromise = Promise.all([
      import('highlight.js/lib/core'),
      import('highlight.js/styles/github.css'),
      import('highlight.js/lib/languages/bash'),
      import('highlight.js/lib/languages/c'),
      import('highlight.js/lib/languages/cpp'),
      import('highlight.js/lib/languages/csharp'),
      import('highlight.js/lib/languages/css'),
      import('highlight.js/lib/languages/diff'),
      import('highlight.js/lib/languages/dockerfile'),
      import('highlight.js/lib/languages/go'),
      import('highlight.js/lib/languages/java'),
      import('highlight.js/lib/languages/javascript'),
      import('highlight.js/lib/languages/json'),
      import('highlight.js/lib/languages/markdown'),
      import('highlight.js/lib/languages/plaintext'),
      import('highlight.js/lib/languages/python'),
      import('highlight.js/lib/languages/rust'),
      import('highlight.js/lib/languages/sql'),
      import('highlight.js/lib/languages/typescript'),
      import('highlight.js/lib/languages/xml'),
      import('highlight.js/lib/languages/yaml'),
    ]).then(([coreModule, , ...languageModules]) => {
      const hljs = coreModule.default;
      const names = [
        'bash',
        'c',
        'cpp',
        'csharp',
        'css',
        'diff',
        'dockerfile',
        'go',
        'java',
        'javascript',
        'json',
        'markdown',
        'plaintext',
        'python',
        'rust',
        'sql',
        'typescript',
        'xml',
        'yaml',
      ];
      languageModules.forEach((module, index) => {
        hljs.registerLanguage(
          names[index],
          (module as HighlightLanguageModule).default,
        );
      });
      return hljs;
    });
  }
  return highlightJsPromise;
};

const loadMathRenderer = async () => {
  if (!mathRendererPromise) {
    mathRendererPromise = import('katex/contrib/auto-render').then((module) => module.default);
  }
  return mathRendererPromise;
};

const loadMermaid = async () => {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((module) => module.default);
  }
  return mermaidPromise;
};

const ensureMermaidConfigured = async () => {
  const mermaidApi = await loadMermaid();
  if (mermaidConfigured) return mermaidApi;

  mermaidApi.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    suppressErrorRendering: true,
    theme: 'base',
    fontFamily: CODE_FONT_FAMILY,
    themeVariables: {
      darkMode: false,
      primaryColor: '#e8ecdc',
      primaryTextColor: '#353b2e',
      primaryBorderColor: '#88976d',
      secondaryColor: '#ede9de',
      tertiaryColor: '#f5f1e8',
      lineColor: '#737367',
      mainBkg: '#fffdf7',
      clusterBkg: '#f5f1e8',
      clusterBorder: '#dcd6c8',
      noteBkgColor: '#f2e9d1',
      noteTextColor: '#353b2e',
    },
  });

  mermaidConfigured = true;
  return mermaidApi;
};

const getCodeFenceLanguage = (codeElement: Element) => {
  const languageClass = Array.from(codeElement.classList).find((name) => name.startsWith('language-'));
  return languageClass ? languageClass.slice('language-'.length).trim().toLowerCase() : '';
};

const highlightCodeBlocks = async (blocks: HTMLElement[]) => {
  if (!blocks.length) return;
  const hljs = await loadHighlightJs();
  blocks.forEach((block) => {
    const language = getCodeFenceLanguage(block);
    if (language && !hljs.getLanguage(language)) {
      block.classList.add('hljs');
      return;
    }
    hljs.highlightElement(block);
  });
};

const renderMathBlocks = async (container: HTMLElement) => {
  if (!MATH_DELIMITER_PATTERN.test(container.textContent || '')) return;

  const renderMathInElement = await loadMathRenderer();
  renderMathInElement(container, {
    delimiters: [
      { left: '$$', right: '$$', display: true },
      { left: '\\[', right: '\\]', display: true },
      { left: '$', right: '$', display: false },
      { left: '\\(', right: '\\)', display: false },
    ],
    throwOnError: false,
    strict: 'ignore',
    trust: false,
    ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
    ignoredClasses: ['mermaid-diagram', 'mermaid-block'],
  });
};

const renderMermaidBlocks = async (
  mermaidBlocks: HTMLElement[],
  renderKey: string,
  isCurrentRun: () => boolean,
) => {
  if (!mermaidBlocks.length) return;

  const mermaidApi = await ensureMermaidConfigured();

  for (const [index, block] of mermaidBlocks.entries()) {
    if (!isCurrentRun()) return;

    const source = String(block.textContent || '').trim();
    const pre = block.closest('pre');
    if (!(pre instanceof HTMLElement) || !source) continue;

    const frame = document.createElement('div');
    frame.className = 'mermaid-block';

    const label = document.createElement('div');
    label.className = 'mermaid-block-label';
    label.textContent = 'Mermaid';

    const diagram = document.createElement('div');
    diagram.className = 'mermaid-diagram';

    frame.append(label, diagram);
    pre.replaceWith(frame);

    try {
      const mermaidRenderId = `mermaid-${renderKey}-${index}-${++mermaidRenderSequence}`.replace(/[^a-zA-Z0-9_-]/g, '-');
      const { svg } = await mermaidApi.render(mermaidRenderId, source);
      if (!isCurrentRun()) return;
      diagram.innerHTML = svg;
    } catch (error) {
      if (!isCurrentRun()) return;

      const errorNotice = document.createElement('div');
      errorNotice.className = 'mermaid-render-error';
      errorNotice.textContent = 'Mermaid 渲染失败，已回退为源码展示。';

      const fallback = document.createElement('pre');
      fallback.className = 'mermaid-source-fallback';

      const fallbackCode = document.createElement('code');
      fallbackCode.textContent = source;
      fallback.append(fallbackCode);

      frame.replaceChildren(label, errorNotice, fallback);
      console.error('Mermaid render failed:', error);
    }
  }
};

const renderEvidenceMarkerButtons = (container: HTMLElement) => {
  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode(node) {
        const text = String(node.nodeValue || '');
        const parent = node.parentElement;
        EVIDENCE_MARKER_PATTERN.lastIndex = 0;
        if (!EVIDENCE_MARKER_PATTERN.test(text)) return NodeFilter.FILTER_REJECT;
        if (parent?.closest('pre, code, a, button, textarea')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    },
  );
  const nodes: Text[] = [];
  while (walker.nextNode()) {
    if (walker.currentNode instanceof Text) nodes.push(walker.currentNode);
  }

  nodes.forEach((node) => {
    const source = String(node.nodeValue || '');
    const fragment = document.createDocumentFragment();
    let lastIndex = 0;
    EVIDENCE_MARKER_PATTERN.lastIndex = 0;

    for (const match of source.matchAll(EVIDENCE_MARKER_PATTERN)) {
      const evidenceId = String(match[2] || '').trim();
      const start = match.index || 0;
      if (start > lastIndex) {
        fragment.append(document.createTextNode(source.slice(lastIndex, start)));
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'evidence-source-btn';
      button.dataset.evidenceId = evidenceId;
      button.dataset.citationKind = match[1];
      button.title = '查看来源证据（View source）';
      button.textContent = '查看来源';
      fragment.append(button);
      lastIndex = start + match[0].length;
    }

    if (lastIndex < source.length) {
      fragment.append(document.createTextNode(source.slice(lastIndex)));
    }
    node.replaceWith(fragment);
  });
};

const enhanceRenderedContent = async (
  container: HTMLElement,
  renderKey: string,
  isCurrentRun: () => boolean,
) => {
  renderEvidenceMarkerButtons(container);
  container.querySelectorAll('img.markdown-inline-image').forEach((image) => {
    if (!image.getAttribute('title')) image.setAttribute('title', '双击查看大图');
  });

  const codeBlocks = Array.from(container.querySelectorAll('pre code')).filter(
    (block): block is HTMLElement => block instanceof HTMLElement,
  );
  const mermaidBlocks = codeBlocks.filter((block) => getCodeFenceLanguage(block) === 'mermaid');
  const standardCodeBlocks = codeBlocks.filter((block) => getCodeFenceLanguage(block) !== 'mermaid');

  await highlightCodeBlocks(standardCodeBlocks);
  if (!isCurrentRun()) return;

  await renderMathBlocks(container);
  if (!isCurrentRun()) return;

  await renderMermaidBlocks(mermaidBlocks, renderKey, isCurrentRun);
};

const renderMarkdown = (content: string, role: MarkdownRole, workspaceId: string) => {
  const imageCompatibleContent = autoEmbedStandaloneImagePaths(content);
  const source = role === 'assistant' ? highlightAssistantCallouts(imageCompatibleContent) : imageCompatibleContent;
  const html = marked.parse(source, {
    gfm: true,
    breaks: true,
    renderer: buildMarkdownRenderer(workspaceId, role),
  });

  return DOMPurify.sanitize(String(html || ''), {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['class', 'data-local-path', 'target', 'rel', 'loading', 'title', 'alt'],
  });
};

const evidenceList = (value: unknown[] | undefined) =>
  Array.isArray(value)
    ? value.map(stringifyEvidenceItem).map((item) => item.trim()).filter(Boolean)
    : [];

const compactGraphLabel = (value: unknown, maxLength = 22) => {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 3).trim()}...`;
};

const graphNodeFill = (type: unknown) => {
  switch (String(type || '').toLowerCase()) {
    case 'evidence':
      return '#7bd0ff';
    case 'source':
      return '#9ee6bb';
    case 'claim':
      return '#f2cc60';
    case 'mutation':
    case 'metric':
    case 'entity':
      return '#d2a8ff';
    case 'anchor':
      return '#ffa657';
    case 'artifact':
    case 'paper_visual':
      return '#ffb6c1';
    default:
      return '#8b949e';
  }
};

const graphEdgeStroke = (type: unknown) =>
  String(type || '').toLowerCase() === 'contradicts' ? '#f85149' : '#56616a';

export default function MarkdownContent(props: MarkdownContentProps) {
  let contentRef: HTMLDivElement | undefined;
  let enhancementVersion = 0;
  const [evidenceModal, setEvidenceModal] = createSignal<EvidenceModalState>({
    open: false,
    loading: false,
    error: '',
  });
  const [inlineLightboxImage, setInlineLightboxImage] = createSignal<LightboxImageSource | null>(null);
  const [localFileMenu, setLocalFileMenu] = createSignal<LocalFileMenuState | null>(null);
  const resolvedFilePaths = new Map<string, Promise<string>>();

  const role = createMemo(() => String(props.role || 'assistant'));
  const renderKey = createMemo(() => String(props.renderKey || 'markdown'));
  const workspaceId = createMemo(() => {
    const currentSession = sessionStore.currentSession();
    return String(currentSession?.workspace_id || currentSession?.workdir_name || '').trim();
  });
  const evidenceDetails = createMemo(() => evidenceModal().evidence || {});
  const evidenceClaims = createMemo(() => evidenceList(evidenceDetails().claims));
  const evidenceEntities = createMemo(() => evidenceList(evidenceDetails().entities));
  const evidenceCitations = createMemo(() => evidenceList(evidenceDetails().citations));
  const evidenceImages = createMemo(() => (
    Array.isArray(evidenceDetails().images) ? evidenceDetails().images! : []
  ));
  const evidenceGraph = createMemo(() => evidenceModal().graph || {});
  const evidenceDoctor = createMemo(() => evidenceModal().doctor || {});
  const evidenceMeta = createMemo(() => {
    const record = evidenceDetails();
    return [
      evidenceKindLabel(record.kind),
      record.confidence ? `置信度（Confidence）：${record.confidence}` : '',
    ]
      .map((item) => String(item || '').trim())
      .filter(Boolean);
  });
  const evidenceKeyValues = createMemo(() => {
    const record = evidenceDetails();
    return [
      ['来源文件', record.source_path ? readableSourceLabel(shortDisplayPath(record.source_path), '来源文件') : ''],
      ['原文位置', readableSourceLabel(record.locator, '')],
    ].filter(([, value]) => String(value || '').trim()) as string[][];
  });
  const graphNodes = createMemo(() => {
    const currentEvidenceNode = `evidence:${String(evidenceDetails().evidence_id || '').trim()}`;
    return (Array.isArray(evidenceGraph().nodes) ? evidenceGraph().nodes! : [])
      .filter((node) => String(node.node_id || '') !== currentEvidenceNode)
      .slice(0, 14);
  });
  const graphEdges = createMemo(() =>
    (Array.isArray(evidenceGraph().edges) ? evidenceGraph().edges! : []).slice(0, 12),
  );
  const graphVisualNodes = createMemo(() => {
    const nodes = Array.isArray(evidenceGraph().nodes) ? evidenceGraph().nodes! : [];
    if (!nodes.length) return [];
    const startNodeId = String(
      evidenceGraph().start_node_id || `evidence:${String(evidenceDetails().evidence_id || '').trim()}`,
    );
    const startNode = nodes.find((node) => String(node.node_id || '') === startNodeId) || nodes[0];
    const visible = [
      startNode,
      ...nodes
        .filter((node) => String(node.node_id || '') !== String(startNode.node_id || ''))
        .slice(0, 11),
    ];
    const outerCount = Math.max(1, visible.length - 1);
    return visible.map((node, index) => {
      if (index === 0) {
        return { ...node, x: 50, y: 50, isStart: true };
      }
      const angle = -Math.PI / 2 + ((index - 1) / outerCount) * Math.PI * 2;
      return {
        ...node,
        x: 50 + Math.cos(angle) * 34,
        y: 50 + Math.sin(angle) * 31,
        isStart: false,
      };
    });
  });
  const graphNodePositionById = createMemo(() => {
    const positions: Record<string, { x: number; y: number }> = {};
    for (const node of graphVisualNodes()) {
      const id = String(node.node_id || '').trim();
      if (id) positions[id] = { x: Number(node.x || 0), y: Number(node.y || 0) };
    }
    return positions;
  });
  const graphVisualEdges = createMemo(() => {
    const positions = graphNodePositionById();
    return (Array.isArray(evidenceGraph().edges) ? evidenceGraph().edges! : [])
      .filter((edge) => positions[String(edge.source || '')] && positions[String(edge.target || '')])
      .slice(0, 18);
  });
  const graphLabelById = createMemo(() => {
    const labels: Record<string, string> = {};
    for (const node of Array.isArray(evidenceGraph().nodes) ? evidenceGraph().nodes! : []) {
      const id = String(node.node_id || '').trim();
      if (id) labels[id] = readableSourceLabel(shortDisplayPath(node.label), '关联来源');
    }
    return labels;
  });
  const doctorIssues = createMemo(() =>
    (Array.isArray(evidenceDoctor().issues) ? evidenceDoctor().issues! : []).slice(0, 8),
  );
  const doctorStatus = createMemo(() => String(evidenceDoctor().status || '').trim().toLowerCase());
  const formattedContent = createMemo(() => {
    const raw = String(props.content || '');
    if (!raw) return '';
    try {
      const html = renderMarkdown(raw, role(), workspaceId());
      const output = html || escapeHtml(raw).replace(/\n/g, '<br>');
      if (role() !== 'assistant' && role() !== 'tool') return output;
      const container = document.createElement('div');
      container.innerHTML = output;
      sanitizeEvidenceProse(container);
      return container.innerHTML;
    } catch (error) {
      console.error('Markdown render failed:', error);
      return escapeHtml(raw).replace(/\n/g, '<br>');
    }
  });

  createEffect(() => {
    const html = formattedContent();
    const container = contentRef;
    const currentRenderKey = renderKey();

    if (!container || !html) return;

    const currentVersion = ++enhancementVersion;
    queueMicrotask(() => {
      if (!contentRef || contentRef !== container || enhancementVersion !== currentVersion) return;
      void enhanceRenderedContent(container, currentRenderKey, () => enhancementVersion === currentVersion).catch((error) => {
        console.error('Markdown enhancement failed:', error);
      });
    });
  });

  const closeEvidenceModal = () => {
    setEvidenceModal({ open: false, loading: false, error: '' });
  };

  const resolveFullFilePath = (localPath: string) => {
    if (!desktop?.resolveFile || !workspaceId()) return Promise.resolve(localPath);
    const cached = resolvedFilePaths.get(localPath);
    if (cached) return cached;
    const pending = desktop.resolveFile(workspaceId(), localPath)
      .then((result) => String(result.path || localPath))
      .catch(() => localPath);
    resolvedFilePaths.set(localPath, pending);
    return pending;
  };

  const fileNameFromPath = (localPath: string) => localPath.split('/').filter(Boolean).pop() || localPath;

  const openInlineImage = (image: HTMLImageElement) => {
    const src = String(image.currentSrc || image.src || '').trim();
    if (!src) return;
    const localPath = String(image.dataset.localPath || image.closest('[data-local-path]')?.getAttribute('data-local-path') || '').trim();
    setInlineLightboxImage({
      src,
      alt: String(image.alt || image.title || localPath.split('/').pop() || '图片'),
      downloadName: localPath || undefined,
    });
  };

  const evidenceImageUrl = (image: EvidenceImage) => {
    const evidenceId = String(evidenceDetails().evidence_id || '').trim();
    const query = String(evidenceModal().imageQuery || '').trim();
    return `/api/evidence/${encodeURIComponent(evidenceId)}/images/${Number(image.index)}${query ? `?${query}` : ''}`;
  };

  return (
    <>
      <div
        ref={contentRef}
        class={`markdown-content ${role() === 'user' ? 'markdown-content-user' : ''} ${props.class || ''}`.trim()}
        onClick={(event) => {
          const target = event.target;
          if (!(target instanceof Element)) return;

          const inlineImage = target.closest('img.markdown-inline-image');
          if (inlineImage instanceof HTMLImageElement && usesSingleTapImagePreview()) {
            event.preventDefault();
            openInlineImage(inlineImage);
            return;
          }

          const evidenceButton = target.closest('.evidence-source-btn') as HTMLElement | null;
          const evidenceId = String(evidenceButton?.dataset?.evidenceId || '').trim();
          if (evidenceId) {
            event.preventDefault();
            if (props.allowSessionActions === false) return;
            if (evidenceButton?.dataset.citationKind === 'knowledge') {
              openCitation({ kind: 'knowledge', id: evidenceId }, evidenceButton);
            } else if (props.onEvidenceClick) {
              props.onEvidenceClick(evidenceId);
            } else {
              openCitation({ kind: 'evidence', id: evidenceId }, evidenceButton);
            }
            return;
          }

          const localRef = target.closest('[data-local-path]') as HTMLElement | null;
          const localPath = String(localRef?.dataset?.localPath || '').trim();
          if (!localPath) return;
          event.preventDefault();
          if (props.allowSessionActions !== false) sessionStore.requestFilePreview(localPath);
        }}
        onMouseOver={(event) => {
          if (props.allowSessionActions === false) return;
          const target = event.target;
          if (!(target instanceof Element)) return;
          const localRef = target.closest('[data-local-path]') as HTMLElement | null;
          const localPath = String(localRef?.dataset?.localPath || '').trim();
          if (!localPath || !localRef || localRef.dataset.fullPathResolved === 'true') return;
          localRef.dataset.fullPathResolved = 'true';
          void resolveFullFilePath(localPath).then((fullPath) => {
            if (localRef.isConnected) localRef.title = fullPath;
          });
        }}
        onContextMenu={(event) => {
          if (props.allowSessionActions === false) {event.preventDefault();return;}
          const target = event.target;
          if (!(target instanceof Element)) return;
          const localRef = target.closest('[data-local-path]') as HTMLElement | null;
          const localPath = String(localRef?.dataset?.localPath || '').trim();
          if (!localPath) return;
          event.preventDefault();
          setLocalFileMenu({ path: localPath, x: event.clientX, y: event.clientY });
        }}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          const target = event.target;
          if (!(target instanceof Element)) return;
          const localRef = target.closest('[data-local-path]') as HTMLElement | null;
          const localPath = String(localRef?.dataset?.localPath || '').trim();
          if (!localPath) return;
          event.preventDefault();
          if (props.allowSessionActions !== false) sessionStore.requestFilePreview(localPath);
        }}
        onDblClick={(event) => {
          const target = event.target;
          if (!(target instanceof Element)) return;
          const inlineImage = target.closest('img.markdown-inline-image');
          if (!(inlineImage instanceof HTMLImageElement)) return;
          event.preventDefault();
          event.stopPropagation();
          openInlineImage(inlineImage);
        }}
        innerHTML={formattedContent()}
      />
      <Show when={localFileMenu()} keyed>
        {(menu) => (
          <Portal>
            <div class="markdown-file-context-layer" onPointerDown={() => setLocalFileMenu(null)}>
              <div
                class="resource-library-context-menu markdown-file-context-menu"
                style={{
                  left: `${Math.min(menu.x, window.innerWidth - 190)}px`,
                  top: `${Math.min(menu.y, window.innerHeight - 190)}px`,
                }}
                onPointerDown={(event) => event.stopPropagation()}
              >
                <button onClick={() => { setLocalFileMenu(null); sessionStore.requestFilePreview(menu.path); }}>预览文件</button>
                <Show when={desktop?.revealFile}>
                  <button onClick={() => { setLocalFileMenu(null); void desktop?.revealFile(workspaceId(), menu.path); }}>在 Finder 中显示</button>
                </Show>
                <Show when={desktop?.copyFilePath}>
                  <button onClick={() => { setLocalFileMenu(null); void desktop?.copyFilePath(workspaceId(), menu.path); }}>复制完整路径</button>
                </Show>
                <button onClick={() => { setLocalFileMenu(null); void navigator.clipboard.writeText(fileNameFromPath(menu.path)); }}>复制文件名</button>
              </div>
            </div>
          </Portal>
        )}
      </Show>
      <Show when={inlineLightboxImage()}>
        {(image) => <ImageLightbox image={image()} onClose={() => setInlineLightboxImage(null)} />}
      </Show>
      <Show when={evidenceModal().open}>
        <Portal>
          <div
            class="fixed inset-0 z-[1000] flex items-center justify-center bg-black/65 px-4 py-6"
            onClick={closeEvidenceModal}
          >
            <div
              class="max-h-[82vh] w-full max-w-4xl overflow-hidden rounded-lg border border-[#30363d] bg-[#10161a] shadow-[0_24px_80px_rgba(0,0,0,0.45)]"
              onClick={(event) => event.stopPropagation()}
            >
              <div class="flex items-center justify-between gap-3 border-b border-[#2a3136] px-5 py-4">
                <div>
                  <div class="text-sm font-semibold text-[#f0f6fc]">来源证据（Source Evidence）</div>
                </div>
                <button
                  type="button"
                  class="rounded-lg border border-[#30363d] px-3 py-1.5 text-sm text-[#c9d1d9] transition-colors hover:bg-[#182027]"
                  onClick={closeEvidenceModal}
                >
                  关闭
                </button>
              </div>
              <div class="max-h-[calc(82vh-74px)] overflow-auto px-5 py-4">
                <Show
                  when={!evidenceModal().loading}
                  fallback={<div class="py-10 text-center text-sm text-[#8b949e]">正在加载来源...</div>}
                >
                  <Show
                    when={!evidenceModal().error}
                    fallback={<div class="rounded-xl border border-[#da3633]/30 bg-[#2b1515] px-4 py-3 text-sm text-[#ffb6b0]">{evidenceModal().error}</div>}
                  >
                    <div class="text-base font-semibold text-[#f0f6fc]">
                      {readableSourceLabel(evidenceDetails().title, '来源证据')}
                    </div>
                    <div class="mt-3 flex flex-wrap gap-2">
                      <For each={evidenceMeta()}>
                        {(item) => (
                          <span class="rounded-full border border-[#30363d] bg-[#171c20] px-2.5 py-1 text-xs text-[#9faeb7]">{item}</span>
                        )}
                      </For>
                    </div>

                    <Show when={doctorStatus()}>
                      <div class={`mt-4 rounded-xl border px-4 py-3 ${
                        doctorStatus() === 'fail'
                          ? 'border-[#da3633]/35 bg-[#2b1515] text-[#ffb6b0]'
                          : doctorStatus() === 'warn'
                            ? 'border-[#f1a02b]/35 bg-[#241b10] text-[#ffe1b3]'
                            : 'border-[#238636]/35 bg-[#0f1c13] text-[#9ee6bb]'
                      }`}>
                        <div class="flex flex-wrap items-center justify-between gap-2">
                          <div class="text-xs font-semibold">证据健康（Evidence health）</div>
                          <div class="text-xs">{doctorStatus().toUpperCase()} · {Number(evidenceDoctor().issue_count || 0)} 个问题（issues）</div>
                        </div>
                        <Show when={doctorIssues().length > 0}>
                          <div class="mt-2 space-y-1 text-xs leading-relaxed">
                            <For each={doctorIssues()}>
                              {(issue) => (
                                <div>
                                  <span class="font-semibold">{issue.severity === 'error' ? '错误' : '提醒'}</span>
                                  <span> · {readableSourceLabel(issue.message, '该来源需要进一步核对')}</span>
                                </div>
                              )}
                            </For>
                          </div>
                        </Show>
                      </div>
                    </Show>

                    <div class="mt-4">
                      <div class="text-[11px] font-semibold text-[#8b949e]">证据内容（Content）</div>
                      <div class="mt-2 whitespace-pre-wrap rounded-xl border border-[#2a3136] bg-[#0d1117] px-4 py-3 text-sm leading-relaxed text-[#dee3e8]">
                        {String(evidenceDetails().content || '暂无内容（No content）')}
                      </div>
                    </div>

                    <Show when={evidenceImages().length > 0}>
                      <div class="mt-4">
                        <div class="text-[11px] font-semibold text-[#8b949e]">来源图片（Source images）</div>
                        <div class="mt-2 grid gap-3 md:grid-cols-2">
                          <For each={evidenceImages()}>
                            {(image) => (
                              <figure class="overflow-hidden rounded-lg border border-[#2a3136] bg-[#0d1117]">
                                <ZoomableImage
                                  src={evidenceImageUrl(image)}
                                  alt={readableSourceLabel(image.label || image.name, '来源图片')}
                                  downloadName={String(image.name || '') || undefined}
                                  class="max-h-[520px] w-full object-contain"
                                  loading="lazy"
                                />
                                <figcaption class="border-t border-[#2a3136] px-3 py-2 text-xs text-[#9faeb7]">
                                  {readableSourceLabel(image.label || image.name, `来源图片 ${image.index + 1}`)}
                                </figcaption>
                              </figure>
                            )}
                          </For>
                        </div>
                      </div>
                    </Show>

                    <Show when={evidenceKeyValues().length > 0}>
                      <dl class="mt-4 grid grid-cols-[110px_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
                        <For each={evidenceKeyValues()}>
                          {(row) => (
                            <>
                              <dt class="text-[#8b949e]">{row[0]}</dt>
                              <dd class="min-w-0 break-words text-[#dee3e8]">{row[1]}</dd>
                            </>
                          )}
                        </For>
                      </dl>
                    </Show>

                    <Show when={evidenceClaims().length > 0}>
                      <div class="mt-4">
                        <div class="text-[11px] font-semibold text-[#8b949e]">结论（Claims）</div>
                        <div class="mt-2 whitespace-pre-wrap rounded-xl border border-[#2a3136] bg-[#0d1117] px-4 py-3 text-sm text-[#dee3e8]">
                          {evidenceClaims().join('\n')}
                        </div>
                      </div>
                    </Show>

                    <Show when={evidenceEntities().length > 0}>
                      <div class="mt-4">
                        <div class="text-[11px] font-semibold text-[#8b949e]">实体（Entities）</div>
                        <div class="mt-2 flex flex-wrap gap-2">
                          <For each={evidenceEntities()}>
                            {(entity) => (
                              <span class="rounded-full border border-[#30363d] bg-[#171c20] px-2.5 py-1 text-xs text-[#c9d1d9]">{entity}</span>
                            )}
                          </For>
                        </div>
                      </div>
                    </Show>

                    <Show when={graphNodes().length > 0 || graphEdges().length > 0}>
                      <div class="mt-4">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                          <div class="text-[11px] font-semibold text-[#8b949e]">关联图谱（Related graph）</div>
                          <div class="text-xs text-[#6f7d87]">
                            {Number(evidenceGraph().node_count || 0)} 个节点（nodes） · {Number(evidenceGraph().edge_count || 0)} 条关系（edges）
                            <Show when={Number(evidenceGraph().conflict_count || 0) > 0}>
                              <span> · {Number(evidenceGraph().conflict_count || 0)} 个冲突（conflicts）</span>
                            </Show>
                          </div>
                        </div>
                        <Show when={graphVisualNodes().length > 1}>
                          <div class="mt-3 overflow-hidden rounded-xl border border-[#2a3136] bg-[#0d1117] px-3 py-3">
                            <svg viewBox="0 0 100 100" role="img" aria-label="Evidence relationship graph" class="h-64 w-full">
                              <For each={graphVisualEdges()}>
                                {(edge) => {
                                  const source = graphNodePositionById()[String(edge.source || '')];
                                  const target = graphNodePositionById()[String(edge.target || '')];
                                  const edgeType = String(edge.type || '');
                                  return (
                                    <g>
                                      <line
                                        x1={source.x}
                                        y1={source.y}
                                        x2={target.x}
                                        y2={target.y}
                                        stroke={graphEdgeStroke(edgeType)}
                                        stroke-width={edgeType === 'contradicts' ? 1.3 : 0.75}
                                        stroke-dasharray={edgeType === 'contradicts' ? '3 2' : ''}
                                        opacity={edgeType === 'contradicts' ? 0.95 : 0.7}
                                      />
                                      <title>{graphEdgeTypeLabel(edgeType)}</title>
                                    </g>
                                  );
                                }}
                              </For>
                              <For each={graphVisualNodes()}>
                                {(node) => (
                                  <g>
                                    <circle
                                      cx={Number(node.x || 0)}
                                      cy={Number(node.y || 0)}
                                      r={node.isStart ? 7 : 5.3}
                                      fill={graphNodeFill(node.type)}
                                      stroke={node.isStart ? '#f0f6fc' : '#0d1117'}
                                      stroke-width={node.isStart ? 1.2 : 0.8}
                                    />
                                    <text
                                      x={Number(node.x || 0)}
                                      y={Number(node.y || 0) + (node.isStart ? 11 : 9)}
                                      text-anchor="middle"
                                      fill="#c9d1d9"
                                      font-size={String(node.isStart ? 3.7 : 3.1)}
                                    >
                                      {compactGraphLabel(readableSourceLabel(shortDisplayPath(node.label), '关联来源'), node.isStart ? 20 : 16)}
                                    </text>
                                    <title>{`${graphNodeTypeLabel(node.type)}：${readableSourceLabel(shortDisplayPath(node.label), '关联来源')}`}</title>
                                  </g>
                                )}
                              </For>
                            </svg>
                          </div>
                        </Show>
                        <Show when={graphNodes().length > 0}>
                          <div class="mt-2 flex flex-wrap gap-2">
                            <For each={graphNodes()}>
                              {(node) => (
                                <span
                                  class="max-w-full rounded-full border border-[#30363d] bg-[#171c20] px-2.5 py-1 text-xs text-[#c9d1d9]"
                                  title={`${graphNodeTypeLabel(node.type)}：${readableSourceLabel(shortDisplayPath(node.label), '关联来源')}`}
                                >
                                  <span class="text-[#8b949e]">{graphNodeTypeLabel(node.type)}</span>
                                  <span> · {readableSourceLabel(shortDisplayPath(node.label), '关联来源')}</span>
                                </span>
                              )}
                            </For>
                          </div>
                        </Show>
                        <Show when={graphEdges().length > 0}>
                          <div class="mt-3 space-y-1 rounded-xl border border-[#2a3136] bg-[#0d1117] px-4 py-3 text-xs leading-relaxed text-[#c9d1d9]">
                            <For each={graphEdges()}>
                              {(edge) => (
                                <div class="break-words">
                                  <span>{graphLabelById()[String(edge.source || '')] || '关联来源'}</span>
                                  <span class="text-[#8b949e]"> --{graphEdgeTypeLabel(edge.type)}--&gt; </span>
                                  <span>{graphLabelById()[String(edge.target || '')] || '关联来源'}</span>
                                </div>
                              )}
                            </For>
                          </div>
                        </Show>
                      </div>
                    </Show>

                    <Show when={evidenceCitations().length > 0}>
                      <div class="mt-4">
                        <div class="text-[11px] font-semibold text-[#8b949e]">引用定位（Citations）</div>
                        <div class="mt-2 whitespace-pre-wrap rounded-xl border border-[#2a3136] bg-[#0d1117] px-4 py-3 font-mono text-xs leading-relaxed text-[#c9d1d9]">
                          {evidenceCitations().join('\n\n')}
                        </div>
                      </div>
                    </Show>
                  </Show>
                </Show>
              </div>
            </div>
          </div>
        </Portal>
      </Show>
    </>
  );
}
