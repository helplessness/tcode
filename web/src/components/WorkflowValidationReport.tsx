import { For, Show, createMemo } from 'solid-js';

import type {
  WorkflowRun,
  WorkflowStageState,
  WorkflowValidationCheck,
  WorkflowValidationReport,
} from '../api/client';

type WorkflowValidationFailure = {
  validator: string;
  validatorLabel: string;
  code: string;
  message: string;
  evidenceLabel: string;
  evidenceValues: string[];
  nextStep: string;
  retryable: boolean;
};

type WorkflowValidationReportPanelProps = {
  report?: WorkflowValidationReport | null;
  stageLabel?: string;
  variant?: 'run' | 'stage';
};

const VALIDATOR_LABELS: Record<string, string> = {
  dependencies_committed: '依赖提交检查',
  input_contract: '阶段输入检查',
  non_empty: '非空结果检查',
  no_failure_signal: '失败信号检查',
  json_schema: 'JSON Schema 检查',
  required_artifacts: '必需 artifact 检查',
  files_exist: 'Artifact 文件检查',
  contains_all: '必需文本检查',
  contains_any: '候选文本检查',
  review_decision: 'Review gate 检查',
};

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => String(item ?? '').trim())
    .filter(Boolean);
}

function failureEvidence(check: WorkflowValidationCheck) {
  const evidence = check.evidence && typeof check.evidence === 'object' ? check.evidence : {};
  const missingArtifacts = stringList(evidence.missing_artifacts);
  if (missingArtifacts.length > 0) {
    return { label: '缺失 artifact', values: missingArtifacts };
  }

  const invalidArtifactFiles = stringList(evidence.invalid_artifact_files);
  if (invalidArtifactFiles.length > 0) {
    return { label: '缺失 artifact 文件', values: invalidArtifactFiles };
  }

  const missingDependencies = stringList(evidence.missing_dependencies);
  if (missingDependencies.length > 0) {
    return { label: '未提交依赖', values: missingDependencies };
  }

  const missingInputs = stringList(evidence.missing_inputs);
  if (missingInputs.length > 0) {
    return { label: '缺失输入', values: missingInputs };
  }

  if (String(check.validator || '').trim().toLowerCase() === 'review_decision') {
    const decision = String(evidence.decision || '').trim();
    return {
      label: 'Review gate 原因',
      values: [decision ? `当前决策为 ${decision}` : '没有返回 PASS、REVISE 或 ESCALATE'],
    };
  }

  const errors = stringList(evidence.errors);
  if (errors.length > 0) {
    return { label: '校验详情', values: errors };
  }

  const terms = stringList(evidence.terms);
  const hits = new Set(stringList(evidence.hits));
  const missingTerms = terms.filter((term) => !hits.has(term));
  if (missingTerms.length > 0) {
    return { label: '缺失内容', values: missingTerms };
  }

  return { label: '', values: [] as string[] };
}

function nextStepForFailure(check: WorkflowValidationCheck, evidenceValues: string[]) {
  const code = String(check.code || '').trim().toLowerCase();
  const names = evidenceValues.join('、');
  if (code === 'missing_artifact') {
    return `补齐 artifact_refs 中的 ${names || '必需 artifact'}，再重新执行该阶段。`;
  }
  if (code === 'artifact_file_missing') {
    return `确认 ${names || 'artifact'} 已生成，且文件路径位于当前工作区内，再重试该阶段。`;
  }
  if (code === 'missing_review_decision') {
    return '让 review gate 明确返回 PASS、REVISE 或 ESCALATE，再重新校验。';
  }
  if (code === 'dependency_not_committed') {
    return `先处理并提交依赖阶段${names ? `（${names}）` : ''}，再继续当前阶段。`;
  }
  if (code === 'missing_stage_inputs') {
    return `补充缺失输入${names ? `（${names}）` : ''}，然后重新执行当前阶段。`;
  }
  if (code === 'invalid_output_schema') {
    return '按输出契约修正 JSON 结构和字段类型，再重新执行该阶段。';
  }
  if (code === 'empty_stage_result') {
    return '重新执行该阶段，并确保返回非空结果。';
  }
  if (code === 'required_output_text_missing') {
    return `在阶段输出中补齐要求的内容${names ? `（${names}）` : ''}，再重新校验。`;
  }
  if (code === 'explicit_failure_signal') {
    return '先处理阶段输出中声明的失败或阻塞条件，再重新执行；若无法恢复，请人工接管或选择回退。';
  }
  if (code === 'unknown_validator') {
    return '检查 Workflow 定义中的 validator 配置，修正后重新运行。';
  }
  return check.retryable
    ? '修正上述校验问题后重新执行当前阶段。'
    : '该失败不可自动重试，请人工接管、回退或终止运行。';
}

export function workflowValidationFailures(
  report?: WorkflowValidationReport | null,
): WorkflowValidationFailure[] {
  const checks = Array.isArray(report?.checks) ? report.checks : [];
  return checks
    .filter((check): check is WorkflowValidationCheck => Boolean(check && check.ok === false))
    .map((check) => {
      const validator = String(check.validator || 'unknown').trim() || 'unknown';
      const evidence = failureEvidence(check);
      return {
        validator,
        validatorLabel: VALIDATOR_LABELS[validator] || 'Validator',
        code: String(check.code || 'validation_failed').trim() || 'validation_failed',
        message: String(check.message || report?.summary || '阶段校验未通过。').trim(),
        evidenceLabel: evidence.label,
        evidenceValues: evidence.values,
        nextStep: nextStepForFailure(check, evidence.values),
        retryable: Boolean(check.retryable),
      };
    });
}

export function blockedWorkflowValidationStage(run?: WorkflowRun | null): WorkflowStageState | null {
  if (String(run?.status || '').trim().toLowerCase() !== 'blocked') return null;
  const stages = Array.isArray(run?.stage_states) ? run.stage_states : [];
  const hasFailures = (stage: WorkflowStageState) => workflowValidationFailures(stage.validation_report).length > 0;
  const currentStageId = String(run?.current_stage_id || '').trim();
  return stages.find((stage) => stage.stage_id === currentStageId && hasFailures(stage))
    || [...stages].reverse().find((stage) => hasFailures(stage))
    || null;
}

export function WorkflowValidationReportPanel(props: WorkflowValidationReportPanelProps) {
  const failures = createMemo(() => workflowValidationFailures(props.report));
  const isRunView = () => props.variant === 'run';

  return (
    <Show when={failures().length > 0}>
      <section
        class={`${isRunView() ? 'mt-4' : 'mt-3'} rounded-2xl border border-[#da3633]/35 bg-[#201317] px-4 py-3`}
        data-testid="workflow-validation-report"
      >
        <Show when={props.report?.diagnostic}>
          {(diagnostic) => <div class="mb-3 text-xs text-[#f1d4d4]">
            <div class="font-semibold">{diagnostic().title}</div>
            <p class="mt-1">{diagnostic().message}</p>
            <p class="mt-1">处理建议：{diagnostic().suggestion}</p>
            <p class="mt-1">已有阶段结果与本阶段草稿可在运行详情查看。</p>
          </div>}
        </Show>
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#ffb6b0]">
              {isRunView() ? '校验阻塞详情' : '校验失败'}
            </div>
            <Show when={props.stageLabel}>
              <div class="mt-1 text-xs text-[#d7a19d]">阶段：{props.stageLabel}</div>
            </Show>
          </div>
          <span class="rounded-full border border-[#da3633]/30 bg-[#da3633]/10 px-2.5 py-1 text-[10px] font-semibold text-[#ffb6b0]">
            {failures().length} 个 validator 未通过
          </span>
        </div>

        <div class="mt-3 space-y-3">
          <For each={failures()}>
            {(failure) => (
              <div class="rounded-xl border border-[#5a1e23] bg-[#160f12] px-3 py-3" data-validator={failure.validator}>
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-xs font-semibold text-[#ffd4d0]">
                    {failure.validatorLabel} ({failure.validator})
                  </span>
                  <code class="rounded border border-[#5a1e23] bg-[#27161a] px-2 py-0.5 text-[10px] text-[#ff9c96]">
                    {failure.code}
                  </code>
                  <Show when={failure.retryable}>
                    <span class="text-[10px] text-[#d7a19d]">可重试</span>
                  </Show>
                </div>
                <div class="mt-2 text-xs text-[#f0c1bd]">原因：{failure.message}</div>
                <Show when={failure.evidenceValues.length > 0}>
                  <div class="mt-2 text-xs text-[#f0c1bd]">
                    {failure.evidenceLabel}：{failure.evidenceValues.join('、')}
                  </div>
                </Show>
                <div class="mt-2 rounded-lg border border-[#f1a02b]/20 bg-[#2a2114] px-3 py-2 text-xs text-[#ffe1b3]">
                  下一步：{failure.nextStep}
                </div>
              </div>
            )}
          </For>
        </div>
      </section>
    </Show>
  );
}

export function WorkflowBlockedValidation(props: { run?: WorkflowRun | null }) {
  const stage = createMemo(() => blockedWorkflowValidationStage(props.run));
  return (
    <Show when={stage()}>
      {(blockedStage) => (
        <WorkflowValidationReportPanel
          report={blockedStage().validation_report}
          stageLabel={blockedStage().label || blockedStage().stage_id}
          variant="run"
        />
      )}
    </Show>
  );
}
