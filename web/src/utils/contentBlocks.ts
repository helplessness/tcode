import type { CanvasContentBlock } from '../api/generated/protocol';
import type { PageContentBlock } from '../api/generated/protocol';
export type { PageContentBlock } from '../api/generated/protocol';
import type { ResearchCompareBlock } from '../api/generated/protocol';
export type { ResearchCompareBlock } from '../api/generated/protocol';
export type { CanvasContentBlock } from '../api/generated/protocol';

// One reply can contain drafts created while refining the same named drawing.
// Keep them accessible without stacking several full canvases in the answer.
export function groupCanvasResults(blocks: MessageContentBlock[]): CanvasContentBlock[][] {
  const groups = new Map<string, Map<string, CanvasContentBlock>>();
  for (const block of blocks) {
    if (block.type !== 'canvas') continue;
    const key = JSON.stringify([block.session_id, block.title.trim()]);
    if (!groups.has(key)) groups.set(key, new Map());
    const group = groups.get(key)!;
    group.delete(block.canvas_id); group.set(block.canvas_id, block);
  }
  return [...groups.values()].map(group => [...group.values()]);
}

export type ImageContentBlock = {
  type: 'image';
  asset_id: string;
  session_id: string;
  path: string;
  mime_type: string;
  size: number;
  sha256?: string;
  alt?: string;
  caption?: string;
};

export type McpAppContentBlock = {
  type: 'mcp_app';
  schema: 'tcode.mcp-app.v2';
  app_id: string;
  session_id: string;
  server_name: string;
  tool_name: string;
  resource_uri: string;
  mime_type: 'text/html;profile=mcp-app';
  tool_input: Record<string, unknown>;
  tool_result: Record<string, unknown>;
  fallback: string;
};

export type ArtifactContentBlock = {
  type: 'artifact';
  artifact_id: string;
  uri: string;
  media_type: string;
  title: string;
  fallback: string;
};

export type ResultScalar = string | number | boolean | null;

export type JobMetric = {
  key: string;
  label: string;
  value: ResultScalar;
  unit?: string;
};

export type JobSummaryContentBlock = {
  type: 'job_summary';
  schema: 'tcode.job-summary.v1';
  job_id: string;
  result_type: string;
  status: string;
  title: string;
  description: string;
  metrics: JobMetric[];
  fallback: string;
};

export type DataTableColumn = {
  key: string;
  title: string;
  type: 'string' | 'integer' | 'number' | 'boolean';
  unit?: string;
};

export type DataTableContentBlock = {
  type: 'data_table';
  schema: 'tcode.table.v1';
  id: string;
  title: string;
  columns: DataTableColumn[];
  rows: Record<string, ResultScalar>[];
  total_rows: number;
  primary_key: string;
  default_sort?: { key: string; direction: 'asc' | 'desc' };
  fallback: string;
};

export type ResultSelectionContentBlock = {
  type: 'result_selection'; schema: 'tcode.result-selection.v1'; result_id: string;
  items: Array<{item_id: string; label: string; metrics: Record<string, ResultScalar>}>;
  fallback: string;
};

export type MessageContentBlock =
  | ResearchCompareBlock
  | CanvasContentBlock
  | PageContentBlock
  | ResultSelectionContentBlock
  | ImageContentBlock
  | McpAppContentBlock
  | ArtifactContentBlock
  | JobSummaryContentBlock
  | DataTableContentBlock;

const SUPPORTED_IMAGE_MIME_TYPES = new Set([
  'image/png',
  'image/jpeg',
  'image/webp',
  'image/gif',
  'image/bmp',
]);

const resultText = (value: unknown, maximum: number) => String(value ?? '').trim().slice(0, maximum);

const resultScalar = (value: unknown): ResultScalar => {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') return value.slice(0, 16_384);
  return null;
};

export const sanitizeMessageContentBlocks = (value: unknown): MessageContentBlock[] => {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const blocks: MessageContentBlock[] = [];
  for (const raw of value) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const item = raw as Record<string, unknown>;
    const blockType = String(item.type || '').trim();
    if (blockType === 'intelligent_page') {
      if (item.schema === 'tcode.intelligent-page.v1'
        && typeof item.page_id === 'string' && /^page_[0-9a-f]{32}$/.test(item.page_id)
        && typeof item.revision_id === 'string' && /^rev_[0-9a-f]{32}$/.test(item.revision_id)
        && typeof item.session_id === 'string' && item.session_id.length > 0 && item.session_id.length <= 200
        && typeof item.title === 'string' && item.title.length > 0 && item.title.length <= 256) {
        const key = `page:${item.page_id}:${item.revision_id}`;
        if (!seen.has(key)) { seen.add(key); blocks.push({ type: 'intelligent_page', schema: 'tcode.intelligent-page.v1', page_id: item.page_id,
          revision_id: item.revision_id, session_id: item.session_id, title: item.title, fallback: `交互页面：${item.title}` }); }
      }
      continue;
    }
    if (blockType === 'canvas') {
      if (item.schema === 'tcode.canvas.v1' && item.kind === 'svg'
        && typeof item.canvas_id === 'string' && /^canvas_[0-9a-f]{32}$/.test(item.canvas_id)
        && typeof item.revision_id === 'string' && /^rev_[0-9a-f]{32}$/.test(item.revision_id)
        && typeof item.session_id === 'string' && item.session_id.length > 0 && item.session_id.length <= 200
        && typeof item.title === 'string' && item.title.length > 0 && item.title.length <= 256) {
        const key = `canvas:${item.canvas_id}:${item.revision_id}`;
        if (!seen.has(key)) {
          seen.add(key);
          blocks.push({ type: 'canvas', schema: 'tcode.canvas.v1', kind: 'svg', canvas_id: item.canvas_id,
            revision_id: item.revision_id, session_id: item.session_id, title: item.title, fallback: `可交互 SVG：${item.title}` });
        }
      }
      continue;
    }
    if (blockType === 'research_compare') {
      if (item.schema === 'tcode.research-compare.v1' && typeof item.run_id === 'string'
        && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(item.run_id) && !item.run_id.includes('..')
        && typeof item.result_digest === 'string' && /^[a-f0-9]{64}$/.test(item.result_digest)
        && typeof item.session_id === 'string' && item.session_id.length > 0 && item.session_id.length <= 200) {
        const key = `compare:${item.run_id}:${item.result_digest}:${item.session_id}`;
        if (!seen.has(key)) { seen.add(key); blocks.push({ type: 'research_compare', schema: 'tcode.research-compare.v1',
          run_id: item.run_id, result_digest: item.result_digest, session_id: item.session_id,
          title: '候选比较与证据核对', fallback: '候选比较：' + item.run_id }); }
      }
      continue;
    }
    if (blockType === 'result_selection') {
      if (item.schema === 'tcode.result-selection.v1' && typeof item.result_id === 'string' && Array.isArray(item.items) && item.items.length <= 20) {
        const items = item.items.filter((i: any) => i && typeof i.item_id === 'string' && typeof i.label === 'string').map((i: any) => ({item_id: i.item_id, label: i.label.slice(0, 256), metrics: i.metrics || {}}));
        const source = item.source as Record<string, unknown> | undefined;
        if (items.length) blocks.push({type: 'result_selection', schema: 'tcode.result-selection.v1', result_id: item.result_id, items, fallback: (source?.app_id === 'candidate_compare' ? '已选择研究候选（未代表采用或验证）：' : '已选用：') + items.map((i: any) => i.label).join('、')});
      }
      continue;
    }
    if (blockType === 'job_summary') {
      const title = resultText(item.title, 512);
      if (item.schema !== 'tcode.job-summary.v1' || !title) continue;
      const metrics: JobMetric[] = [];
      const metricKeys = new Set<string>();
      for (const candidate of Array.isArray(item.metrics) ? item.metrics.slice(0, 64) : []) {
        if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
        const metric = candidate as Record<string, unknown>;
        const key = resultText(metric.key, 128);
        const label = resultText(metric.label, 256);
        if (!key || !label || metricKeys.has(key)) continue;
        metricKeys.add(key);
        const unit = resultText(metric.unit, 80);
        metrics.push({
          key,
          label,
          value: resultScalar(metric.value),
          ...(unit ? { unit } : {}),
        });
      }
      const jobId = resultText(item.job_id, 200);
      const key = `job_summary:${jobId || title}`;
      if (seen.has(key)) continue;
      seen.add(key);
      blocks.push({
        type: 'job_summary',
        schema: 'tcode.job-summary.v1',
        job_id: jobId,
        result_type: resultText(item.result_type, 160),
        status: resultText(item.status, 40),
        title,
        description: resultText(item.description, 4000),
        metrics,
        fallback: resultText(item.fallback, 4000) || title,
      });
      continue;
    }
    if (blockType === 'data_table') {
      const id = resultText(item.id, 128);
      const title = resultText(item.title, 512);
      if (item.schema !== 'tcode.table.v1' || !id || !title || !Array.isArray(item.columns)) continue;
      const columns: DataTableColumn[] = [];
      const columnKeys = new Set<string>();
      for (const candidate of item.columns.slice(0, 64)) {
        if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
        const column = candidate as Record<string, unknown>;
        const key = resultText(column.key, 128);
        const columnTitle = resultText(column.title, 256);
        const columnType = resultText(column.type, 32) as DataTableColumn['type'];
        if (
          !key
          || !columnTitle
          || columnKeys.has(key)
          || !['string', 'integer', 'number', 'boolean'].includes(columnType)
        ) continue;
        columnKeys.add(key);
        const unit = resultText(column.unit, 80);
        columns.push({ key, title: columnTitle, type: columnType, ...(unit ? { unit } : {}) });
      }
      if (columns.length === 0) continue;
      const rows: Record<string, ResultScalar>[] = [];
      for (const candidate of Array.isArray(item.rows) ? item.rows.slice(0, 100) : []) {
        if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
        const source = candidate as Record<string, unknown>;
        const row: Record<string, ResultScalar> = {};
        for (const column of columns) row[column.key] = resultScalar(source[column.key]);
        rows.push(row);
      }
      const primaryKey = columnKeys.has(resultText(item.primary_key, 128))
        ? resultText(item.primary_key, 128)
        : columns[0].key;
      const totalRowsValue = Number(item.total_rows);
      const totalRows = Number.isFinite(totalRowsValue)
        ? Math.max(rows.length, Math.trunc(totalRowsValue))
        : rows.length;
      const defaultSort = item.default_sort && typeof item.default_sort === 'object' && !Array.isArray(item.default_sort)
        ? item.default_sort as Record<string, unknown>
        : null;
      const sortKey = resultText(defaultSort?.key, 128);
      const sortDirection = resultText(defaultSort?.direction, 8);
      const seenKey = `data_table:${id}`;
      if (seen.has(seenKey)) continue;
      seen.add(seenKey);
      blocks.push({
        type: 'data_table',
        schema: 'tcode.table.v1',
        id,
        title,
        columns,
        rows,
        total_rows: totalRows,
        primary_key: primaryKey,
        ...(columnKeys.has(sortKey) && (sortDirection === 'asc' || sortDirection === 'desc')
          ? { default_sort: { key: sortKey, direction: sortDirection } }
          : {}),
        fallback: resultText(item.fallback, 4000),
      });
      continue;
    }
    if (blockType === 'artifact') {
      const artifactId = String(item.artifact_id || '').trim();
      const uri = String(item.uri || '').trim();
      const mediaType = String(item.media_type || 'application/octet-stream').trim();
      if (
        !/^art_[0-9a-f]{20,64}$/.test(artifactId)
        || !uri.startsWith('/api/files/download?')
        || !mediaType
      ) continue;
      const key = `artifact:${artifactId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      blocks.push({
        type: 'artifact',
        artifact_id: artifactId,
        uri: uri.slice(0, 4096),
        media_type: mediaType.slice(0, 200),
        title: String(item.title || 'Artifact').slice(0, 500),
        fallback: String(item.fallback || 'Artifact').slice(0, 20_000),
      });
      continue;
    }
    if (blockType === 'mcp_app') {
      const sessionId = String(item.session_id || '').trim();
      const serverName = String(item.server_name || '').trim();
      const toolName = String(item.tool_name || '').trim();
      const resourceUri = String(item.resource_uri || '').trim();
      const identity = `${serverName}\0${toolName}\0${resourceUri}`;
      const legacyAppId = `legacy_${identity}`;
      const appId = String(item.app_id || '').trim();
      if (
        !sessionId
        || !serverName
        || !toolName
        || !resourceUri.startsWith('ui://')
      ) continue;
      const key = `mcp_app:${sessionId}:${appId || legacyAppId}:${resourceUri}`;
      if (seen.has(key)) continue;
      seen.add(key);
      blocks.push({
        type: 'mcp_app',
        schema: 'tcode.mcp-app.v2',
        app_id: appId.slice(0, 200),
        session_id: sessionId,
        server_name: serverName.slice(0, 200),
        tool_name: toolName.slice(0, 300),
        resource_uri: resourceUri.slice(0, 2000),
        mime_type: 'text/html;profile=mcp-app',
        tool_input: item.tool_input && typeof item.tool_input === 'object' && !Array.isArray(item.tool_input)
          ? item.tool_input as Record<string, unknown>
          : {},
        tool_result: item.tool_result && typeof item.tool_result === 'object' && !Array.isArray(item.tool_result)
          ? item.tool_result as Record<string, unknown>
          : { content: [] },
        fallback: String(item.fallback || '交互界面暂不可用').slice(0, 2000),
      });
      continue;
    }
    if (blockType !== 'image') continue;
    const assetId = String(item.asset_id || '').trim();
    const sessionId = String(item.session_id || '').trim();
    const path = String(item.path || '').trim().replace(/^\/+/, '');
    const mimeType = String(item.mime_type || '').trim().toLowerCase();
    if (
      !/^[A-Za-z0-9_-]{8,96}$/.test(assetId)
      || !sessionId
      || !path
      || !SUPPORTED_IMAGE_MIME_TYPES.has(mimeType)
    ) continue;
    const key = `${sessionId}:${assetId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const sha256 = String(item.sha256 || '').trim().toLowerCase();
    blocks.push({
      type: 'image',
      asset_id: assetId,
      session_id: sessionId,
      path,
      mime_type: mimeType,
      size: Math.max(0, Number(item.size || 0) || 0),
      ...(/^[0-9a-f]{64}$/.test(sha256) ? { sha256 } : {}),
      alt: String(item.alt || '').trim() || path.split('/').pop() || '图片',
      caption: String(item.caption || '').trim(),
    });
  }
  return blocks;
};

export const getPresentedImageUrl = (block: ImageContentBlock) => {
  const search = new URLSearchParams({ session_id: block.session_id });
  return `/api/media/${encodeURIComponent(block.asset_id)}?${search.toString()}`;
};
