export const reasoningEffortOptions: Array<'minimal' | 'low' | 'medium' | 'high'> = ['minimal', 'low', 'medium', 'high'];
export const reasoningEffortLabel = (effort: string) => {
  const labels: Record<string, string> = {
    minimal: '极简',
    low: '低',
    medium: '中',
    high: '高',
  };
  return labels[effort] || effort;
};
export const agentDisplayLabel = (agentName: string) => {
  const labels: Record<string, string> = {
    build: '构建模式',
    manager: '团队编排',
    paper_auto: '文献自动协作',
    protein_orchestrator: '蛋白设计团队',
  };
  return labels[agentName] || agentName;
};
