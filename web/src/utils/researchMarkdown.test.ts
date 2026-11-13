import {it,expect} from 'vitest';
import {readableResearchMarkdown,readableResearchExcerpt,researchIndexTitle} from './researchMarkdown';
it('preserves a complete Markdown table and resolves full and shortened references',()=>{
  const md='最近进展\n\n| 实验 | 标题 | parent_experiment_id | 状态 |\n|---|---|---|---|\n| `exp-abc…789` | 基线 | `null` | completed / frozen |\n| `exp-def` | 后续 | `exp-abc…789` | planned |';
  const result=readableResearchMarkdown(md,new Map([['exp-abc123789','基线']]));
  expect(result).toContain('| 标题 | 上级实验 | 状态 |\n| --- | --- | --- |');
  expect(result).toContain('| 后续 | 基线 | 待执行 |');
  expect(result).not.toContain('exp-');
});
it('does not guess an ambiguous shortened name or truncate Markdown',()=>{
  const text='**进展**\n\n'+ '正文。'.repeat(300)+'\n\n- `hyp-unknown`\n- `exp-a…x`';
  const result=readableResearchMarkdown(text,new Map([['exp-abcx','甲'],['exp-abdx','乙']]));
  expect(result).toContain('**进展**\n\n');
  expect(result).toContain('假说（未记录名称）');
  expect(result).toContain('相关记录（未记录名称）');
  expect(result.length).toBeGreaterThan(520);
});

it('recovers complete rows from flattened legacy Markdown and flags a cut row',()=>{
  const value='进展： | 实验 | 标题 | parent_experiment_id | 状态 | |---|---|---|---| | `exp-a` | 基线 | `null` | completed | | `exp-b` | 后续 | `exp-a`';
  const result=readableResearchMarkdown(value);
  expect(result).toContain('| 标题 | 上级实验 | 状态 |\n| --- | --- | --- |');
  expect(result).toContain('| 基线 | `无` | 已完成 |');
  expect(result).toContain('部分历史摘要已被截断');
  expect(result).not.toContain('exp-');
});

it('labels known research tool references in projected historical prose',()=>{
  expect(readableResearchMarkdown('调用 research_workspace 的 list_projects 和 list_research_branches。')).toBe('调用 研究工作区 的 查看课题 和 查看实验分支。');
});

it('projects serialized search records without leaking raw fields or opaque page references',()=>{
  const content='对象类型：`event` ```json {"actor_id":"admin","payload":{"preview":"打开 page_abcdef 和 rev_123456"}}';
  expect(readableResearchExcerpt(content)).toBe('打开 交互界面 和 已保存版本');
  expect(readableResearchExcerpt('"failed_paths": [], "origin_pack_ids": ["pack-abc"]',true)).not.toContain('origin_pack_ids');
  expect(readableResearchExcerpt('```json {"actor_id":"admin"')).not.toContain('actor_id');
  expect(researchIndexTitle('event · rev- | event > 结构化字段')).toBe('研究资料');
  expect(readableResearchExcerpt('原文\n\n| 名称 | 数值 |')).toContain('| 名称 | 数值 |');
});
