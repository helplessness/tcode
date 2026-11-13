import { describe, expect, it } from 'vitest';
import type { WikiClaim } from '../api/paperWiki';
import {
  WIKI_CLAIM_EVIDENCE_PREFIX,
  buildWikiAgentDisplayContent,
} from './wikiEvidenceDisplay';

const claim = (overrides: Partial<WikiClaim> = {}): WikiClaim => ({
  claim_id: 'claim-1',
  statement: 'A107N retains strong inhibition.',
  evidence_ids: ['evidence-1'],
  support_status: 'supported',
  confidence: 0.9,
  origin: 'paper_library',
  ...overrides,
});

describe('Wiki 综合结论 Evidence 展示', () => {
  it('只在只读综合结论中添加可点击的 Claim 标记', () => {
    const content = [
      'A107N retains strong inhibition.',
      '',
      '## 综合结论',
      '',
      '- A107N retains strong inhibition.',
      '',
      '## 其他信息',
      '',
      '- A107N retains strong inhibition.',
    ].join('\n');

    const rendered = buildWikiAgentDisplayContent(content, [claim()]);

    expect(rendered).toContain(
      `- A107N retains strong inhibition. [evidence:${WIKI_CLAIM_EVIDENCE_PREFIX}claim-1]`,
    );
    expect(rendered.match(/\[evidence:/g)).toHaveLength(1);
  });

  it('不为没有 Evidence 的结论生成来源入口', () => {
    const content = '## 综合结论\n\n- 用户补充的未验证判断。';
    const rendered = buildWikiAgentDisplayContent(content, [claim({
      claim_id: 'claim-note',
      statement: '用户补充的未验证判断。',
      evidence_ids: [],
      support_status: 'manual_unverified',
      origin: 'user_note',
    })]);

    expect(rendered).toBe(content);
    expect(rendered).not.toContain('[evidence:');
  });

  it('不会重复添加已有的展示标记', () => {
    const content = '## 综合结论\n\n- A107N retains strong inhibition. [evidence:evidence-1]';
    expect(buildWikiAgentDisplayContent(content, [claim()])).toBe(content);
  });
});

