export type TeamWorkerCatalogEntry = {
  name: string;
  agent_name?: string;
  role?: string;
  enabled?: boolean;
  visible?: boolean;
  resident?: boolean;
  permission_preset?: string;
  deletable?: boolean;
  peer_whitelist?: string[];
  skill_tags?: string[];
  metadata?: Record<string, any>;
};

export type TeamCatalogEntry = {
  key: string;
  display_name: string;
  runtime_agent: string;
  visible_agent_name: string;
  slug?: string;
  source?: string;
  delegation_reason?: string;
  hidden_agent_names?: string[];
  execution_agent_names?: string[];
  bootstrap_agent_names?: string[];
  team_mode_agent_names?: string[];
  workflow_mode_agent_names?: string[];
  forced_scheduler_agent_names?: string[];
  mode_hint_visible?: string;
  mode_hint_runtime?: string;
  supports_memory?: boolean;
  supports_subagents?: boolean;
  worker_names?: string[];
  resident_worker_names?: string[];
  session_prefixes?: string[];
  disabled_worker_names?: string[];
  workers?: TeamWorkerCatalogEntry[];
  available?: boolean;
};

const DEFAULT_TEAM_CATALOG: TeamCatalogEntry[] = [
  {
    key: 'paper',
    display_name: '文献自动协作团队',
    runtime_agent: 'paper_auto_coordinator',
    visible_agent_name: 'paper_auto',
    slug: 'paper',
    source: 'paper-research-workflow',
    delegation_reason: '识别到文献/PDF/综述类任务',
    hidden_agent_names: ['paper_auto_coordinator'],
    execution_agent_names: ['paper_auto_coordinator'],
    bootstrap_agent_names: ['paper_auto', 'paper_auto_coordinator'],
    team_mode_agent_names: ['paper_auto', 'paper_auto_coordinator'],
    workflow_mode_agent_names: ['paper_auto', 'paper_auto_coordinator'],
    forced_scheduler_agent_names: ['paper_auto', 'paper_auto_coordinator'],
    mode_hint_visible: '文献自动模式：统一进入协作型研究团队，按任务需要自由组合解析、检索、阅读、归纳和复核。',
    mode_hint_runtime: '文献自动模式：当前会话已进入协作型研究团队，会按任务需要动态调用检索、阅读、归纳、复核和通用助理。',
    supports_memory: true,
    supports_subagents: true,
    worker_names: [
      'paper_generalist',
      'paper_library_manager',
      'paper_library_retriever',
      'paper_search',
      'paper_reader',
      'paper_synthesis',
      'paper_review',
    ],
    resident_worker_names: ['paper_generalist'],
    session_prefixes: ['research-'],
    workers: [
      { name: 'paper_generalist', role: '通用研究助理', enabled: true, visible: true, resident: true },
      { name: 'paper_library_manager', role: '本地文献库管理员', enabled: true, visible: true },
      { name: 'paper_library_retriever', role: '本地文献库检索师', enabled: true, visible: true },
      { name: 'paper_search', role: '文献检索规划师', enabled: true, visible: true },
      { name: 'paper_reader', role: '论文阅读师', enabled: true, visible: true },
      { name: 'paper_synthesis', role: '综述归纳师', enabled: true, visible: true },
      { name: 'paper_review', role: '研究复核师', enabled: true, visible: true },
    ],
    available: true,
  },
  {
    key: 'protein',
    display_name: '蛋白设计团队',
    runtime_agent: 'protein_orchestrator',
    visible_agent_name: 'protein_orchestrator',
    slug: 'protein',
    source: 'builtin',
    delegation_reason: '识别到蛋白设计/结构/序列类任务',
    hidden_agent_names: [],
    execution_agent_names: ['protein_orchestrator'],
    bootstrap_agent_names: ['protein_orchestrator'],
    team_mode_agent_names: ['protein_orchestrator'],
    workflow_mode_agent_names: ['protein_orchestrator'],
    forced_scheduler_agent_names: ['protein_orchestrator'],
    mode_hint_visible: '固定专家团队模式：自动使用预置蛋白设计专家协作，不需要手动建团队。',
    mode_hint_runtime: '固定专家团队模式：自动使用预置蛋白设计专家协作，不需要手动建团队。',
    supports_memory: true,
    supports_subagents: true,
    worker_names: [
      'generalist',
      'requirements',
      'paper_generalist',
      'design',
      'structure',
      'scoring',
      'experiment',
      'risk',
      'verifier',
      'report',
    ],
    resident_worker_names: ['generalist'],
    session_prefixes: ['protein-'],
    workers: [
      { name: 'generalist', role: '蛋白通用执行主管', enabled: true, visible: true, resident: true },
      { name: 'requirements', role: '蛋白质需求工程师', enabled: true, visible: true },
      { name: 'paper_generalist', role: '文献证据抽取与入库助理', enabled: true, visible: true },
      { name: 'design', role: '蛋白质序列设计师', enabled: true, visible: true },
      { name: 'structure', role: '结构生物学工程师', enabled: true, visible: true },
      { name: 'scoring', role: '蛋白质评分工程师', enabled: true, visible: true },
      { name: 'experiment', role: '实验验证工程师', enabled: true, visible: true },
      { name: 'risk', role: '生物安全风险评估师', enabled: true, visible: true },
      { name: 'verifier', role: '独立复核师', enabled: true, visible: true },
      { name: 'report', role: '技术报告师', enabled: true, visible: true },
    ],
    available: true,
  },
];

let catalogEntries: TeamCatalogEntry[] = [...DEFAULT_TEAM_CATALOG];

const MANAGER_AGENT_NAMES = new Set(['manager']);

function normalizeNames(...groups: Array<string[] | undefined>): string[] {
  const merged = groups.flatMap((group) => group || []);
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of merged) {
    const value = String(item || '').trim();
    if (!value) continue;
    const lowered = value.toLowerCase();
    if (seen.has(lowered)) continue;
    seen.add(lowered);
    result.push(value);
  }
  return result;
}

function normalizeEntry(entry: TeamCatalogEntry): TeamCatalogEntry {
  const runtimeAgent = String(entry.runtime_agent || '').trim();
  const visibleAgent = String(entry.visible_agent_name || runtimeAgent).trim() || runtimeAgent;
  const hiddenAgentNames = normalizeNames(
    entry.hidden_agent_names,
    visibleAgent !== runtimeAgent ? [runtimeAgent] : [],
  );
  const executionAgentNames = normalizeNames(entry.execution_agent_names, [runtimeAgent]);
  const bootstrapAgentNames = normalizeNames(entry.bootstrap_agent_names, [visibleAgent, runtimeAgent]);
  const teamModeAgentNames = normalizeNames(entry.team_mode_agent_names, bootstrapAgentNames);
  const workflowModeAgentNames = normalizeNames(entry.workflow_mode_agent_names, teamModeAgentNames);
  const forcedSchedulerAgentNames = normalizeNames(
    entry.forced_scheduler_agent_names,
    workflowModeAgentNames,
  );
  const residentWorkerNames = normalizeNames(entry.resident_worker_names);
  const normalizedWorkers = (entry.workers || [])
    .map((worker) => ({
      ...worker,
      name: String(worker?.name || '').trim().toLowerCase(),
      agent_name: String(worker?.agent_name || '').trim(),
      role: String(worker?.role || worker?.name || '').trim(),
      enabled: worker?.enabled !== false,
      visible: worker?.visible !== false,
      resident: worker?.resident === true,
      permission_preset: String(worker?.permission_preset || 'developer').trim() || 'developer',
      deletable: worker?.deletable === true,
      peer_whitelist: Array.isArray(worker?.peer_whitelist) ? worker.peer_whitelist : [],
      skill_tags: Array.isArray(worker?.skill_tags) ? worker.skill_tags : [],
    }))
    .filter((worker) => worker.name);
  const workerNames = normalizeNames(
    entry.worker_names,
    normalizedWorkers
      .filter((worker) => worker.enabled !== false && worker.visible !== false)
      .map((worker) => worker.name),
    residentWorkerNames,
  );
  const sessionPrefixes = normalizeNames(entry.session_prefixes);

  return {
    ...entry,
    runtime_agent: runtimeAgent,
    visible_agent_name: visibleAgent,
    hidden_agent_names: hiddenAgentNames,
    execution_agent_names: executionAgentNames,
    bootstrap_agent_names: bootstrapAgentNames,
    team_mode_agent_names: teamModeAgentNames,
    workflow_mode_agent_names: workflowModeAgentNames,
    forced_scheduler_agent_names: forcedSchedulerAgentNames,
    worker_names: workerNames,
    resident_worker_names: normalizeNames(
      residentWorkerNames,
      normalizedWorkers
        .filter((worker) => worker.enabled !== false && worker.visible !== false && worker.resident)
        .map((worker) => worker.name),
    ),
    session_prefixes: sessionPrefixes,
    disabled_worker_names: normalizeNames(entry.disabled_worker_names),
    workers: normalizedWorkers,
  };
}

function normalizedCatalog(): TeamCatalogEntry[] {
  return catalogEntries.map(normalizeEntry);
}

export function hydrateTeamCatalog(entries?: TeamCatalogEntry[]) {
  if (!Array.isArray(entries) || entries.length === 0) {
    catalogEntries = [...DEFAULT_TEAM_CATALOG];
    return;
  }
  catalogEntries = entries.map(normalizeEntry);
}

export function getTeamCatalogEntries(): TeamCatalogEntry[] {
  return normalizedCatalog();
}

export function getTeamCatalogEntryByAgentName(agentName?: string | null): TeamCatalogEntry | undefined {
  const raw = String(agentName || '').trim().toLowerCase();
  if (!raw) return undefined;
  return normalizedCatalog().find((entry) => {
    const names = normalizeNames([
      entry.runtime_agent,
      entry.visible_agent_name,
      ...(entry.hidden_agent_names || []),
      ...(entry.execution_agent_names || []),
      ...(entry.bootstrap_agent_names || []),
      ...(entry.team_mode_agent_names || []),
      ...(entry.workflow_mode_agent_names || []),
      ...(entry.forced_scheduler_agent_names || []),
    ]);
    return names.some((name) => name.toLowerCase() === raw);
  });
}

export function toVisibleTeamAgentName(agentName?: string | null): string {
  const raw = String(agentName || '').trim();
  if (!raw) return '';
  return getTeamCatalogEntryByAgentName(raw)?.visible_agent_name || raw;
}

export function resolveExecutionAgentName(
  selectedAgentName?: string | null,
  currentAgentName?: string | null,
): string {
  const selected = String(selectedAgentName || '').trim();
  const current = String(currentAgentName || '').trim();
  if (!selected) return '';
  const entry = normalizedCatalog().find((item) => item.visible_agent_name === selected);
  if (!entry) return selected;
  if ((entry.execution_agent_names || []).some((name) => name === current)) {
    return current;
  }
  return selected;
}

export function isFrontendHiddenAgent(agentName?: string | null): boolean {
  const raw = String(agentName || '').trim().toLowerCase();
  if (!raw) return false;
  return normalizedCatalog().some((entry) =>
    (entry.hidden_agent_names || []).some((name) => name.toLowerCase() === raw),
  );
}

export function isFixedTeamBootstrapAgent(agentName?: string | null): boolean {
  const raw = String(agentName || '').trim().toLowerCase();
  if (!raw) return false;
  return normalizedCatalog().some((entry) =>
    (entry.bootstrap_agent_names || []).some((name) => name.toLowerCase() === raw),
  );
}

export function isFixedTeamAgent(agentName?: string | null): boolean {
  return Boolean(getTeamCatalogEntryByAgentName(agentName));
}

export function isTeamModeAgent(agentName?: string | null): boolean {
  const raw = String(agentName || '').trim().toLowerCase();
  if (!raw) return false;
  if (MANAGER_AGENT_NAMES.has(raw)) return true;
  return normalizedCatalog().some((entry) =>
    (entry.team_mode_agent_names || []).some((name) => name.toLowerCase() === raw),
  );
}

export function usesFixedScheduler(agentName?: string | null): boolean {
  const raw = String(agentName || '').trim().toLowerCase();
  if (!raw) return false;
  if (MANAGER_AGENT_NAMES.has(raw)) return true;
  return normalizedCatalog().some((entry) =>
    (entry.forced_scheduler_agent_names || []).some((name) => name.toLowerCase() === raw),
  );
}

export function isWorkflowModeAgent(agentName?: string | null): boolean {
  const raw = String(agentName || '').trim().toLowerCase();
  if (!raw) return false;
  if (MANAGER_AGENT_NAMES.has(raw)) return true;
  return normalizedCatalog().some((entry) =>
    (entry.workflow_mode_agent_names || []).some((name) => name.toLowerCase() === raw),
  );
}

export function getWorkflowModeHint(
  selectedAgentName?: string | null,
  actualAgentName?: string | null,
): string {
  const selected = String(selectedAgentName || '').trim();
  const actual = String(actualAgentName || '').trim();
  const entry = getTeamCatalogEntryByAgentName(selected) || getTeamCatalogEntryByAgentName(actual);
  if (!entry) {
    return '当前模式已启用。';
  }
  if (actual && actual === entry.runtime_agent && entry.mode_hint_runtime) {
    return entry.mode_hint_runtime;
  }
  return entry.mode_hint_visible || entry.mode_hint_runtime || entry.display_name;
}
