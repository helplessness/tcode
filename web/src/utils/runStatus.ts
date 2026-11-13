import { toolDisplayLabel } from './userFacingLabels';

function finiteNumber(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : 0;
}

export function formatRuntimeTokenCount(value: unknown): string {
  const tokens = finiteNumber(value);
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K`;
  return `${Math.round(tokens)}`;
}

export function contextCompactionStatusText(event: Record<string, any>): string {
  const status = String(event.status || '').trim().toLowerCase();
  if (status === 'running') {
    const tokens = finiteNumber(event.estimated_tokens);
    const messages = finiteNumber(event.message_count);
    const details = [
      tokens > 0 ? `约 ${formatRuntimeTokenCount(tokens)} tokens` : '',
      messages > 0 ? `${Math.round(messages)} 条消息` : '',
    ].filter(Boolean).join('，');
    return details ? `正在压缩上下文（${details}）` : '正在压缩上下文';
  }

  if (status === 'completed') {
    const original = finiteNumber(event.original_tokens);
    const compressed = finiteNumber(event.compressed_tokens);
    const saved = finiteNumber(event.tokens_saved);
    if (original > 0 && compressed > 0) {
      const savedText = saved > 0 ? `，节省 ${formatRuntimeTokenCount(saved)}` : '';
      return `上下文压缩完成（${formatRuntimeTokenCount(original)} → ${formatRuntimeTokenCount(compressed)}${savedText} tokens），继续思考中`;
    }
    return '上下文压缩完成，继续思考中';
  }

  if (status === 'skipped') return '上下文无需压缩，继续思考中';
  if (status === 'failed' || status === 'error') return '上下文压缩失败，正在恢复';
  return '正在处理上下文';
}

export function runtimeActivityLabel(value: unknown): string {
  const kind = String(value || '').trim().toLowerCase();
  if (!kind) return '';
  if (kind === 'turn_started' || kind === 'agent_stream_started') return '本轮执行已启动';
  if (kind === 'stream_item') return '模型返回了新消息';
  if (kind.startsWith('subagent:completed')) return '子 Agent 已完成';
  if (kind.startsWith('subagent:running')) return '子 Agent 正在执行';
  if (kind.startsWith('subagent:failed')) return '子 Agent 执行失败';
  if (kind.startsWith('tool_progress:')) return '工具状态已更新';
  if (kind.startsWith('llm_progress:running')) return '主模型正在生成回答';
  if (kind.startsWith('llm_progress:completed')) return '主模型已返回回答';
  if (kind.startsWith('llm_progress:timeout')) return '主模型响应超时';
  if (kind.startsWith('process_note:')) return '执行过程已更新';
  if (kind.startsWith('context_compaction:')) return '上下文压缩状态已更新';
  return kind;
}

function streamPhaseLabel(value: unknown): string {
  const phase = String(value || '').trim().toLowerCase();
  if (phase === 'first-item') return '首次响应';
  if (phase === 'next-item') return '后续响应';
  return phase || '未知阶段';
}

export function streamHeartbeatStatusText(
  event: Record<string, any>,
  contextCompacting = false,
): string {
  const seconds = Math.round(finiteNumber(event.stalled_for_seconds));
  const turnSeconds = Math.round(finiteNumber(event.turn_elapsed_seconds));
  const lastActivity = runtimeActivityLabel(event.last_activity_kind);
  const details = [
    seconds > 0 ? `已等待 ${seconds} 秒` : '',
    turnSeconds > seconds && turnSeconds > 0 ? `本轮 ${turnSeconds} 秒` : '',
    lastActivity ? `最近活动：${lastActivity}` : '',
  ].filter(Boolean).join('，');
  const suffix = details ? `（${details}）` : '';
  if (contextCompacting) return `正在压缩上下文${suffix}`;

  const phase = String(event.phase || '').trim().toLowerCase();
  if (phase === 'first-item') return `等待模型首次响应${suffix}`;
  return `等待模型继续响应${suffix}`;
}

export function agentEventStatusText(
  event: Record<string, any>,
  displayName: string,
): string {
  const name = String(displayName || event.display_name || event.agent_name || '子 Agent').trim();
  const status = String(event.status || '').trim().toLowerCase();
  const phase = String(event.phase || '').trim().toLowerCase();
  const message = String(event.message || '').trim();

  if (phase === 'integrating') return `${name} · 子 Agent 已完成，主 Agent 正在汇总`;
  if (phase === 'completed' || status === 'completed') return `${name} · 结果已汇总`;
  if (phase === 'failed' || status === 'failed' || status === 'error') {
    return `${name} · 执行失败${message ? `：${message}` : ''}`;
  }
  if (status === 'running' || status === 'busy') {
    return `${name} · ${message || '正在执行'}`;
  }
  return [name, status, message].filter(Boolean).join(' · ');
}

export function llmProgressStatusText(event: Record<string, any>): string {
  const status = String(event.status || '').trim().toLowerCase();
  const phase = String(event.phase || '').trim().toLowerCase();
  if (status === 'preparing' || phase === 'preparing') return '正在分析你的请求';
  if (status === 'preparing_tools' || phase === 'preparing_tools') return '正在检查可用工具和权限';
  if (status === 'loading_skills' || phase === 'loading_skills') return '正在加载功能目录';
  if (status === 'matching_skills' || phase === 'matching_skills') return '正在选择本次任务需要的功能';
  if (status === 'timeout') return '主模型响应超时，正在使用已完成的工具结果收尾';
  if (status === 'error') return '主模型响应失败，正在恢复';
  if (status === 'completed') return '主模型已返回，正在整理回答';
  if (phase === 'final_response') return '工具结果已就绪，正在生成最终回答';
  return '等待主模型响应';
}

export function processNoteStatusText(event: Record<string, any>): string {
  const message = String(event.message || '').trim();
  if (message) return message.replace(/\b(?:mcp_[a-z0-9_]+|[a-z]+(?:_[a-z0-9]+){2,})\b/gi, toolDisplayLabel);

  const phase = String(event.phase || '').trim().toLowerCase();
  const toolName = event.tool_name ? toolDisplayLabel(event.tool_name) : '';
  const toolCount = Number(event.tool_count);
  if (phase === 'turn_start') return '开始处理请求';
  if (phase === 'preparing') return '我正在准备运行上下文';
  if (phase === 'context_check') return '我正在检查上下文并准备下一步';
  if (phase === 'tool_preparation') return '我在准备可用工具列表';
  if (phase === 'model_wait') return '我在等待模型判断下一步';
  if (phase === 'tool_selection') {
    return Number.isFinite(toolCount) && toolCount > 0
      ? `模型判断需要调用 ${Math.round(toolCount)} 个工具`
      : '模型判断需要调用工具';
  }
  if (phase === 'tool_start') return toolName ? `我开始运行工具：${toolName}` : '我开始运行工具';
  if (phase === 'tool_progress') return toolName ? `工具 ${toolName} 仍在运行` : '工具仍在运行';
  if (phase === 'tool_result') return toolName ? `工具 ${toolName} 已返回，我在判断下一步` : '工具已返回，我在判断下一步';
  if (phase === 'final_response') return '我在整理最终回答';
  return '执行过程已更新';
}

export function outcomeValidationStatusText(event: Record<string, any>): string {
  const status = String(event.status || '').trim().toLowerCase();
  if (status === 'running') return '验证中';
  if (status === 'repair') return '验证未通过，正在修复';
  if (status === 'partial') return '部分完成';
  if (status === 'blocked') return '未验证';
  if (status === 'pass' || status === 'completed') return '已验证';
  return '验证中';
}

export function outcomeVerificationSummary(value: unknown): string {
  const payload = value && typeof value === 'object' ? value as Record<string, any> : {};
  const status = String(payload.verification_status || '').trim().toUpperCase();
  const confidence = Math.max(0, Math.min(1, finiteNumber(payload.confidence)));
  const score = confidence > 0 ? ` ${Math.round(confidence * 100)}%` : '';
  if (status === 'VERIFIED') return `已验证${score}`;
  if (status === 'PARTIAL') return `部分验证${score}`;
  if (status === 'UNVERIFIED') return `未验证${score}`;
  return '';
}

export function canReviewOutcome(value: unknown): boolean {
  const payload = value && typeof value === 'object' ? value as Record<string, any> : {};
  const state = String(payload.confirmation_state || '').trim().toLowerCase();
  // An accepted confirmation is terminal for this message's review controls,
  // even if an older needs-confirmation flag arrives in the same snapshot.
  if (payload.superseded) return false;
  if (payload.advisory) return !['accepted', 'modification_requested'].includes(payload.delivery_state);
  if (payload.user_confirmed || state === 'accepted') return false;
  const effectiveState = state || (payload.needs_user_confirmation ? 'pending' : '');
  return effectiveState === 'pending' || effectiveState === 'objective_failure' || effectiveState === 'modification_requested' || effectiveState === 'repair_exhausted';
}

export function shouldAttemptOutcomeAcceptance(value: unknown): boolean {
  const payload = value && typeof value === 'object' ? value as Record<string, any> : {};
  const state = String(payload.confirmation_state || '').trim().toLowerCase();
  if (payload.advisory) return canReviewOutcome(payload);
  // The backend owns the objective-proof boundary. A pending result may contain
  // semantic gaps that user judgment is explicitly allowed to resolve, so the
  // UI must not disable confirmation merely because criteria_passed < total.
  return state === 'pending';
}

export function outcomeVerificationNarrative(value: unknown): string {
  const payload = value && typeof value === 'object' ? value as Record<string, any> : {};
  const status = String(payload.verification_status || '').trim().toUpperCase();
  if (!['VERIFIED', 'PARTIAL', 'UNVERIFIED'].includes(status)) return '';

  const total = Number(payload.criteria_total);
  const passed = Number(payload.criteria_passed);
  const hasCriteria = Number.isInteger(total) && total > 0
    && Number.isInteger(passed) && passed >= 0 && passed <= total;
  const requiredProof = String(payload.required_proof_level || '').trim().toUpperCase();
  const achievedProof = String(payload.achieved_proof_level || '').trim().toUpperCase();
  const hasProof = /^P[0-4]$/.test(requiredProof) && /^P[0-4]$/.test(achievedProof);
  const components = payload.confidence_components;
  const componentRows = components && typeof components === 'object' && !Array.isArray(components)
    ? [
        ['目标检查', Number(components.objective_checks)],
        ['证据覆盖', Number(components.evidence_coverage)],
        ['独立验证', Number(components.independent_validator)],
        ['结果一致性', Number(components.agreement)],
      ].filter((item): item is [string, number] => Number.isFinite(item[1]))
    : [];
  if (!hasCriteria && !hasProof && componentRows.length === 0) return '';

  let conclusion = '';
  if (hasCriteria) {
    if (status === 'VERIFIED') {
      conclusion = `本次结果通过了 ${passed}/${total} 项验收检查`;
    } else if (status === 'PARTIAL') {
      conclusion = `本次结果已有 ${passed}/${total} 项验收检查通过`;
    } else {
      conclusion = `本次结果目前通过 ${passed}/${total} 项验收检查`;
    }
  } else if (status === 'VERIFIED') {
    conclusion = '当前结果已满足现有验收条件';
  } else if (status === 'PARTIAL') {
    conclusion = '当前结果已有部分验收证据，其余条件尚未验证';
  } else {
    conclusion = '当前结果尚未获得充分的验收证据';
  }
  if (hasProof) {
    conclusion += achievedProof === requiredProof
      ? `，证明等级已达到 ${requiredProof} 要求`
      : `，当前证明等级为 ${achievedProof}，要求为 ${requiredProof}`;
  }
  if (payload.needs_user_confirmation) conclusion += '，仍需用户确认';
  if (payload.user_confirmed) conclusion += '，用户确认已纳入最终裁决';

  let scoreExplanation = '';
  if (componentRows.length > 0) {
    const normalizedRows = componentRows.map(([label, score]) => [
      label,
      Math.max(0, Math.min(1, score)),
    ] as [string, number]);
    const weakest = normalizedRows.reduce((current, item) =>
      item[1] < current[1] ? item : current
    );
    const strongest = Math.max(...normalizedRows.map((item) => item[1]));
    if (weakest[1] >= 0.9) {
      scoreExplanation = `各评分维度表现稳定，最低项“${weakest[0]}”为 ${Math.round(weakest[1] * 100)}%。`;
    } else if (strongest > weakest[1]) {
      scoreExplanation = `综合分主要受“${weakest[0]}”影响（${Math.round(weakest[1] * 100)}%），其余评分维度表现更高。`;
    }
  }
  return `${conclusion}。${scoreExplanation}`;
}

export function agentEventDisplayMessage(event: Record<string, any>): string {
  const phase = String(event.phase || '').trim().toLowerCase();
  const status = String(event.status || '').trim().toLowerCase();
  const message = String(event.message || '').trim();
  if (phase === 'integrating') return '子 Agent 已完成，主 Agent 正在汇总';
  if (phase === 'completed' || status === 'completed') return '结果已汇总';
  return message;
}

export function formatAgentRunError(value: unknown): string {
  const raw = String(value || '').trim() || '请求失败';
  const llmTimeout = raw.match(/^LLM request timed out after ([\d.]+)s(?: in harness)?/i);
  if (llmTimeout) {
    return `主模型在 ${Math.round(Number(llmTimeout[1]))} 秒内未返回结果`;
  }
  const stalled = raw.match(
    /^Agent stream stalled \(([^)]+)\) for ([\d.]+)s; last_activity=(.+)$/,
  );
  if (stalled) {
    const activity = runtimeActivityLabel(stalled[3]);
    return `执行已连续 ${Math.round(Number(stalled[2]))} 秒没有有效活动（阶段：${streamPhaseLabel(stalled[1])}，最近活动：${activity || stalled[3]}）`;
  }

  const turnLimit = raw.match(
    /^Agent turn exceeded maximum duration of ([\d.]+)s; last_activity=(.+)$/,
  );
  if (turnLimit) {
    const activity = runtimeActivityLabel(turnLimit[2]);
    return `本轮执行超过最大时长 ${Math.round(Number(turnLimit[1]))} 秒（最近活动：${activity || turnLimit[2]}）`;
  }
  return raw;
}
