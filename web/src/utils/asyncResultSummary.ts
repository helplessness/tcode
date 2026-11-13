export type AsyncResultSummaryView = {
  text: string;
  raw: string;
  structured: boolean;
};

type JsonRecord = Record<string, any>;

const asRecord = (value: unknown): JsonRecord | null => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonRecord
    : null
);

export const parseAsyncResultSummary = (value?: string): JsonRecord | null => {
  const raw = String(value || '').trim();
  if (!raw) return null;
  try {
    return asRecord(JSON.parse(raw));
  } catch {
    const start = raw.indexOf('{');
    const end = raw.lastIndexOf('}');
    if (start < 0 || end <= start) return null;
    try {
      return asRecord(JSON.parse(raw.slice(start, end + 1)));
    } catch {
      return null;
    }
  }
};

const nestedRecord = (value: unknown): JsonRecord | null => {
  const record = asRecord(value);
  if (record) return record;
  if (typeof value === 'string') return parseAsyncResultSummary(value);
  return null;
};

const firstRecord = (...values: unknown[]) => {
  for (const value of values) {
    const record = nestedRecord(value);
    if (record) return record;
  }
  return null;
};

// MCP may wrap a JSON result in text content, including the collected target result.
const unwrapResult = (value: JsonRecord): JsonRecord => {
  let current = value;
  for (let depth = 0; depth < 5; depth++) {
    const block = Array.isArray(current.content)
      ? current.content.find((item: any) => item?.type === 'text' && nestedRecord(item.text))
      : undefined;
    const next = block ? nestedRecord(block.text) : null;
    if (!next) break;
    current = { ...current, ...next, content: next.content };
  }
  return current;
};

const formatBytes = (value: unknown) => {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
};

const statusLabel = (value: unknown) => {
  const status = String(value || '').trim().toLowerCase();
  return ({
    completed: '已完成',
    done: '已完成',
    failed: '失败',
    error: '失败',
    cancelled: '已取消',
    canceled: '已取消',
    running: '运行中',
    queued: '排队中',
  } as Record<string, string>)[status] || String(value || '').trim();
};

const compactText = (value: string, limit = 600) => {
  const text = String(value || '').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
};

export const formatAsyncResultSummary = (
  value?: string,
  fallback?: string,
): AsyncResultSummaryView => {
  const raw = String(value || fallback || '').trim();
  if (!raw) return { text: '', raw: '', structured: false };

  const source = parseAsyncResultSummary(raw);
  if (!source) {
    if (/^[\[{]/.test(raw)) {
      return { text: '结果详情未能解析，请打开原会话查看。', raw, structured: false };
    }
    return { text: compactText(raw), raw, structured: false };
  }

  const parsed = unwrapResult(source);
  const target = unwrapResult(firstRecord(parsed.target_result) || {});
  const structured = firstRecord(
    target.structuredContent,
    target.data,
    parsed.structuredContent,
    parsed.data,
  ) || {};
  const outputBundle = firstRecord(
    parsed.output_bundle,
    structured.output_bundle,
    target.output_bundle,
  ) || {};
  const manifest = firstRecord(
    parsed.output_manifest,
    structured.output_manifest,
    target.output_manifest,
  ) || {};
  const summary = firstRecord(parsed.summary) || {};

  const resultCode = typeof structured.result_code === 'boolean'
    ? structured.result_code
    : typeof parsed.result_code === 'boolean'
      ? parsed.result_code
      : undefined;
  const validated = typeof parsed.validated === 'boolean'
    ? parsed.validated
    : typeof structured.validated === 'boolean'
      ? structured.validated
      : resultCode;
  const status = statusLabel(parsed.status || structured.status || target.status);
  const jobId = String(parsed.job_id || '').trim();
  const runId = String(parsed.run_id || structured.run_id || target.run_id || '').trim();
  const bundleId = String(outputBundle.bundle_id || '').trim();
  const totalFiles = Number(manifest.total_files || outputBundle.total_files || 0);
  const totalSize = formatBytes(manifest.total_size || outputBundle.total_size);
  const validatedCount = Number(summary.validated_count || 0);
  const rejectedCount = Number(summary.rejected_count || 0);
  const outputPath = String(parsed.output_path || structured.output_path || '').trim();

  const headline = [
    status ? `状态：${status}` : '',
    validated === true ? '结果校验通过' : validated === false ? '结果校验未通过' : '',
  ].filter(Boolean).join(' · ');
  const lines = [
    headline,
    jobId ? `Job：${jobId}` : '',
    runId ? `Run：${runId}` : '',
    bundleId
      ? `输出包：${bundleId}${totalFiles > 0 ? ` · ${totalFiles} 个文件` : ''}${totalSize ? ` · ${totalSize}` : ''}`
      : totalFiles > 0
        ? `输出：${totalFiles} 个文件${totalSize ? ` · ${totalSize}` : ''}`
        : '',
    (validatedCount > 0 || rejectedCount > 0)
      ? `产物校验：${validatedCount} 项通过${rejectedCount > 0 ? `，${rejectedCount} 项被拒绝` : ''}`
      : '',
    outputPath ? `输出目录：${outputPath}` : '',
  ].filter(Boolean);

  return {
    text: lines.length > 0 ? lines.join('\n') : '结果详情已回传，请打开原会话查看。',
    raw,
    structured: true,
  };
};

export const formatAsyncCompletionNotice = (event: {
  status: string;
  target_server?: string | null;
  target_tool?: string | null;
  result_summary?: string | null;
  error_summary?: string | null;
}, sessionTitle?: string) => {
  const softwareTasks: Record<string, [string, string]> = {
    'gnina-mcp': ['GNINA', '分子对接'],
    'boltz-mcp': ['Boltz', '结构预测'],
    'ligandmpnn-mcp': ['LigandMPNN', '序列设计'],
    'placer-mcp': ['PLACER', '结构建模'],
    'plip-mcp': ['PLIP', '相互作用分析'],
    'rfdiffusion-mcp': ['RFdiffusion', '蛋白质设计'],
    'evolvepro-mcp': ['EvolvePro', '实验进化分析'],
  };
  const [software, task] = softwareTasks[event.target_server || '']
    || [event.target_server?.replace(/-mcp$/, '') || '', '后台任务'];
  const failure = parseAsyncResultSummary(event.error_summary || '');
  const localWaitTimeout = event.status === 'failed' && Boolean(failure && (
    failure.failure_kind === 'local_wait_timeout'
    || (failure.status === 'timed_out' && failure.job_id && failure.process_id)
  ));
  const status = localWaitTimeout ? '本地等待超时' : event.status === 'failed' ? '失败' : event.status === 'cancelled' ? '已取消' : '已完成';
  const formatted = formatAsyncResultSummary(
    event.status === 'failed' ? event.error_summary || event.result_summary || '' : event.result_summary || '',
    event.error_summary || '',
  );
  const meaningfulTitle = sessionTitle && !/^(new chat|新对话|新会话|当前任务)$/i.test(sessionTitle.trim())
    ? compactText(sessionTitle, 100) : '';
  const result = localWaitTimeout
    ? '本地等待已超时，远端计算是否结束及结果尚未确认。请先核查原任务，不要直接重提。'
    : event.status === 'failed'
    ? (formatted.text && !formatted.structured ? formatted.text : '运行失败，请查看原会话中的错误详情。')
    : event.status === 'cancelled'
      ? '任务已取消，可打开原会话查看记录。'
      : formatted.text.includes('结果校验未通过')
        ? '计算已结束，但返回产物校验未通过，请查看原会话。'
        : formatted.text.includes('结果校验通过')
          ? '返回产物校验通过，可打开原会话查看结果。'
          : '计算已完成，可打开原会话查看结果与校验状态。';
  return {
    title: `${software ? `${software} ` : ''}${task}${status}`,
    body: [`任务：${meaningfulTitle || task}`, software ? `软件：${software}` : '软件：未记录', result].join('\n'),
    details: formatted.raw,
  };
};
