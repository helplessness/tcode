import { describe, expect, it } from 'vitest';
import { getPresentedImageUrl, sanitizeMessageContentBlocks, groupCanvasResults, type CanvasContentBlock } from './contentBlocks';

describe('structured message content blocks', () => {
  it('groups same-reply named drafts, keeps the last result and preserves distinct drawings/sessions', () => {
    const canvas = (id:string,title='装置',session='chat',revision='r') => ({type:'canvas',canvas_id:id,title,session_id:session,revision_id:revision} as CanvasContentBlock);
    const groups = groupCanvasResults([canvas('a'),canvas('b'),canvas('a','装置','chat','new'),canvas('c','另一张图'),canvas('d','装置','other')]);
    expect(groups.map(group=>group.map(b=>b.canvas_id))).toEqual([['b','a'],['c'],['d']]);
    expect(groups[0][1].revision_id).toBe('new');
  });
  it('keeps bound comparison references and rejects unsafe or duplicate identities', () => {
    const comparison = { type: 'research_compare', schema: 'tcode.research-compare.v1', run_id: 'wf-real',
      result_digest: 'a'.repeat(64), session_id: 'session-1', title: '候选比较' };
    expect(sanitizeMessageContentBlocks([comparison, comparison, { ...comparison, run_id: '../other' },
      { ...comparison, result_digest: '' }, { ...comparison, session_id: '' }])).toHaveLength(1);
  });
  it('keeps valid registered images and drops unsupported blocks', () => {
    const blocks = sanitizeMessageContentBlocks([
      {
        type: 'image',
        asset_id: 'img_12345678',
        session_id: 'session-1',
        path: '/artifacts/figure.png',
        mime_type: 'image/png',
        size: 42,
        caption: 'Figure 1',
      },
      { type: 'image', asset_id: 'bad', mime_type: 'image/svg+xml' },
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe('image');
    if (blocks[0].type !== 'image') throw new Error('expected image block');
    expect(blocks[0].path).toBe('artifacts/figure.png');
    expect(getPresentedImageUrl(blocks[0])).toBe(
      '/api/media/img_12345678?session_id=session-1',
    );
  });

  it('accepts stable ui:// MCP Apps descriptors without persisted tokens', () => {
    const blocks = sanitizeMessageContentBlocks([
      {
        type: 'mcp_app',
        schema: 'tcode.mcp-app.v2',
        app_id: 'app_0123456789abcdef01234567',
        session_id: 'session-1',
        server_name: 'charts',
        tool_name: 'render',
        resource_uri: 'ui://chart/main',
      },
      {
        type: 'mcp_app',
        session_id: 'session-1',
        server_name: 'charts',
        tool_name: 'render',
        resource_uri: 'https://example.com/unsafe',
      },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      type: 'mcp_app',
      schema: 'tcode.mcp-app.v2',
      app_id: 'app_0123456789abcdef01234567',
      resource_uri: 'ui://chart/main',
      mime_type: 'text/html;profile=mcp-app',
    });
  });

  it('keeps only internal structured Artifact download descriptors', () => {
    const blocks = sanitizeMessageContentBlocks([
      {
        type: 'artifact',
        artifact_id: 'art_0123456789abcdef01234567',
        uri: '/api/files/download?path=output.csv&workspace_id=ws-1',
        media_type: 'text/csv',
        title: 'output.csv',
      },
      {
        type: 'artifact',
        artifact_id: 'art_abcdefabcdefabcdefabcd',
        uri: 'https://example.com/output.csv',
      },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      type: 'artifact',
      title: 'output.csv',
      media_type: 'text/csv',
    });
  });

  it('sanitizes TMCP summaries and ranking tables for session persistence', () => {
    const blocks = sanitizeMessageContentBlocks([
      {
        type: 'job_summary',
        schema: 'tcode.job-summary.v1',
        job_id: 'job-gnina-1',
        result_type: 'molecular_docking.ranking',
        status: 'completed',
        title: '分子对接完成',
        description: '已生成候选构象排名',
        metrics: [
          { key: 'best_score', label: '最佳亲和力', value: -9.4, unit: 'kcal/mol' },
          { key: 'invalid', label: '异常值', value: { nested: true } },
        ],
      },
      {
        type: 'data_table',
        schema: 'tcode.table.v1',
        id: 'docking-ranking',
        title: '对接排名',
        columns: [
          { key: 'rank', title: '排名', type: 'integer' },
          { key: 'affinity', title: '亲和力', type: 'number', unit: 'kcal/mol' },
        ],
        rows: [{ rank: 1, affinity: -9.4, private_path: '/remote/internal' }],
        total_rows: 1,
        primary_key: 'rank',
        default_sort: { key: 'affinity', direction: 'asc' },
      },
    ]);

    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({
      type: 'job_summary',
      title: '分子对接完成',
      metrics: [
        { key: 'best_score', value: -9.4 },
        { key: 'invalid', value: null },
      ],
    });
    expect(blocks[1]).toMatchObject({
      type: 'data_table',
      default_sort: { key: 'affinity', direction: 'asc' },
      rows: [{ rank: 1, affinity: -9.4 }],
    });
  });
});
