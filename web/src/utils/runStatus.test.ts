import { describe, expect, it } from 'vitest';

import {
  agentEventDisplayMessage,
  agentEventStatusText,
  canReviewOutcome,
  contextCompactionStatusText,
  formatAgentRunError,
  formatRuntimeTokenCount,
  llmProgressStatusText,
  outcomeValidationStatusText,
  outcomeVerificationNarrative,
  outcomeVerificationSummary,
  processNoteStatusText,
  shouldAttemptOutcomeAcceptance,
  streamHeartbeatStatusText,
} from './runStatus';

describe('run status text', () => {
  it('removes review and modification controls once confirmation is accepted', () => {
    expect(canReviewOutcome({advisory:true, confirmation_state:'pending', needs_user_confirmation:true})).toBe(true);
    expect(canReviewOutcome({advisory:true, delivery_state:'accepted'})).toBe(false);
    expect(canReviewOutcome({advisory:true, delivery_state:'modification_requested'})).toBe(false);
    expect(shouldAttemptOutcomeAcceptance({advisory:true, criteria_failed:2})).toBe(true);
    expect(canReviewOutcome({ confirmation_state: 'pending', needs_user_confirmation: true })).toBe(true);
    expect(canReviewOutcome({ confirmation_state: 'accepted' })).toBe(false);
    expect(canReviewOutcome({ user_confirmed: true })).toBe(false);
    expect(canReviewOutcome({
      confirmation_state: 'pending', needs_user_confirmation: true, user_confirmed: true,
    })).toBe(false);
    expect(canReviewOutcome({
      confirmation_state: 'objective_failure', needs_user_confirmation: true, user_confirmed: true,
    })).toBe(false);
    // Rejected acceptance is not a successful user confirmation.
    expect(canReviewOutcome({ confirmation_state: 'objective_failure', user_confirmed: false })).toBe(true);
    expect(canReviewOutcome({ confirmation_state: 'modification_requested' })).toBe(true);
    expect(canReviewOutcome({ confirmation_state: 'repair_exhausted', repair_attempt: 3 })).toBe(true);
    expect(canReviewOutcome({ confirmation_state: 'pending', superseded: true })).toBe(false);
    expect(canReviewOutcome(undefined)).toBe(false);
  });

  it('allows pending results to reach backend confirmation adjudication', () => {
    expect(shouldAttemptOutcomeAcceptance({
      confirmation_state: 'pending', criteria_passed: 0, criteria_total: 2,
    })).toBe(true);
    expect(shouldAttemptOutcomeAcceptance({
      confirmation_state: 'pending', criteria_passed: 2, criteria_total: 2,
    })).toBe(true);
    expect(shouldAttemptOutcomeAcceptance({ confirmation_state: 'pending' })).toBe(true);
    expect(shouldAttemptOutcomeAcceptance({ confirmation_state: 'modification_requested' })).toBe(false);
  });

  it('describes an active context compaction with its estimated size', () => {
    expect(contextCompactionStatusText({
      status: 'running',
      estimated_tokens: 104_200,
      message_count: 86,
    })).toBe('正在压缩上下文（约 104K tokens，86 条消息）');
  });

  it('summarizes completed compaction savings', () => {
    expect(contextCompactionStatusText({
      status: 'completed',
      original_tokens: 104_200,
      compressed_tokens: 31_000,
      tokens_saved: 73_200,
    })).toBe('上下文压缩完成（104K → 31K，节省 73K tokens），继续思考中');
  });

  it('distinguishes model waiting from a compaction heartbeat', () => {
    const event = { phase: 'first-item', stalled_for_seconds: 45 };
    expect(streamHeartbeatStatusText(event, false)).toBe('等待模型首次响应（已等待 45 秒）');
    expect(streamHeartbeatStatusText(event, true)).toBe('正在压缩上下文（已等待 45 秒）');
  });

  it('shows turn activity details in stream heartbeats', () => {
    expect(streamHeartbeatStatusText({
      phase: 'next-item',
      stalled_for_seconds: 120,
      turn_elapsed_seconds: 420,
      last_activity_kind: 'subagent:completed',
    })).toBe('等待模型继续响应（已等待 120 秒，本轮 420 秒，最近活动：子 Agent 已完成）');
  });

  it('describes the subagent integration phase without claiming completion', () => {
    const event = {
      status: 'running',
      phase: 'integrating',
      message: 'Subagent completed; integrating result',
    };
    expect(agentEventStatusText(event, '论文助手')).toBe(
      '论文助手 · 子 Agent 已完成，主 Agent 正在汇总',
    );
    expect(agentEventDisplayMessage(event)).toBe('子 Agent 已完成，主 Agent 正在汇总');
  });

  it('shows when completed tool results are waiting for final model synthesis', () => {
    expect(llmProgressStatusText({ status: 'preparing', phase: 'preparing' })).toBe('正在分析你的请求');
    expect(llmProgressStatusText({ status: 'preparing_tools', phase: 'preparing_tools' })).toBe('正在检查可用工具和权限');
    expect(llmProgressStatusText({ status: 'loading_skills' })).toBe('正在加载功能目录');
    expect(llmProgressStatusText({ status: 'matching_skills' })).toBe('正在选择本次任务需要的功能');
    expect(llmProgressStatusText({
      status: 'running',
      phase: 'final_response',
    })).toBe('工具结果已就绪，正在生成最终回答');
    expect(llmProgressStatusText({
      status: 'timeout',
      phase: 'final_response',
    })).toBe('主模型响应超时，正在使用已完成的工具结果收尾');
  });

  it('shows outcome validation and repair states', () => {
    expect(outcomeValidationStatusText({ status: 'running' })).toBe('验证中');
    expect(outcomeValidationStatusText({ status: 'repair', repair_attempt: 1 })).toBe(
      '验证未通过，正在修复',
    );
    expect(outcomeValidationStatusText({ status: 'partial' })).toBe('部分完成');
    expect(outcomeVerificationSummary({ verification_status: 'VERIFIED', confidence: 0.92 })).toBe(
      '已验证 92%',
    );
    expect(outcomeVerificationSummary({ verification_status: 'PARTIAL', confidence: 0.61 })).toBe(
      '部分验证 61%',
    );
    expect(outcomeVerificationSummary({ verification_status: 'NOT_REQUIRED', confidence: 1 })).toBe('');
  });

  it('adds a concise evidence-based explanation only when score details exist', () => {
    expect(outcomeVerificationNarrative({
      verification_status: 'VERIFIED',
      confidence: 0.94,
      confidence_components: {
        objective_checks: 1,
        evidence_coverage: 1,
        independent_validator: 0.75,
        agreement: 1,
      },
      required_proof_level: 'P1',
      achieved_proof_level: 'P1',
    })).toBe(
      '当前结果已满足现有验收条件，证明等级已达到 P1 要求。综合分主要受“独立验证”影响（75%），其余评分维度表现更高。',
    );
    expect(outcomeVerificationNarrative({
      verification_status: 'VERIFIED',
      confidence: 0.94,
    })).toBe('');
  });

  it('explains when user confirmation was used as bounded semantic evidence', () => {
    expect(outcomeVerificationNarrative({
      verification_status: 'VERIFIED',
      confidence: 0.94,
      confidence_components: {
        objective_checks: 1,
        evidence_coverage: 1,
        independent_validator: 0.75,
        agreement: 1,
      },
      required_proof_level: 'P1',
      achieved_proof_level: 'P1',
      user_confirmed: true,
    })).toBe(
      '当前结果已满足现有验收条件，证明等级已达到 P1 要求，用户确认已纳入最终裁决。综合分主要受“独立验证”影响（75%），其余评分维度表现更高。',
    );
  });

  it('shows public process notes without needing raw reasoning text', () => {
    expect(processNoteStatusText({
      kind: 'process_note',
      phase: 'tool_selection',
      tool_count: 2,
    })).toBe('模型判断需要调用 2 个工具');
    expect(processNoteStatusText({
      kind: 'process_note',
      phase: 'tool_start',
      tool_name: 'paper_literature_search',
    })).toBe('我开始运行工具：文献检索与整理');
    expect(processNoteStatusText({
      kind: 'process_note',
      phase: 'tool_progress',
      tool_name: 'paper_library_ingest_pdf',
    })).toBe('工具 文献检索与整理 仍在运行');
    expect(processNoteStatusText({
      kind: 'process_note',
      message: '工具 paper_literature_search 已完成，正在整理结果',
    })).toBe('工具 文献检索与整理 已完成，正在整理结果');
  });

  it('translates harness timeout errors into actionable status text', () => {
    expect(formatAgentRunError(
      'LLM request timed out after 180s in harness',
    )).toBe('主模型在 180 秒内未返回结果');
    expect(formatAgentRunError(
      'Agent stream stalled (next-item) for 600s; last_activity=subagent:completed',
    )).toBe(
      '执行已连续 600 秒没有有效活动（阶段：后续响应，最近活动：子 Agent 已完成）',
    );
    expect(formatAgentRunError(
      'Agent turn exceeded maximum duration of 3600s; last_activity=stream_item',
    )).toBe(
      '本轮执行超过最大时长 3600 秒（最近活动：模型返回了新消息）',
    );
  });

  it('formats token counts compactly', () => {
    expect(formatRuntimeTokenCount(999)).toBe('999');
    expect(formatRuntimeTokenCount(12_600)).toBe('13K');
    expect(formatRuntimeTokenCount(1_250_000)).toBe('1.3M');
  });
});
