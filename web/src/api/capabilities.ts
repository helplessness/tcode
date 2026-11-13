/** MCP servers, skills, plugin commands and health checks. */

const API_BASE = '';

export type MCPServerStatus = {
  name: string;
  status?: string;
  connected: boolean;
  error?: string;
  tools: number;
  callable_tools: number;
  route_mode: 'direct' | 'job_protocol' | 'unknown' | string;
  user_callable: boolean;
  submit_via?: string | null;
  route_label?: string;
  route_reason?: string;
  policy_flags?: Record<string, boolean>;
};

export type MCPToolsResponse = {
  tools: Array<{
    name?: string;
    tool?: string;
    description?: string;
    server?: string;
    inputSchema?: Record<string, unknown>;
  }>;
  direct_call_allowed: boolean;
  route_mode?: string;
  submit_via?: string | null;
  message?: string;
};

export type MCPFileTransfer = {
  receiver: 'artifact_refs' | 'server_paths';
  tools: Array<{name: string; files: Array<{pointer: string; kind: 'file' | 'directory'; transport: 'artifact'}>}>;
};
export type MCPFileDiscovery = Array<{name: string; inputSchema: Record<string, unknown>; fields: Array<{pointer: string; label: string; schema: Record<string, unknown>}>}>;

export type MCPConfigServer = {
  name: string;
  url: string;
  transport: 'streamable-http' | 'sse';
  enabled: boolean;
  invocation_mode: 'auto' | 'direct' | 'async';
  submit_via: string;
  connect_timeout: number;
  timeout: number;
  header_names: string[];
  dynamic_headers?: DynamicHeaders;
  editable?: boolean;
  managed_message?: string;
  file_transfer?: MCPFileTransfer | null;
  file_transfer_status?: 'adapter_required' | 'disabled' | 'registered' | 'activation_required';
};

export type DynamicHeaders = Record<string, { variable: string; description: string; extraction_hint: string; required: boolean; max_length: number }>;

async function mcpConfigRequest(path = '', init?: RequestInit) {
  const resp = await fetch(`${API_BASE}/api/mcp/config${path}`, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(String(data.detail || 'MCP 配置操作失败'));
  return data;
}

export async function getMCPConfig(workdirName?: string): Promise<{ servers: MCPConfigServer[]; can_edit: boolean; config_file: string }> {
  return mcpConfigRequest(workdirName ? `?workdir_name=${encodeURIComponent(workdirName)}` : '');
}

export async function saveMCPConfig(name: string, config: Omit<MCPConfigServer, 'name' | 'header_names'> & { headers?: Record<string, string>; remove_headers?: string[] }, workdirName?: string) {
  return mcpConfigRequest(`/${encodeURIComponent(name)}${workdirName ? `?workdir_name=${encodeURIComponent(workdirName)}` : ''}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config),
  });
}

export async function deleteMCPConfig(name: string, workdirName?: string) {
  return mcpConfigRequest(`/${encodeURIComponent(name)}${workdirName ? `?workdir_name=${encodeURIComponent(workdirName)}` : ''}`, { method: 'DELETE' });
}

export async function reloadMCPConfig() {
  return mcpConfigRequest('/reload', { method: 'POST' });
}

export async function discoverMCPFiles(config: {name: string; url: string; transport: string; headers: Record<string, string>}): Promise<{tools: MCPFileDiscovery}> {
  return mcpConfigRequest('/discover-files', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(config)});
}

export async function getMCPServers(options: { refreshFailed?: boolean } = {}): Promise<MCPServerStatus[]> {
  try {
    const suffix = options.refreshFailed ? '?refresh_failed=true' : '';
    const resp = await fetch(`${API_BASE}/api/mcp/status${suffix}`);
    const data = await resp.json();
    return data.servers || [];
  } catch (e) {
    console.error('MCP status error:', e);
    return [];
  }
}

export async function getMCPTools(serverName: string): Promise<MCPToolsResponse> {
  try {
    const resp = await fetch(`${API_BASE}/api/mcp/tools?server=${encodeURIComponent(serverName)}`);
    const data = await resp.json();
    return {
      tools: data.tools || [],
      direct_call_allowed: Boolean(data.direct_call_allowed),
      route_mode: data.route_mode || '',
      submit_via: data.submit_via ?? null,
      message: data.message || '',
    };
  } catch (e) {
    console.error('MCP tools error:', e);
    return {
      tools: [],
      direct_call_allowed: false,
      route_mode: '',
      submit_via: null,
      message: '加载 MCP 工具失败',
    };
  }
}

export type SkillAgentState = {
  available: boolean;
  enabled: boolean;
};

export type SkillCatalogAgent = {
  name: string;
  display_name: string;
  description: string;
  mode: string;
};

export type SkillCatalogItem = {
  name: string;
  skill_id: string;
  description: string;
  location: string;
  source: string;
  source_kind: string;
  plugin: string;
  version: string;
  category: string;
  tags: string[];
  required_agent: string;
  agent_states: Record<string, SkillAgentState>;
};

export type SkillPackageFile = {
  name: string;
  path: string;
  relative_path: string;
  size: number;
  primary: boolean;
};

export type SkillDetail = SkillCatalogItem & {
  content: string;
  files: SkillPackageFile[];
};

export type SkillCatalogResponse = {
  skills: SkillCatalogItem[];
  agents: SkillCatalogAgent[];
  activation?: {
    version: number;
    agents: Record<string, { disabled?: string[] }>;
    updated_at?: string;
  };
};

const skillWorkspaceQuery = (workspaceId = '') => {
  const value = String(workspaceId || '').trim();
  return value ? `?workspace_id=${encodeURIComponent(value)}` : '';
};

export async function getSkillCatalog(workspaceId = ''): Promise<SkillCatalogResponse> {
  const resp = await fetch(`${API_BASE}/api/skills${skillWorkspaceQuery(workspaceId)}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || '无法加载技能目录'));
  }
  return {
    skills: Array.isArray(data.skills) ? data.skills : [],
    agents: Array.isArray(data.agents) ? data.agents : [],
    activation: data.activation,
  };
}

export async function getSkills(workspaceId = ''): Promise<SkillCatalogItem[]> {
  try {
    return (await getSkillCatalog(workspaceId)).skills;
  } catch (e) {
    console.error('Skills error:', e);
    return [];
  }
}

export async function getSkillDetail(skillName: string, workspaceId = ''): Promise<SkillDetail> {
  const resp = await fetch(`${API_BASE}/api/skills/${encodeURIComponent(skillName)}${skillWorkspaceQuery(workspaceId)}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || '无法加载技能详情'));
  }
  return data as SkillDetail;
}

export async function setSkillActivation(
  skillName: string,
  agentName: string,
  enabled: boolean,
  workspaceId = '',
): Promise<{ name: string; agent: string; enabled: boolean; agent_states: Record<string, SkillAgentState> }> {
  const resp = await fetch(`${API_BASE}/api/skills/${encodeURIComponent(skillName)}/activation`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent: agentName, enabled, workspace_id: workspaceId }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || '无法更新技能状态'));
  }
  return data;
}

export type SkillSourceCandidate = {
  name: string; description: string; repository: string; path: string;
  source_url: string; install_path: string; ref: string;
};
export type SkillSourcePreview = {
  page_url: string; candidates: SkillSourceCandidate[]; warnings: string[]; notice: string;
};

export async function resolveSkillSource(url: string, workspaceId = '', signal?: AbortSignal): Promise<SkillSourcePreview> {
  const resp = await fetch(`${API_BASE}/api/skills/resolve-source`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, workspace_id: workspaceId }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(String(data.detail || '无法解析技能来源'));
  return data;
}

export async function installSkill(sourceUrl: string, workspaceId = '', discovery?: {
  introduction_url: string; path: string; ref: string; confirmed: true;
}): Promise<{
  name: string;
  description: string;
  location: string;
  source: string;
  scope: string;
}> {
  const resp = await fetch(`${API_BASE}/api/skills/install`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_url: sourceUrl, workspace_id: workspaceId, scope: 'workspace', method: 'auto', ...discovery }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || 'Skill 安装失败'));
  }
  return data;
}

export type PluginCommand = {
  name: string;
  plugin: string;
  description: string;
  argument_hint?: string;
  agent?: string;
  source: string;
};

export async function getPluginCommands(workdir?: string): Promise<PluginCommand[]> {
  try {
    const search = new URLSearchParams();
    const normalizedWorkdir = String(workdir || '').trim();
    if (normalizedWorkdir) {
      search.set('workdir', normalizedWorkdir);
    }
    const query = search.toString();
    const resp = await fetch(
      `${API_BASE}/api/plugins/commands${query ? `?${query}` : ''}`,
    );
    const data = await resp.json();
    return data.commands || [];
  } catch (e) {
    console.error('Plugin commands error:', e);
    return [];
  }
}

export async function getHealth() {
  try {
    const resp = await fetch(`${API_BASE}/api/health`);
    const data = await resp.json();
    return data;
  } catch (e) {
    console.error('Health error:', e);
    return null;
  }
}
