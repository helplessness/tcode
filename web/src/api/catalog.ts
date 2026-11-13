/** Agent, team, runtime and model catalogs. */
import type { TeamCatalogEntry } from '../utils/teamRegistry';

const API_BASE = '';

export async function getAgents(sessionId?: string, workdirName?: string) {
  try {
    const params = new URLSearchParams();
    if (sessionId) params.set('session_id', sessionId);
    if (workdirName) params.set('workdir_name', workdirName);
    const suffix = params.size > 0 ? `?${params.toString()}` : '';
    const resp = await fetch(`${API_BASE}/api/agents${suffix}`);
    const data = await resp.json();
    return data.agents || [];
  } catch (e) {
    console.error('Agents error:', e);
    return [];
  }
}

export async function getTeamCatalog(
  sessionId?: string,
  workdirName?: string,
): Promise<TeamCatalogEntry[]> {
  try {
    const params = new URLSearchParams();
    if (sessionId) params.set('session_id', sessionId);
    if (workdirName) params.set('workdir_name', workdirName);
    const suffix = params.size > 0 ? `?${params.toString()}` : '';
    const resp = await fetch(`${API_BASE}/api/teams/catalog${suffix}`);
    const data = await resp.json();
    return Array.isArray(data.teams) ? data.teams : [];
  } catch (e) {
    console.error('Team catalog error:', e);
    return [];
  }
}

export type TeamRoutingRule = {
  name: string;
  team_key: string;
  priority: number;
  source: string;
};

export async function getTeamRoutingRules(): Promise<TeamRoutingRule[]> {
  try {
    const resp = await fetch(`${API_BASE}/api/teams/routing/rules`);
    const data = await resp.json();
    return Array.isArray(data.rules) ? data.rules : [];
  } catch (e) {
    console.error('Team routing rules error:', e);
    return [];
  }
}

export async function inspectTeamRouting(payload: {
  message: string;
  requested_agent_name?: string;
  requested_execution_profile?: string;
}): Promise<{
  matched: boolean;
  decision?: Record<string, any> | null;
  rules: TeamRoutingRule[];
}> {
  const resp = await fetch(`${API_BASE}/api/teams/routing/inspect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '检查团队路由失败');
  }
  return {
    matched: Boolean(data.matched),
    decision: data.decision && typeof data.decision === 'object' ? data.decision : null,
    rules: Array.isArray(data.rules) ? data.rules : [],
  };
}

export type ModelOption = {
  id: string;
  provider_id: string;
  model_id: string;
  name: string;
  supports_images?: boolean | null;
};

export type ChatRuntimeOption = {
  runtime_id: string;
  display_name: string;
  available: boolean;
  enabled: boolean;
  release: string;
  models: string[];
  diagnostics?: string[];
  capabilities?: Record<string, unknown>;
  source_type?: 'builtin' | 'profile' | 'entrypoint' | string;
  source_name?: string;
  distribution_name?: string;
  distribution_version?: string;
  definition_digest?: string;
  trust_state?: string;
  compatibility_state?: string;
  conformance_state?: string;
  capabilities_verified?: string[];
};

export async function getChatRuntimes(sessionId?: string): Promise<{
  default_runtime_id: string;
  preferred_model_id: string;
  runtimes: ChatRuntimeOption[];
}> {
  const params = new URLSearchParams();
  if (sessionId) params.set('session_id', sessionId);
  const query = params.toString();
  const resp = await fetch(`${API_BASE}/api/chat/runtimes${query ? `?${query}` : ''}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || `Harness 列表请求失败 (${resp.status})`));
  }
  return {
    default_runtime_id: String(data.default_runtime_id || 'tcode-native'),
    preferred_model_id: String(data.preferred_model_id || ''),
    runtimes: Array.isArray(data.runtimes)
      ? data.runtimes.map((item: any) => ({
          runtime_id: String(item.runtime_id || ''),
          display_name: String(item.display_name || item.runtime_id || ''),
          available: Boolean(item.available),
          enabled: Boolean(item.enabled),
          release: String(item.release || 'beta'),
          models: Array.isArray(item.models)
            ? item.models.map((model: unknown) => String(model || '').trim()).filter(Boolean)
            : [],
          diagnostics: Array.isArray(item.diagnostics)
            ? item.diagnostics.map((value: unknown) => String(value || '')).filter(Boolean)
            : [],
          capabilities: item.capabilities && typeof item.capabilities === 'object'
            ? item.capabilities as Record<string, unknown>
            : {},
          source_type: String(item.source_type || 'builtin'),
          source_name: String(item.source_name || ''),
          distribution_name: String(item.distribution_name || ''),
          distribution_version: String(item.distribution_version || ''),
          definition_digest: String(item.definition_digest || ''),
          trust_state: String(item.trust_state || ''),
          compatibility_state: String(item.compatibility_state || ''),
          conformance_state: String(item.conformance_state || ''),
          capabilities_verified: Array.isArray(item.capabilities_verified)
            ? item.capabilities_verified.map((value: unknown) => String(value || '')).filter(Boolean)
            : [],
        })).filter((item: ChatRuntimeOption) => item.runtime_id)
      : [],
  };
}

export async function getModels(sessionId?: string, workspaceId?: string): Promise<{ current_model: string; models: ModelOption[]; config_scope?: string; config_workdir?: string }> {
  const params = new URLSearchParams();
  if (sessionId) {
    params.set('session_id', sessionId);
  }
  if (workspaceId) {
    params.set('workspace_id', workspaceId);
  }
  const query = params.toString();
  const resp = await fetch(`${API_BASE}/api/models${query ? `?${query}` : ''}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || `模型列表请求失败 (${resp.status})`));
  }
  return {
    current_model: data.current_model || '',
    models: data.models || [],
    config_scope: data.config_scope || '',
    config_workdir: data.config_workdir || '',
  };
}

export async function selectModel(model: string, sessionId?: string, workspaceId?: string): Promise<{ model: string; config_scope?: string; config_workdir?: string }> {
  const params = new URLSearchParams();
  if (workspaceId) {
    params.set('workspace_id', workspaceId);
  }
  const query = params.toString();
  const resp = await fetch(`${API_BASE}/api/models/select${query ? `?${query}` : ''}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ model, session_id: sessionId || null }),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || '切换模型失败');
  }
  const data = await resp.json().catch(() => ({}));
  return {
    model: String(data.model || model || '').trim(),
    config_scope: String(data.config_scope || ''),
    config_workdir: String(data.config_workdir || ''),
  };
}

export type AgentMCPMapping = {
  allowed_tools: 'all' | string[];
  description: string;
};

export async function getAgentMCPMapping(): Promise<{
  source: string;
  agents: Record<string, AgentMCPMapping>;
}> {
  try {
    const resp = await fetch(`${API_BASE}/api/agents/mcp`);
    const data = await resp.json();
    return {
      source: data.source || '',
      agents: data.agents || {},
    };
  } catch (e) {
    console.error('Agent MCP mapping error:', e);
    return {
      source: '',
      agents: {},
    };
  }
}
