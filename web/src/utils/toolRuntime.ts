type ToolLike = {
  tool_name?: string;
  metadata?: Record<string, any>;
  error?: string | null;
};

export function getToolVerificationLabel(tool: ToolLike): string | null {
  const metadata = tool.metadata || {};
  const audit = metadata.computer_audit || {};
  if (metadata.outcome_unknown || audit.status === 'outcome_unknown') return null;
  const pending = metadata.computer_verification_pending === true
    || metadata.computer?.action_receipt?.verification === 'pending';
  const legacy = metadata.known_action_receipt === true
    && audit.status === 'failed_known'
    && ['effect_unverifiable', 'partial_effect', 'suspected_noop'].includes(audit.error_code);
  // This is the receipt at dispatch time, not a live task-level waiting state.
  return (pending && !tool.error) || legacy ? '已发送' : null;
}

export type ToolRuntimeRow = {
  label: string;
  value: string;
};

export type ToolRuntimeSummary = {
  sandboxed: boolean;
  badge: string;
  badgeClass: string;
  panelClass: string;
  rows: ToolRuntimeRow[];
};

const RUNTIME_TOOLS = new Set(['bash', 'exec', 'process', 'browser', 'skill_run', 'paper_library_ingest_pdf']);

const isTruthy = (value: any): boolean => {
  if (value === true) return true;
  if (typeof value === 'number') return value !== 0;
  const text = String(value ?? '').trim().toLowerCase();
  return ['1', 'true', 'yes', 'on'].includes(text);
};

const textValue = (...values: any[]): string => {
  for (const value of values) {
    const text = String(value ?? '').trim();
    if (text) return text;
  }
  return '';
};

const objectValue = (value: any): Record<string, any> => {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, any>;
  return {};
};

const compactLabel = (...values: any[]): string => {
  return values
    .map((value) => String(value ?? '').trim())
    .filter(Boolean)
    .join(' / ');
};

export function getToolRuntimeSummary(tool: ToolLike): ToolRuntimeSummary | null {
  const toolName = String(tool.tool_name || '').trim().toLowerCase();
  const metadata = tool.metadata && typeof tool.metadata === 'object' && !Array.isArray(tool.metadata)
    ? tool.metadata
    : {};
  const execution = objectValue(metadata.execution_environment);
  const delegatedExecution = objectValue(metadata.delegated_execution_environment);
  const childExecution = objectValue(metadata.child_execution_environment);
  const requiredExecution = objectValue(metadata.required_execution_environment);
  const delegated = Object.keys(delegatedExecution).length > 0 ? delegatedExecution : childExecution;
  const sandboxMeta = objectValue(metadata.sandbox);
  const location = String(execution.location || '').trim().toLowerCase();
  const delegatedLocation = String(delegated.location || '').trim().toLowerCase();
  const hasExecutionSandboxed = Object.prototype.hasOwnProperty.call(execution, 'sandboxed');
  const hasDelegatedSandboxed = Object.prototype.hasOwnProperty.call(delegated, 'sandboxed');
  const notExecuted = isTruthy(metadata.execution_skipped)
    || location === 'not_executed'
    || location === 'blocked';

  const hasDelegatedSandboxSignal =
    delegatedLocation === 'sandbox' ||
    (hasDelegatedSandboxed && isTruthy(delegated.sandboxed)) ||
    Boolean(textValue(
      delegated.sandbox_job_id,
      delegated.container_id,
      delegated.container_name,
      delegated.sandbox_tool,
    ));

  const hasSandboxSignal =
    hasDelegatedSandboxSignal ||
    location === 'sandbox' ||
    isTruthy(execution.sandboxed) ||
    isTruthy(metadata.sandbox) ||
    isTruthy(metadata.sandbox_executed) ||
    isTruthy(metadata.sandbox_worker) ||
    isTruthy(sandboxMeta.enabled) ||
    isTruthy(sandboxMeta.sandbox_worker) ||
    Boolean(textValue(
      metadata.sandbox_job_id,
      metadata.container_id,
      metadata.container_name,
      sandboxMeta.sandbox_job_id,
      sandboxMeta.container_id,
      sandboxMeta.container_name,
      execution.sandbox_job_id,
      execution.container_id,
      execution.container_name,
      delegated.sandbox_job_id,
      delegated.container_id,
      delegated.container_name,
    ));
  const hasHostSignal = location === 'host' || (hasExecutionSandboxed && isTruthy(execution.sandboxed) === false);

  const shouldShow = notExecuted || hasSandboxSignal || hasHostSignal || RUNTIME_TOOLS.has(toolName);
  if (!shouldShow) return null;
  const sandboxed = !notExecuted && hasSandboxSignal;
  const delegatedSandboxed = !notExecuted
    && (hasDelegatedSandboxSignal || (isTruthy(metadata.delegated_execution) && sandboxed));
  const runtime = notExecuted
    ? requiredExecution
    : delegatedSandboxed
      ? delegated
      : execution;

  const rows: ToolRuntimeRow[] = notExecuted
    ? [
        { label: '执行状态', value: '未执行' },
        { label: '要求环境', value: 'Sandbox' },
      ]
    : delegatedSandboxed
    ? [
        { label: '调度环境', value: '宿主机' },
        { label: '实际执行', value: 'Sandbox 子任务' },
      ]
    : [
        {
          label: '执行环境',
          value: sandboxed ? 'Sandbox' : '宿主机',
        },
      ];

  const sandboxTool = textValue(runtime.sandbox_tool, metadata.sandbox_tool, metadata.sandbox_tool_name, metadata.runtime_tool);
  const runnerUrl = textValue(runtime.runner_url, metadata.sandbox_runner_url);
  const image = textValue(runtime.image, metadata.sandbox_image, sandboxMeta.image);
  const runtimeName = textValue(runtime.runtime, metadata.sandbox_runtime, metadata.runtime, metadata.docker_runtime, sandboxMeta.runtime);
  const backend = textValue(runtime.backend, metadata.sandbox_backend, metadata.backend, sandboxMeta.backend);
  const network = textValue(runtime.network, metadata.sandbox_network, metadata.network_policy, sandboxMeta.network);
  const container = textValue(runtime.container_id, runtime.container_name, metadata.container_id, metadata.container_name, sandboxMeta.container_id, sandboxMeta.container_name);
  const processId = textValue(runtime.process_id, metadata.process_id);
  const sandboxJob = textValue(runtime.sandbox_job_id, metadata.sandbox_job_id, sandboxMeta.sandbox_job_id);
  const status = textValue(runtime.status, metadata.sandbox_status, metadata.status, sandboxMeta.status);
  const returnCode = textValue(runtime.exit_code, metadata.returncode, metadata.exitCode, metadata.exit_code, sandboxMeta.exit_code);
  const containerCwd = textValue(runtime.container_cwd, metadata.container_cwd);
  const profile = textValue(runtime.profile, metadata.sandbox_profile, sandboxMeta.sandbox_profile);
  const workerInput = textValue(runtime.worker_input, metadata.worker_input, sandboxMeta.input_path);
  const workerOutput = textValue(runtime.worker_output, metadata.worker_output, sandboxMeta.output_path);
  const sourceTool = textValue(runtime.source_tool_name);
  const sourceAgent = textValue(runtime.source_agent_name);
  const sourceSession = textValue(runtime.source_session_id);
  const workdir = textValue(execution.workdir, metadata.workdir);
  const requiredAgent = textValue(requiredExecution.agent);

  if (requiredAgent) rows.push({ label: '要求代理', value: requiredAgent });
  if (sourceTool) rows.push({ label: '子工具', value: sourceTool });
  if (sourceAgent) rows.push({ label: '子代理', value: sourceAgent });
  if (sourceSession) rows.push({ label: '子会话', value: sourceSession });
  if (sandboxTool) rows.push({ label: 'Sandbox 工具', value: sandboxTool });
  if (backend || runtimeName) rows.push({ label: '后端/运行时', value: compactLabel(backend, runtimeName) });
  if (image) rows.push({ label: '镜像', value: image });
  if (runnerUrl) rows.push({ label: 'Runner', value: runnerUrl });
  if (network) rows.push({ label: '网络', value: network });
  if (container) rows.push({ label: '容器', value: container });
  if (sandboxJob) rows.push({ label: 'Sandbox Job', value: sandboxJob });
  if (processId) rows.push({ label: 'Process ID', value: processId });
  if (status) rows.push({ label: '状态', value: status });
  if (returnCode) rows.push({ label: '返回码', value: returnCode });
  if (containerCwd) rows.push({ label: '容器目录', value: containerCwd });
  if (profile) rows.push({ label: 'Profile', value: profile });
  if (workerInput) rows.push({ label: 'Worker Input', value: workerInput });
  if (workerOutput) rows.push({ label: 'Worker Output', value: workerOutput });
  if (!sandboxed && workdir) rows.push({ label: '宿主机目录', value: workdir });

  return {
    sandboxed,
    badge: notExecuted ? '未执行' : delegatedSandboxed ? 'Host -> Sandbox' : (sandboxed ? 'Sandbox' : 'Host'),
    badgeClass: notExecuted
      ? 'border-[#8b6f33] bg-[#211b10] text-[#f2cc60]'
      : sandboxed
      ? 'border-[#256f4a] bg-[#0f2418] text-[#7ee787]'
      : 'border-[#36556d] bg-[#101922] text-[#8ed5ff]',
    panelClass: notExecuted
      ? 'border-[#8b6f33] bg-[#1a160e]'
      : sandboxed
      ? 'border-[#256f4a] bg-[#0f1913]'
      : 'border-[#36556d] bg-[#0d141b]',
    rows,
  };
}
