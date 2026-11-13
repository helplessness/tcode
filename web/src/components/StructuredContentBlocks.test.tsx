import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';
import StructuredContentBlocks from './StructuredContentBlocks';

describe('StructuredContentBlocks job results', () => {
  it('renders a metric summary and ranking table without exposing undeclared row fields', () => {
    const html = renderToString(() => (
      <StructuredContentBlocks
        blocks={[
          {
            type: 'job_summary',
            schema: 'tcode.job-summary.v1',
            job_id: 'job-1',
            result_type: 'molecular_docking.ranking',
            status: 'completed',
            title: '分子对接完成',
            description: '已生成候选构象排名',
            metrics: [
              { key: 'best_score', label: '最佳亲和力', value: -9.4, unit: 'kcal/mol' },
            ],
            fallback: '分子对接完成',
          },
          {
            type: 'data_table',
            schema: 'tcode.table.v1',
            id: 'ranking',
            title: '对接排名',
            columns: [
              { key: 'rank', title: '排名', type: 'integer' },
              { key: 'affinity', title: '亲和力', type: 'number' },
            ],
            rows: [{ rank: 1, affinity: -9.4 }],
            total_rows: 1,
            primary_key: 'rank',
            default_sort: { key: 'affinity', direction: 'asc' },
            fallback: '第一名亲和力 -9.4',
          },
        ]}
      />
    ));

    expect(html).toContain('分子对接完成');
    expect(html).toContain('最佳亲和力');
    expect(html).toContain('kcal/mol');
    expect(html).toContain('对接排名');
    expect(html).toContain('显示 ');
    expect(html).toContain(' / ');
    expect(html).toContain(' 条');
    expect(html).toContain('-9.4');
  });

  it('contains MCP App loading inside the structured result block', () => {
    const html = renderToString(() => (
      <StructuredContentBlocks
        blocks={[{
          type: 'mcp_app',
          schema: 'tcode.mcp-app.v2',
          app_id: 'app-1',
          session_id: 'session-1',
          server_name: 'job-protocol-mcp',
          tool_name: 'collect_job_result',
          resource_uri: 'ui://tmcp/job-results/v1',
          mime_type: 'text/html;profile=mcp-app',
          tool_input: {},
          tool_result: {},
          fallback: '交互结果',
        }]}
      />
    ));

    expect(html).toContain('data-testid="mcp-app-loading"');
    expect(html).toContain('正在加载交互界面');
  });
});

it('renders generic selected items without scientific-tool branching', () => {
  const html = renderToString(() => <StructuredContentBlocks blocks={[{
    type: 'result_selection', schema: 'tcode.result-selection.v1', result_id: 'result-1',
    items: [{item_id: 'model-1', label: '候选结构', metrics: {confidence_score: 0.9}}],
    fallback: '已选用：候选结构',
  }]} />);
  expect(html).toContain('已选用：候选结构');
  expect(html).toContain('model-1');
  expect(html).toContain('confidence_score');
});
