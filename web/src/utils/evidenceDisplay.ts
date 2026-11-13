const FIELD_LABELS: Record<string, string> = {
  anchor: '锚点（Anchor）',
  anchor_id: '锚点（Anchor）',
  anchor_ids: '锚点（Anchor）',
  artifact: '产物（Artifact）',
  artifact_ref: '产物（Artifact）',
  artifact_refs: '产物（Artifact）',
  chunk_id: '文本块（Chunk）',
  confidence: '置信度（Confidence）',
  doc_id: '文献（Document）',
  evidence_id: '证据编号（Evidence ID）',
  fact_id: '事实（Fact）',
  figure_image_paths: '论文图（Figure）',
  image_path: '图片（Image）',
  locator: '定位（Locator）',
  page: '页码（Page）',
  page_image_path: '论文页（Page）',
  page_image_paths: '论文页（Page）',
  pdf_path: 'PDF 文件（PDF file）',
  record_path: '记录文件（Record file）',
  row_index: '行号（Row）',
  source: '来源（Source）',
  source_agent: '来源代理（Source agent）',
  source_id: '来源（Source）',
  source_path: '来源文件（Source file）',
  source_type: '来源类型（Source type）',
  stage: '阶段（Stage）',
  stage_id: '阶段（Stage）',
  table_id: '表格（Table）',
  table_image_path: '表格图片（Table image）',
  tables_extract_path: '表格提取（Tables extract）',
  text: '文本（Text）',
  visual_manifest_path: '图片清单（Visual manifest）',
  workflow_run_id: '工作流（Workflow）',
};

const KIND_LABELS: Record<string, string> = {
  paper_experimental_condition: '实验条件（Experimental condition）',
  paper_figure_image: '论文图（Paper figure）',
  paper_gene_alias: '基因别名（Gene alias）',
  paper_mutation: '论文变异（Paper mutation）',
  paper_page_chunk: '论文页文本块（Paper page chunk）',
  paper_page_image: '论文页（Paper page）',
  paper_passage_observation: '论文段落观察（Passage observation）',
  paper_chunk: '论文文本块（Paper chunk）',
  paper_fact: '论文事实（Paper fact）',
  paper_anchor: '论文锚点（Paper anchor）',
  paper_visual: '论文图片（Paper visual）',
  paper_table: '论文表格（Paper table）',
  source: '来源（Source）',
  claim: '结论（Claim）',
  tool_result: '执行记录',
  observation: '观察记录',
  artifact: '来源文件',
};

const GRAPH_NODE_LABELS: Record<string, string> = {
  anchor: '锚点（Anchor）',
  artifact: '产物（Artifact）',
  claim: '结论（Claim）',
  entity: '实体（Entity）',
  evidence: '证据（Evidence）',
  metric: '指标（Metric）',
  mutation: '变异（Mutation）',
  paper_fact: '论文事实（Paper fact）',
  paper_visual: '论文图片（Paper visual）',
  paper_chunk: '论文片段',
  workflow_run: '工作流',
  source: '来源（Source）',
  stage: '阶段（Stage）',
};

const GRAPH_EDGE_LABELS: Record<string, string> = {
  contradicts: '反驳（Contradicts）',
  cites_anchor: '引用锚点（Cites anchor）',
  contains_evidence: '包含证据（Contains evidence）',
  derived_from: '派生自（Derived from）',
  has_artifact: '包含产物（Has artifact）',
  has_source: '包含来源（Has source）',
  materialized_as_evidence: '生成证据（Materialized as evidence）',
  points_to_artifact: '指向产物（Points to artifact）',
  produced_evidence: '产出证据（Produced evidence）',
  relates_to: '关联（Relates to）',
  supports: '支持（Supports）',
};

const PATH_FIELDS = new Set([
  'figure_image_paths',
  'image_path',
  'markdown_path',
  'page_image_path',
  'page_image_paths',
  'pdf_path',
  'record_path',
  'source_path',
  'table_image_path',
  'tables_extract_path',
  'visual_manifest_path',
]);

const INTERNAL_ID = /\b(?:pack|ev|asset|research|event|session|workflow|run|activity|finding|conflict|claim|chunk|anchor|paper|fact|proposal|revision|experiment|episode|link)-[0-9a-f]{8,}[a-z0-9_.:-]*\b|\b(?:evidence|paper|source|artifact|workflow_run|stage|private-evidence|private-assertion):[^\s,，;；\]\)]+|\b[0-9a-f]{32}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
const INTERNAL_LABEL = /^(?:pack|ev|asset|research|event|session|workflow|run|activity|finding|conflict|claim|chunk|anchor|paper|fact|proposal|revision|experiment|episode|link)-[a-z0-9][a-z0-9_.:-]*$/i;
const INTERNAL_TOOL = /\b(?:paper_(?:library|literature|prepare|wiki|research)[a-z0-9_]*|tcode_evidence_[a-z0-9_]+|evidence_(?:search|graph|doctor|write)|assign_task|job_submit|job_describe_target_tool|mcp__[a-z0-9_]+)\b/gi;

export const userFacingEvidenceText = (value: string) => value.split(/(\[evidence:[A-Za-z0-9_.:-]+\])/g).map(part =>
  /^\[evidence:/.test(part) ? part : part.replace(INTERNAL_ID, identifier => /^pack-/i.test(identifier) ? '研究包' : '来源资料')
    .replace(INTERNAL_TOOL, tool => /(?:lookup|search)/.test(tool) ? '检索资料' : /doctor/.test(tool) ? '检查证据' : /(?:get_record|read)/.test(tool) ? '读取资料' : '处理资料'),
).join('');

export const internalEvidenceTokenLabel = (value: string): string | undefined => {
  const matches = [...value.matchAll(INTERNAL_ID), ...value.matchAll(INTERNAL_TOOL)];
  return matches.some(match => match[0] === value) ? userFacingEvidenceText(value) : undefined;
};

export function sanitizeEvidenceProse(container: HTMLElement) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  for (const node of nodes) {
    if (node.parentElement?.closest('pre,code,blockquote,math,.katex,.mermaid-diagram,.mermaid-block,.evidence-source-btn')) continue;
    node.textContent = userFacingEvidenceText(node.textContent || '');
  }
}

export const readableSourceLabel = (value: unknown, fallback = '来源资料') => {
  const raw = String(value || '').trim();
  if (INTERNAL_LABEL.test(raw)) return fallback;
  const text = raw.replace(/\[evidence:[^\]]+\]/g, '').replace(INTERNAL_ID, '').replace(INTERNAL_TOOL, '').replace(/\s+/g, ' ').replace(/^[\s:：·,，;；-]+|[\s:：·,，;；-]+$/g, '').trim();
  return text && !/^[._:/-]+$/.test(text) ? text : fallback;
};

export const shortDisplayPath = (value: unknown) => {
  const text = String(value || '').trim();
  if (!text) return '';
  if (/^(https?:|data:|mailto:)/i.test(text)) return text;
  const normalized = text.replace(/\\/g, '/');
  const parts = normalized.split('/').filter(Boolean);
  return normalized.includes('/') ? parts[parts.length - 1] || text : text;
};

export const evidenceFieldLabel = (key: unknown) => {
  const normalized = String(key || '').trim();
  return FIELD_LABELS[normalized.toLowerCase()] || normalized;
};

export const evidenceKindLabel = (kind: unknown) => {
  const normalized = String(kind || '').trim();
  return KIND_LABELS[normalized.toLowerCase()] || '来源证据';
};

export const graphNodeTypeLabel = (type: unknown) => {
  const normalized = String(type || '').trim();
  return GRAPH_NODE_LABELS[normalized.toLowerCase()] || '关联来源';
};

export const graphEdgeTypeLabel = (type: unknown) => {
  const normalized = String(type || '').trim();
  return GRAPH_EDGE_LABELS[normalized.toLowerCase()] || '关联（Relates to）';
};

export const sanitizeEvidenceDisplayValue = (value: unknown, parentKey = ''): unknown => {
  if (PATH_FIELDS.has(parentKey.toLowerCase())) {
    return Array.isArray(value) ? value.map(item => readableSourceLabel(shortDisplayPath(item), '来源文件')) : readableSourceLabel(shortDisplayPath(value), '来源文件');
  }
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeEvidenceDisplayValue(item, parentKey));
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).filter(([key]) => !(/(?:^|_)(?:id|ids)$|^(?:source_agent|tool_name|tool_server|content_hash|embedding_version)$/.test(key) && !['table_id', 'pmid', 'doi'].includes(key))).map(([key, item]) => [
        evidenceFieldLabel(key),
        sanitizeEvidenceDisplayValue(item, key),
      ]),
    );
  }
  if (typeof value === 'string') return readableSourceLabel(value, '');
  return value;
};

export const stringifyEvidenceItem = (item: unknown) => {
  if (typeof item === 'string') return readableSourceLabel(item, '');
  try {
    return JSON.stringify(sanitizeEvidenceDisplayValue(item), null, 2);
  } catch {
    return String(item || '');
  }
};
