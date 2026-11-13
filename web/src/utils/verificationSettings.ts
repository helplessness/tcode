export type VerificationMode = 'light' | 'standard' | 'strict';
export interface VerificationSettings {
  enabled: boolean;
  mode: VerificationMode;
  effective?: {
    state: 'disabled' | 'partial' | 'enabled';
    stages: Record<string, { enabled: boolean; reason: string }>;
    policy: { max_tokens: number; timeout: number; repairs: number };
  };
}

export const verificationStageLabels: Record<string, string> = {
  planner: '任务验收规划', guard: '最终结果验证', worker_review: 'Worker 自动审查',
  retry_review: '工具重试审核', workflow_final: 'Workflow 最终追加验证',
};

export function verificationStageStatus(stage?: { enabled: boolean; reason: string }): string {
  if (!stage) return '状态未知，请刷新或检查后端版本';
  if (stage.enabled) return '已启用，按任务需要触发';
  if (stage.reason === 'account_disabled') return '账户已关闭';
  if (stage.reason === 'light_mode') return '轻量档跳过模型规划';
  if (stage.reason === 'deployment_disabled') return '服务端未启用';
  return '未启用';
}

export function verificationEffectiveLabel(settings?: VerificationSettings): string {
  if (!settings?.effective) return '实际生效状态未知';
  return { disabled: '自动验证已关闭', partial: '自动验证部分生效', enabled: '自动验证已启用' }[settings.effective.state] || '实际生效状态未知';
}
