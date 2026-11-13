import { confirmAction } from './FeedbackDialog';
import { For, Show, createMemo, createSignal, onMount } from 'solid-js';
import {
  addTenantMember,
  createTenant,
  deleteTenant,
  listTenantMembers,
  listTenants,
  removeTenantMember,
  renameTenant,
  setTenantEnabled,
  switchTenant,
  updateTenantMemberRole,
  type AuthUser,
  type TenantMember,
  type TenantSummary,
} from '../api/client';

interface TenantManagementPanelProps {
  currentUser: AuthUser;
  onClose: () => void;
  onTenantSwitched: (user: AuthUser) => void;
}

const roleLabel = (role: string) => ({ owner: '所有者', admin: '管理员', member: '成员' }[role] || role);

export default function TenantManagementPanel(props: TenantManagementPanelProps) {
  const [tenants, setTenants] = createSignal<TenantSummary[]>([]);
  const [selectedTenantId, setSelectedTenantId] = createSignal(props.currentUser.tenant_id);
  const [members, setMembers] = createSignal<TenantMember[]>([]);
  const [loading, setLoading] = createSignal(true);
  const [membersLoading, setMembersLoading] = createSignal(false);
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');

  const [newTenantId, setNewTenantId] = createSignal('');
  const [newTenantName, setNewTenantName] = createSignal('');
  const [renameValue, setRenameValue] = createSignal('');
  const [deleteConfirmation, setDeleteConfirmation] = createSignal('');

  const [memberEmail, setMemberEmail] = createSignal('');
  const [memberName, setMemberName] = createSignal('');
  const [memberUserId, setMemberUserId] = createSignal('');
  const [memberPassword, setMemberPassword] = createSignal('');
  const [memberRole, setMemberRole] = createSignal<'member' | 'admin' | 'owner'>('member');

  const selectedTenant = createMemo(() => (
    tenants().find((tenant) => tenant.tenant_id === selectedTenantId()) || null
  ));
  const canManageSelected = createMemo(() => (
    ['owner', 'admin'].includes(selectedTenant()?.role || '')
  ));

  const showError = (value: unknown) => {
    setError(value instanceof Error ? value.message : '操作失败');
    setNotice('');
  };

  const loadMembers = async (tenant = selectedTenant()) => {
    if (!tenant || !['owner', 'admin'].includes(tenant.role)) {
      setMembers([]);
      return;
    }
    setMembersLoading(true);
    try {
      setMembers(await listTenantMembers(tenant.tenant_id));
    } catch (value) {
      showError(value);
      setMembers([]);
    } finally {
      setMembersLoading(false);
    }
  };

  const loadAll = async (preferredTenantId = selectedTenantId()) => {
    setLoading(true);
    setError('');
    try {
      const result = await listTenants();
      setTenants(result.tenants || []);
      const nextId = result.tenants.some((tenant) => tenant.tenant_id === preferredTenantId)
        ? preferredTenantId
        : (result.current_tenant_id || result.tenants[0]?.tenant_id || '');
      setSelectedTenantId(nextId);
      const nextTenant = result.tenants.find((tenant) => tenant.tenant_id === nextId) || null;
      setRenameValue(nextTenant?.name || '');
      setDeleteConfirmation('');
      await loadMembers(nextTenant);
    } catch (value) {
      showError(value);
    } finally {
      setLoading(false);
    }
  };

  const selectTenant = async (tenant: TenantSummary) => {
    setSelectedTenantId(tenant.tenant_id);
    setRenameValue(tenant.name);
    setDeleteConfirmation('');
    setError('');
    setNotice('');
    await loadMembers(tenant);
  };

  const run = async (key: string, action: () => Promise<void>, message: string) => {
    setBusy(key);
    setError('');
    setNotice('');
    try {
      await action();
      setNotice(message);
    } catch (value) {
      showError(value);
    } finally {
      setBusy('');
    }
  };

  const handleCreateTenant = async (event: SubmitEvent) => {
    event.preventDefault();
    const tenantId = newTenantId().trim();
    await run('create-tenant', async () => {
      await createTenant(tenantId, newTenantName().trim());
      setNewTenantId('');
      setNewTenantName('');
      await loadAll(tenantId);
    }, '租户已创建，你是该租户的所有者。');
  };

  const handleRename = async () => {
    const tenant = selectedTenant();
    if (!tenant) return;
    await run('rename', async () => {
      await renameTenant(tenant.tenant_id, renameValue().trim());
      await loadAll(tenant.tenant_id);
    }, '租户展示名称已更新，租户 ID 和目录路径保持不变。');
  };

  const handleStatus = async (enabled: boolean) => {
    const tenant = selectedTenant();
    if (!tenant) return;
    await run(enabled ? 'restore' : 'disable', async () => {
      await setTenantEnabled(tenant.tenant_id, enabled);
      if (enabled && tenant.tenant_id === props.currentUser.tenant_id) {
        props.onTenantSwitched(await switchTenant(tenant.tenant_id));
        return;
      }
      await loadAll(tenant.tenant_id);
    }, enabled ? '租户已恢复。' : '租户已停用，任务与 Workspace 访问已锁定。');
  };

  const handleSwitch = async () => {
    const tenant = selectedTenant();
    if (!tenant) return;
    await run('switch', async () => {
      const user = await switchTenant(tenant.tenant_id);
      props.onTenantSwitched(user);
    }, '已切换当前租户。');
  };

  const handleDelete = async () => {
    const tenant = selectedTenant();
    if (!tenant) return;
    await run('delete', async () => {
      await deleteTenant(tenant.tenant_id, deleteConfirmation());
      await loadAll(props.currentUser.tenant_id);
    }, '租户及其运行数据已永久删除。');
  };

  const handleAddMember = async (event: SubmitEvent) => {
    event.preventDefault();
    const tenant = selectedTenant();
    if (!tenant) return;
    await run('add-member', async () => {
      await addTenantMember(tenant.tenant_id, {
        email: memberEmail().trim(),
        name: memberName().trim(),
        user_id: memberUserId().trim(),
        password: memberPassword(),
        role: memberRole(),
      });
      setMemberEmail('');
      setMemberName('');
      setMemberUserId('');
      setMemberPassword('');
      setMemberRole('member');
      await loadMembers(tenant);
      await loadAll(tenant.tenant_id);
    }, '成员已加入租户；已存在的账号无需重新设置密码。');
  };

  const handleRoleChange = async (member: TenantMember, role: string) => {
    const tenant = selectedTenant();
    if (!tenant) return;
    await run(`role-${member.user_id}`, async () => {
      await updateTenantMemberRole(tenant.tenant_id, member.user_id, role);
      await loadMembers(tenant);
      await loadAll(tenant.tenant_id);
    }, '成员角色已更新。');
  };

  const handleRemoveMember = async (member: TenantMember) => {
    const tenant = selectedTenant();
    if (!tenant || !await confirmAction(`确认将 ${member.email} 移出 ${tenant.name}？`)) return;
    await run(`remove-${member.user_id}`, async () => {
      await removeTenantMember(tenant.tenant_id, member.user_id);
      await loadMembers(tenant);
      await loadAll(tenant.tenant_id);
    }, '成员已移除，其账号和其他租户关系不受影响。');
  };

  onMount(() => void loadAll(props.currentUser.tenant_id));

  return (
    <div class="fixed inset-0 z-[90] flex items-center justify-center bg-black/70 p-3 backdrop-blur-sm" onMouseDown={(event) => {
      if (event.target === event.currentTarget) props.onClose();
    }}>
      <div class="flex h-[min(860px,94vh)] w-[min(1180px,96vw)] min-w-0 flex-col overflow-hidden rounded-2xl border border-[#303a42] bg-[#11171b] text-[#dee3e8] shadow-[0_30px_100px_rgba(0,0,0,0.62)]">
        <header class="flex items-center justify-between gap-4 border-b border-[#2a3136] px-5 py-4">
          <div>
            <h2 class="text-lg font-semibold text-[#f7fafc]">个人与租户管理</h2>
            <p class="mt-1 text-xs text-[#8f9da7]">当前账号：{props.currentUser.email} · 用户 ID：{props.currentUser.user_id}</p>
          </div>
          <button type="button" class="rounded border border-[#3a434a] px-3 py-1.5 text-xs hover:border-[#7bd0ff]/50 hover:text-[#8ed5ff]" onClick={props.onClose}>关闭</button>
        </header>

        <Show when={error()}>
          <div class="mx-5 mt-3 rounded border border-[#ef4444]/35 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#fca5a5]">{error()}</div>
        </Show>
        <Show when={notice()}>
          <div class="mx-5 mt-3 rounded border border-[#22c55e]/30 bg-[#22c55e]/10 px-3 py-2 text-sm text-[#86efac]">{notice()}</div>
        </Show>

        <div class="grid min-h-0 flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[320px_minmax(0,1fr)]">
          <aside class="min-h-0 overflow-y-auto border-b border-[#2a3136] bg-[#0d1216]/75 p-4 lg:border-b-0 lg:border-r">
            <div class="mb-3 flex items-center justify-between">
              <h3 class="text-xs font-bold uppercase tracking-[0.18em] text-[#8f9da7]">我的租户</h3>
              <span class="text-xs text-[#65737d]">{tenants().length}</span>
            </div>
            <Show when={!loading()} fallback={<div class="py-8 text-center text-sm text-[#8f9da7]">正在加载租户...</div>}>
              <div class="space-y-2">
                <For each={tenants()}>
                  {(tenant) => (
                    <button
                      type="button"
                      class={`w-full rounded-xl border p-3 text-left transition ${selectedTenantId() === tenant.tenant_id ? 'border-[#38bdf8]/55 bg-[#38bdf8]/10' : 'border-[#2d353b] bg-[#151b1f] hover:border-[#51606a]'}`}
                      onClick={() => void selectTenant(tenant)}
                    >
                      <div class="flex items-start justify-between gap-2">
                        <div class="min-w-0">
                          <div class="truncate text-sm font-semibold text-[#edf4f8]">{tenant.name}</div>
                          <div class="mt-1 truncate font-mono text-[11px] text-[#81909a]">{tenant.tenant_id}</div>
                        </div>
                        <span class={`rounded-full border px-2 py-0.5 text-[10px] ${tenant.status === 'active' ? 'border-[#22c55e]/30 bg-[#22c55e]/10 text-[#86efac]' : 'border-[#f59e0b]/35 bg-[#f59e0b]/10 text-[#fcd34d]'}`}>
                          {tenant.status === 'active' ? '启用' : '停用'}
                        </span>
                      </div>
                      <div class="mt-2 flex items-center justify-between text-[11px] text-[#81909a]">
                        <span>{roleLabel(tenant.role)}</span>
                        <span>{tenant.member_count} 位成员</span>
                      </div>
                      <Show when={tenant.tenant_id === props.currentUser.tenant_id}>
                        <div class="mt-2 text-[10px] font-semibold text-[#7bd0ff]">当前租户</div>
                      </Show>
                    </button>
                  )}
                </For>
              </div>
            </Show>

            <form class="mt-5 space-y-2 rounded-xl border border-[#2d353b] bg-[#151b1f] p-3" onSubmit={handleCreateTenant}>
              <h4 class="text-sm font-semibold text-[#edf4f8]">创建新租户</h4>
              <input class="h-9 w-full rounded border border-[#364048] bg-[#0d1216] px-3 text-xs outline-none focus:border-[#38bdf8]/60" placeholder="租户 ID，例如 lab-a" value={newTenantId()} onInput={(event) => setNewTenantId(event.currentTarget.value)} required />
              <input class="h-9 w-full rounded border border-[#364048] bg-[#0d1216] px-3 text-xs outline-none focus:border-[#38bdf8]/60" placeholder="展示名称，例如 蛋白设计实验室" value={newTenantName()} onInput={(event) => setNewTenantName(event.currentTarget.value)} required />
              <p class="text-[10px] leading-4 text-[#6f7d87]">租户 ID 将进入目录路径，创建后不可修改。</p>
              <button class="h-9 w-full rounded border border-[#38bdf8]/35 bg-[#38bdf8]/12 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50" disabled={Boolean(busy())}>{busy() === 'create-tenant' ? '创建中...' : '创建租户'}</button>
            </form>
          </aside>

          <main class="min-h-0 overflow-y-auto p-4 md:p-5">
            <Show when={selectedTenant()} fallback={<div class="py-20 text-center text-sm text-[#8f9da7]">请选择或创建租户。</div>}>
              {(tenantAccessor) => {
                const tenant = () => tenantAccessor();
                return (
                  <div class="space-y-5">
                    <section class="rounded-xl border border-[#2d353b] bg-[#151b1f] p-4">
                      <div class="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h3 class="text-base font-semibold text-[#f7fafc]">{tenant().name}</h3>
                          <p class="mt-1 font-mono text-xs text-[#81909a]">workdirs/{tenant().tenant_id}/&lt;user_id&gt;/&lt;workspace_id&gt;</p>
                        </div>
                        <div class="flex flex-wrap gap-2">
                          <Show when={tenant().tenant_id !== props.currentUser.tenant_id && tenant().status === 'active'}>
                            <button class="rounded border border-[#38bdf8]/35 bg-[#38bdf8]/10 px-3 py-1.5 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50" disabled={Boolean(busy())} onClick={() => void handleSwitch()}>{busy() === 'switch' ? '切换中...' : '切换到此租户'}</button>
                          </Show>
                          <Show when={canManageSelected()}>
                            <button class={`rounded border px-3 py-1.5 text-xs font-semibold disabled:opacity-50 ${tenant().status === 'active' ? 'border-[#f59e0b]/35 bg-[#f59e0b]/10 text-[#fcd34d]' : 'border-[#22c55e]/35 bg-[#22c55e]/10 text-[#86efac]'}`} disabled={Boolean(busy()) || (tenant().status === 'active' && tenant().tenant_id === props.currentUser.tenant_id)} onClick={() => void handleStatus(tenant().status !== 'active')} title={tenant().status === 'active' && tenant().tenant_id === props.currentUser.tenant_id ? '请先切换到另一个租户' : ''}>
                              {tenant().status === 'active' ? (tenant().tenant_id === props.currentUser.tenant_id ? '先切换再停用' : '停用租户') : '恢复租户'}
                            </button>
                          </Show>
                        </div>
                      </div>
                      <Show when={canManageSelected()}>
                        <div class="mt-4 flex flex-col gap-2 sm:flex-row">
                          <input class="h-9 min-w-0 flex-1 rounded border border-[#364048] bg-[#0d1216] px-3 text-sm outline-none focus:border-[#38bdf8]/60" value={renameValue()} onInput={(event) => setRenameValue(event.currentTarget.value)} />
                          <button class="h-9 rounded border border-[#4b5560] px-3 text-xs font-semibold hover:border-[#7bd0ff]/50 disabled:opacity-50" disabled={Boolean(busy()) || renameValue().trim() === tenant().name} onClick={() => void handleRename()}>保存名称</button>
                        </div>
                      </Show>
                    </section>

                    <Show when={canManageSelected()} fallback={<section class="rounded-xl border border-[#2d353b] bg-[#151b1f] p-4 text-sm text-[#8f9da7]">你是该租户的普通成员，可以切换和使用自己的 Workspace，但不能管理租户或成员。</section>}>
                      <section class="rounded-xl border border-[#2d353b] bg-[#151b1f] p-4">
                        <div class="mb-3 flex items-center justify-between">
                          <div>
                            <h3 class="text-sm font-semibold text-[#f7fafc]">成员管理</h3>
                            <p class="mt-1 text-xs text-[#81909a]">已有账号只需填写邮箱；新账号还需要至少 12 位的初始密码。</p>
                          </div>
                          <span class="text-xs text-[#81909a]">{members().length} 人</span>
                        </div>
                        <Show when={!membersLoading()} fallback={<div class="py-6 text-center text-xs text-[#81909a]">加载成员...</div>}>
                          <div class="space-y-2">
                            <For each={members()}>
                              {(member) => (
                                <div class="flex flex-col gap-3 rounded-lg border border-[#2d353b] bg-[#0f1418] p-3 sm:flex-row sm:items-center">
                                  <div class="min-w-0 flex-1">
                                    <div class="truncate text-sm font-medium text-[#edf4f8]">{member.name || member.email}</div>
                                    <div class="mt-1 truncate text-[11px] text-[#81909a]">{member.email} · {member.user_id}</div>
                                  </div>
                                  <select
                                    class="h-8 rounded border border-[#364048] bg-[#151b1f] px-2 text-xs outline-none disabled:opacity-60"
                                    value={member.role}
                                    disabled={Boolean(busy()) || (tenant().role !== 'owner' && member.role === 'owner')}
                                    onChange={(event) => void handleRoleChange(member, event.currentTarget.value)}
                                  >
                                    <option value="member">成员</option>
                                    <option value="admin">管理员</option>
                                    <Show when={tenant().role === 'owner'}><option value="owner">所有者</option></Show>
                                  </select>
                                  <button
                                    class="h-8 rounded border border-[#ef4444]/30 px-2 text-xs text-[#fca5a5] hover:bg-[#ef4444]/10 disabled:opacity-40"
                                    disabled={Boolean(busy()) || member.user_id === props.currentUser.user_id || (tenant().role !== 'owner' && member.role === 'owner')}
                                    onClick={() => void handleRemoveMember(member)}
                                  >
                                    移除
                                  </button>
                                </div>
                              )}
                            </For>
                          </div>
                        </Show>

                        <Show when={tenant().status === 'active'}>
                          <form class="mt-4 grid grid-cols-1 gap-2 border-t border-[#2d353b] pt-4 sm:grid-cols-2" onSubmit={handleAddMember}>
                            <input class="h-9 rounded border border-[#364048] bg-[#0d1216] px-3 text-xs outline-none focus:border-[#38bdf8]/60" type="email" placeholder="成员邮箱（必填）" value={memberEmail()} onInput={(event) => setMemberEmail(event.currentTarget.value)} required />
                            <input class="h-9 rounded border border-[#364048] bg-[#0d1216] px-3 text-xs outline-none focus:border-[#38bdf8]/60" placeholder="姓名（新账号可填）" value={memberName()} onInput={(event) => setMemberName(event.currentTarget.value)} />
                            <input class="h-9 rounded border border-[#364048] bg-[#0d1216] px-3 text-xs outline-none focus:border-[#38bdf8]/60" placeholder="用户 ID（新账号可填）" value={memberUserId()} onInput={(event) => setMemberUserId(event.currentTarget.value)} />
                            <input class="h-9 rounded border border-[#364048] bg-[#0d1216] px-3 text-xs outline-none focus:border-[#38bdf8]/60" type="password" placeholder="初始密码（仅新账号必填）" value={memberPassword()} onInput={(event) => setMemberPassword(event.currentTarget.value)} />
                            <select class="h-9 rounded border border-[#364048] bg-[#0d1216] px-3 text-xs outline-none" value={memberRole()} onChange={(event) => setMemberRole(event.currentTarget.value as 'member' | 'admin' | 'owner')}>
                              <option value="member">成员</option>
                              <option value="admin">管理员</option>
                              <Show when={tenant().role === 'owner'}><option value="owner">所有者</option></Show>
                            </select>
                            <button class="h-9 rounded border border-[#38bdf8]/35 bg-[#38bdf8]/10 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50" disabled={Boolean(busy())}>{busy() === 'add-member' ? '添加中...' : '添加成员'}</button>
                          </form>
                        </Show>
                      </section>
                    </Show>

                    <Show when={tenant().role === 'owner'}>
                      <section class="rounded-xl border border-[#ef4444]/25 bg-[#2a1215]/35 p-4">
                        <h3 class="text-sm font-semibold text-[#fca5a5]">危险操作</h3>
                        <p class="mt-1 text-xs leading-5 text-[#c99498]">永久删除会清理该租户的 Workspace、会话、定时任务和 Sandbox Runtime。必须先停用，并切换到另一个租户。</p>
                        <div class="mt-3 flex flex-col gap-2 sm:flex-row">
                          <input class="h-9 min-w-0 flex-1 rounded border border-[#ef4444]/25 bg-[#0d1216] px-3 font-mono text-xs outline-none focus:border-[#ef4444]/60" placeholder={`输入 ${tenant().tenant_id} 确认`} value={deleteConfirmation()} onInput={(event) => setDeleteConfirmation(event.currentTarget.value)} />
                          <button
                            class="h-9 rounded border border-[#ef4444]/35 bg-[#ef4444]/10 px-3 text-xs font-semibold text-[#fca5a5] disabled:cursor-not-allowed disabled:opacity-35"
                            disabled={Boolean(busy()) || tenant().status !== 'inactive' || tenant().tenant_id === props.currentUser.tenant_id || deleteConfirmation() !== tenant().tenant_id}
                            onClick={() => void handleDelete()}
                          >
                            {busy() === 'delete' ? '删除中...' : '永久删除租户'}
                          </button>
                        </div>
                      </section>
                    </Show>
                  </div>
                );
              }}
            </Show>
          </main>
        </div>
      </div>
    </div>
  );
}
