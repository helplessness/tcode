import { getResearchContinuation, listResearchExperiments, listResearchHypotheses } from '../api/research';

// null 表示尚未加载或读取失败，不能当成“没有内容”隐藏已有入口。
export function researchModuleVisible(count: number | null | undefined, selected = false) {
  return selected || count == null || count > 0;
}

export async function loadResearchNavigation(researchId: string, workspaceId?: string) {
  const [hypotheses, experiments, continuation] = await Promise.allSettled([
    listResearchHypotheses(researchId, workspaceId),
    listResearchExperiments(researchId, workspaceId),
    getResearchContinuation(researchId, workspaceId),
  ]);
  return {
    hypotheses: hypotheses.status === 'fulfilled' ? hypotheses.value.hypotheses : null,
    experiments: experiments.status === 'fulfilled' ? experiments.value.experiments : null,
    continuation: continuation.status === 'fulfilled' ? continuation.value.continuation : null,
    errors: [hypotheses, experiments, continuation].flatMap(result => result.status === 'rejected'
      ? [result.reason instanceof Error ? result.reason.message : '研究模块读取失败'] : []),
  };
}
