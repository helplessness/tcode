import { describe, expect, it } from 'vitest';
import { publicNarrationText, skillDisplayName, toolDisplayLabel, userFacingTitle } from './userFacingLabels';

describe('user-facing labels', () => {
  it('labels existing skill records without exposing internal names or mutating arguments', () => {
    const args = { name: 'gnina-runner' };
    expect(toolDisplayLabel('skill', args)).toBe('加载技能 · 分子对接');
    expect(toolDisplayLabel('skill', { name: 'plip-runner' })).toBe('加载技能 · 蛋白与配体相互作用分析');
    expect(skillDisplayName('{"name":"boltz-runner"}')).toBe('蛋白结构预测');
    expect(args).toEqual({ name: 'gnina-runner' });
    expect(toolDisplayLabel('skill')).toBe('加载技能');
    for (const value of [{name:'private_skill_123'}, {}, '{invalid', null]) {
      expect(toolDisplayLabel('skill', value)).toBe('加载技能 · 专项技能');
    }
    expect(skillDisplayName({name:'文献整理'})).toBe('文献整理');
    expect(toolDisplayLabel('mcp_boltz_predict', args)).toBe('蛋白结构预测');
    expect(toolDisplayLabel('skill', {name:'foldx-runner'})).toBe('加载技能 · FoldX 稳定性分析');
    expect(toolDisplayLabel('foldx_scan_mcp_server')).toBe('蛋白位点稳定性扫描');
    expect(toolDisplayLabel('foldx_repair_mcp_server')).toBe('蛋白结构修复');
  });
  it('humanizes narration without changing filenames or scientific names', () => {
    expect(publicNarrationText('调用 capability_catalog 和 mcp_job-protocol-mcp_submit_job。'))
      .toBe('调用 查找可用能力 和 提交计算任务。');
    expect(publicNarrationText('调用 private_unknown_tool。', ['private_unknown_tool']))
      .toBe('调用 执行任务。');
    expect(publicNarrationText('检查 pqq_pose2.sdf、read_config.json 和 PLIP，任务尚未受理。'))
      .toBe('检查 pqq_pose2.sdf、read_config.json 和 PLIP，任务尚未受理。');
  });
  it('names homology operations before the generic search label', () => {
    expect(toolDisplayLabel('homology_search_mcp_server')).toBe('蛋白同源序列搜索');
    expect(toolDisplayLabel('mcp_homology-mcp_homology_align_mcp_server')).toBe('蛋白多序列比对');
    expect(toolDisplayLabel('homology_conservation_mcp_server')).toBe('蛋白位点保守性分析');
    expect(toolDisplayLabel('other_search')).toBe('检索资料');
  });
  it('keeps scientific titles but hides opaque fallback identifiers', () => {
    expect(userFacingTitle('pack-7f99205c100d48d39c4c884a466afafc', '研究资料包')).toBe('研究资料包');
    expect(userFacingTitle('1ABC / BRCA1 蛋白分析', '研究资料包')).toBe('1ABC / BRCA1 蛋白分析');
    expect(userFacingTitle('', '未命名任务')).toBe('未命名任务');
    expect(toolDisplayLabel('mcp_boltz_predict')).toBe('蛋白结构预测');
    expect(toolDisplayLabel('private_unknown_tool')).toBe('执行任务');
  });
});
