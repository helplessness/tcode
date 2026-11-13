import { describe, expect, it } from 'vitest';
import {
  evidenceFieldLabel,
  evidenceKindLabel,
  graphEdgeTypeLabel,
  graphNodeTypeLabel,
  readableSourceLabel,
  sanitizeEvidenceDisplayValue,
  shortDisplayPath,
  stringifyEvidenceItem,
  userFacingEvidenceText,
  internalEvidenceTokenLabel,
} from './evidenceDisplay';

describe('evidence display helpers', () => {
  it('正文隐藏内部标识但保留引用标记和科学标识', () => {
    const content = '研究包 pack-7f99205c100d48d39c4c884a466afafc，paper_library_get_record 已完成。[evidence:ev-123456abcdef] PMID:19004819 DOI:10.1074/jbc.M807718200 A123V';
    const displayed = userFacingEvidenceText(content);
    expect(displayed).not.toMatch(/pack-7f|paper_library_get_record/);
    expect(displayed).toContain('[evidence:ev-123456abcdef]');
    expect(displayed).toContain('PMID:19004819 DOI:10.1074/jbc.M807718200 A123V');
    expect(internalEvidenceTokenLabel('pack-7f99205c100d48d39c4c884a466afafc')).toBe('研究包');
    expect(internalEvidenceTokenLabel('paper_library_get_record')).toBe('读取资料');
    expect(internalEvidenceTokenLabel('A123V')).toBeUndefined();
    expect(internalEvidenceTokenLabel('const model = "paper_library_get_record"')).toBeUndefined();
  });
  it('只展示本地路径的文件名', () => {
    expect(shortDisplayPath('/path/to/page_7.png')).toBe('page_7.png');
    expect(shortDisplayPath('C:\\workspace\\paper\\source.pdf')).toBe('source.pdf');
  });

  it('翻译结构化字段并隐藏引用里的完整路径', () => {
    const presented = sanitizeEvidenceDisplayValue({
      anchor_id: 'anchor-7',
      artifact_refs: { image_path: '/private/work/figure_2.png' },
      stage_id: 'paper_library_ingest',
    });
    expect(presented).toEqual({
      '产物（Artifact）': { '图片（Image）': 'figure_2.png' },
    });
    expect(stringifyEvidenceItem(presented)).not.toContain('/private/work');
    expect(stringifyEvidenceItem(presented)).not.toContain('anchor-7');
    expect(stringifyEvidenceItem(presented)).not.toContain('paper_library_ingest');
  });

  it('以中文（英文）形式展示类型', () => {
    expect(evidenceKindLabel('paper_page_chunk')).toBe('论文页文本块（Paper page chunk）');
    expect(evidenceFieldLabel('record_path')).toBe('记录文件（Record file）');
    expect(graphNodeTypeLabel('artifact')).toBe('产物（Artifact）');
    expect(graphEdgeTypeLabel('cites_anchor')).toBe('引用锚点（Cites anchor）');
  });

  it('隐藏内部编号、工具名，但保留文献标识与科学内容', () => {
    expect(readableSourceLabel('pack-7f99205c100d48d39c4c884a466afafc', '研究资料包')).toBe('研究资料包');
    expect(readableSourceLabel('来源：paper_library_get_record · ev-123456abcdef')).toBe('来源');
    expect(readableSourceLabel('paper:doc-1:fact:1', '论文来源')).toBe('论文来源');
    expect(readableSourceLabel('PMID:19004819 DOI:10.1074/jbc.M807718200 A123V')).toBe('PMID:19004819 DOI:10.1074/jbc.M807718200 A123V');
    expect(readableSourceLabel('Paper-based microfluidic assay')).toBe('Paper-based microfluidic assay');
    expect(evidenceKindLabel('external_harness_source')).toBe('来源证据');
    const output = stringifyEvidenceItem({ evidence_id: 'ev-demo', source_agent: 'paper_library_manager', source: 'PMID:19004819', locator: { page: 2, chunk_id: 'chunk-1' }, tool_name: 'assign_task' });
    expect(output).not.toMatch(/ev-demo|paper_library_manager|chunk-1|assign_task/);
    expect(output).toContain('19004819');
    expect(output).toContain('2');
  });
});
