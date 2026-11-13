import { describe, expect, it, vi } from 'vitest';
import { loadResearchNavigation, researchModuleVisible } from './researchNavigation';
import { getResearchContinuation, listResearchExperiments, listResearchHypotheses } from '../api/research';

vi.mock('../api/research', () => ({getResearchContinuation:vi.fn(),listResearchExperiments:vi.fn(),listResearchHypotheses:vi.fn()}));

describe('研究模块渐进展示', () => {
  it('仅确认无内容时收起，首次主动进入与历史记录仍可访问', () => {
    expect(researchModuleVisible(0)).toBe(false);
    expect(researchModuleVisible(1)).toBe(true);
    expect(researchModuleVisible(0, true)).toBe(true);
    expect(researchModuleVisible(null)).toBe(true);
    expect(researchModuleVisible(undefined)).toBe(true);
  });
  it('一个模块失败不抹掉其他内容，也不伪装成零个假说', async () => {
    vi.mocked(listResearchHypotheses).mockRejectedValueOnce(new Error('暂时无法读取'));
    vi.mocked(listResearchExperiments).mockResolvedValueOnce({count:0,experiments:[]});
    vi.mocked(getResearchContinuation).mockResolvedValueOnce({continuation:null});
    const result = await loadResearchNavigation('project','workspace');
    expect(result.hypotheses).toBeNull();
    expect(result.experiments).toEqual([]);
    expect(result.errors).toEqual(['暂时无法读取']);
    expect(researchModuleVisible(result.hypotheses?.length)).toBe(true);
    expect(listResearchHypotheses).toHaveBeenCalledWith('project','workspace');
  });
});
