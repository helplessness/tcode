import { describe, expect, it } from 'vitest';

import { formatAsyncCompletionNotice, formatAsyncResultSummary, parseAsyncResultSummary } from './asyncResultSummary';

describe('async result summary', () => {
  it('formats a collected job payload into a concise scientific task summary', () => {
    const raw = JSON.stringify({
      job_id: 'job_gnina-mcp_75d600bda21a',
      status: 'completed',
      validated: true,
      summary: { validated_count: 1, rejected_count: 0 },
      target_result: {
        structuredContent: {
          result_code: true,
          run_id: 'local-4a899b0006',
          output_bundle: {
            kind: 'artifact_bundle',
            bundle_id: 'bundle_24170815d1034f6c94818b25b5851832',
          },
          output_manifest: { total_files: 5, total_size: 510626 },
        },
      },
    });

    const result = formatAsyncResultSummary(raw);

    expect(result.structured).toBe(true);
    expect(result.text).toContain('状态：已完成 · 结果校验通过');
    expect(result.text).toContain('job_gnina-mcp_75d600bda21a');
    expect(result.text).toContain('local-4a899b0006');
    expect(result.text).toContain('5 个文件');
    expect(result.text).toContain('498.7 KB');
    expect(result.text).not.toContain('structuredContent');
  });

  it('parses JSON embedded in surrounding result text', () => {
    expect(parseAsyncResultSummary('result: {"result_code":true,"run_id":"run-1"} done'))
      .toMatchObject({ result_code: true, run_id: 'run-1' });
  });

  it('keeps plain summaries readable and bounded', () => {
    const result = formatAsyncResultSummary('plain result');
    expect(result).toMatchObject({ text: 'plain result', structured: false });
  });

  it('unwraps MCP text envelopes while retaining the original detail', () => {
    const raw = JSON.stringify({ content: [{ type: 'text', text: JSON.stringify({
      job_id: 'job-gnina', status: 'completed', validated: true,
      target_result: { content: [{ type: 'text', text: JSON.stringify({run_id: 'run-1'}) }] },
    }) }] });
    const formatted = formatAsyncResultSummary(raw);
    expect(formatted.text).toContain('结果校验通过');
    expect(formatted.text).toContain('Run：run-1');
    const notice = formatAsyncCompletionNotice({
      status: 'completed', target_server: 'gnina-mcp', result_summary: raw,
    }, '受体与 PQQ 对接');
    expect(notice.title).toBe('GNINA 分子对接已完成');
    expect(notice.body).toContain('任务：受体与 PQQ 对接\n软件：GNINA');
    expect(notice.body).toContain('返回产物校验通过');
    expect(notice.body).not.toContain('job-gnina');
    expect(notice.details).toBe(raw);
  });

  it('does not leak truncated JSON or invent validation success', () => {
    const raw = '{"content":[{"type":"text","text":"{\\"job_id\\":\\"job_gnina-mcp_1';
    const notice = formatAsyncCompletionNotice({status: 'completed', target_server: 'gnina-mcp', result_summary: raw}, 'New Chat');
    expect(notice.body).toContain('任务：分子对接');
    expect(notice.body).not.toContain('校验通过');
    expect(notice.body).not.toContain('job_id');
    expect(formatAsyncResultSummary(raw).text).not.toContain('content');
    expect(notice.details).toBe(raw);
  });

  it('keeps failure, cancellation and validation failure distinct', () => {
    expect(formatAsyncCompletionNotice({status: 'failed', target_server: 'gnina-mcp', result_summary: '{"validated":true}', error_summary: '受体文件缺失'}).body).toContain('受体文件缺失');
    expect(formatAsyncCompletionNotice({status: 'cancelled', target_server: 'gnina-mcp'}).title).toBe('GNINA 分子对接已取消');
    expect(formatAsyncCompletionNotice({status: 'completed', result_summary: '{"validated":false}'}).body).toContain('产物校验未通过');
    expect(formatAsyncCompletionNotice({status: 'completed'}).body).toContain('软件：未记录');
  });

  it('labels broker timeouts without treating them as remote calculation failure or success', () => {
    for (const failure of [
      { status: 'timed_out', job_id: 'job-test', process_id: 'process-test' },
      { failure_kind: 'local_wait_timeout', remote_outcome: 'unknown' },
    ]) {
      const notice = formatAsyncCompletionNotice({
        status: 'failed', target_server: 'homology-mcp', error_summary: JSON.stringify(failure),
      });
      expect(notice.title).toContain('本地等待超时');
      expect(notice.body).toContain('远端计算是否结束及结果尚未确认');
      expect(notice.body).toContain('不要直接重提');
      expect(notice.body).not.toContain('计算已完成');
    }
    expect(formatAsyncCompletionNotice({
      status: 'failed', error_summary: JSON.stringify({ status: 'timed_out', message: 'remote computation exceeded its timeout' }),
    }).title).not.toContain('本地等待超时');
  });
});
