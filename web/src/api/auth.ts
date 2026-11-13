/** Authentication and tenant membership requests. */

const API_BASE = '';

export interface AuthUser {
  user_id: string;
  tenant_id: string;
  email: string;
  name?: string;
  role?: string;
  tenant_status?: 'active' | 'inactive';
}

export interface TenantSummary {
  tenant_id: string;
  name: string;
  slug: string;
  status: 'active' | 'inactive';
  role: 'owner' | 'admin' | 'member';
  membership_status: string;
  member_count: number;
  created_at: number;
  updated_at: number;
}

export interface TenantMember {
  user_id: string;
  email: string;
  name: string;
  user_status: string;
  role: 'owner' | 'admin' | 'member';
  membership_status: string;
  created_at: number;
  updated_at: number;
}

async function tenantApi<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(String(data.detail || `租户请求失败 (${response.status})`));
  }
  return data as T;
}

export async function listTenants(): Promise<{ current_tenant_id: string; tenants: TenantSummary[] }> {
  return tenantApi('/api/tenants');
}

export async function createTenant(tenantId: string, name: string): Promise<TenantSummary> {
  const data = await tenantApi<{ tenant: TenantSummary }>('/api/tenants', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenant_id: tenantId, name }),
  });
  return data.tenant;
}

export async function renameTenant(tenantId: string, name: string): Promise<TenantSummary> {
  const data = await tenantApi<{ tenant: TenantSummary }>(`/api/tenants/${encodeURIComponent(tenantId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  return data.tenant;
}

export async function setTenantEnabled(tenantId: string, enabled: boolean): Promise<TenantSummary> {
  const action = enabled ? 'restore' : 'disable';
  const data = await tenantApi<{ tenant: TenantSummary }>(`/api/tenants/${encodeURIComponent(tenantId)}/${action}`, {
    method: 'POST',
  });
  return data.tenant;
}

export async function switchTenant(tenantId: string): Promise<AuthUser> {
  const data = await tenantApi<{ user: AuthUser }>(`/api/tenants/${encodeURIComponent(tenantId)}/switch`, {
    method: 'POST',
  });
  return data.user;
}

export async function listTenantMembers(tenantId: string): Promise<TenantMember[]> {
  const data = await tenantApi<{ members: TenantMember[] }>(`/api/tenants/${encodeURIComponent(tenantId)}/members`);
  return data.members;
}

export async function addTenantMember(
  tenantId: string,
  input: { email: string; name?: string; user_id?: string; password?: string; role: string },
): Promise<TenantMember> {
  const data = await tenantApi<{ member: TenantMember }>(`/api/tenants/${encodeURIComponent(tenantId)}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return data.member;
}

export async function updateTenantMemberRole(
  tenantId: string,
  userId: string,
  role: string,
): Promise<TenantMember> {
  const data = await tenantApi<{ member: TenantMember }>(
    `/api/tenants/${encodeURIComponent(tenantId)}/members/${encodeURIComponent(userId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    },
  );
  return data.member;
}

export async function removeTenantMember(tenantId: string, userId: string): Promise<void> {
  await tenantApi(`/api/tenants/${encodeURIComponent(tenantId)}/members/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
  });
}

export async function deleteTenant(tenantId: string, confirmation: string): Promise<void> {
  await tenantApi(`/api/tenants/${encodeURIComponent(tenantId)}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm_tenant_id: confirmation }),
  });
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const resp = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(String(data.detail || '登录失败'));
  }
  const data = await resp.json();
  return data.user as AuthUser;
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST' });
}

export async function getCurrentUser(): Promise<AuthUser | null> {
  const request = async () => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 8_000);
    try {
      const response = await fetch(`${API_BASE}/api/auth/me`, { signal: controller.signal, cache: 'no-store' });
      // Keep the timeout active until the body is consumed. Electron can receive
      // response headers while leaving the authenticated body stream unsettled.
      const data = await response.json().catch(() => ({}));
      return { response, data };
    }
    finally { window.clearTimeout(timer); }
  };
  let result: Awaited<ReturnType<typeof request>>;
  try { result = await request(); }
  catch (error) {
    if (!(error instanceof DOMException) || error.name !== 'AbortError') throw error;
    // Electron occasionally leaves the first authenticated navigation fetch
    // unsettled even after the backend has written a 200 response. A bounded
    // retry avoids an infinite startup screen without weakening authentication.
    result = await request();
  }
  const { response: resp, data } = result;
  if (resp.status === 401) return null;
  if (!resp.ok) throw new Error('无法读取当前用户');
  return data.user as AuthUser;
}
