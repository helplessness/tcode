import { For, Show, createMemo, createSignal, onMount } from 'solid-js';
import {
  getSkillCatalog,
  getSkillDetail,
  setSkillActivation,
  type SkillCatalogAgent,
  type SkillCatalogItem,
  type SkillDetail,
} from '../api/client';
import MarkdownContent from './MarkdownContent';
import SkillInstallForm from './SkillInstallForm';

type SkillLibraryProps = {
  initialAgent?: string;
  initialSkill?: string;
  workspaceId?: string;
  onClose: () => void;
};

type SkillView = 'active' | 'disabled' | 'all';
type DetailTab = 'overview' | 'files';

const sourceLabels: Record<string, string> = {
  workspace: '工作区',
  parent: '父级项目',
  home: '个人',
  plugin: '插件',
};

const modeLabels: Record<string, string> = {
  primary: '主 Agent',
  manager: '管理 Agent',
  subagent: '子 Agent',
  worker: '工作 Agent',
};

const formatFileSize = (size: number) => {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
};

export default function SkillLibrary(props: SkillLibraryProps) {
  const [agents, setAgents] = createSignal<SkillCatalogAgent[]>([]);
  const [skills, setSkills] = createSignal<SkillCatalogItem[]>([]);
  const [selectedAgent, setSelectedAgent] = createSignal('build');
  const [selectedSkill, setSelectedSkill] = createSignal('');
  const [detail, setDetail] = createSignal<SkillDetail | null>(null);
  const [view, setView] = createSignal<SkillView>('active');
  const [detailTab, setDetailTab] = createSignal<DetailTab>('overview');
  const [query, setQuery] = createSignal('');
  const [loading, setLoading] = createSignal(true);
  const [detailLoading, setDetailLoading] = createSignal(false);
  const [mutating, setMutating] = createSignal('');
  const [error, setError] = createSignal('');
  const [installOpen, setInstallOpen] = createSignal(false);
  let detailRequest = 0;

  const stateFor = (skill: SkillCatalogItem, agentName = selectedAgent()) =>
    skill.agent_states?.[agentName] || { available: false, enabled: false };

  const activeCountFor = (agentName: string) =>
    skills().filter((skill) => stateFor(skill, agentName).enabled).length;

  const disabledCount = createMemo(() =>
    skills().filter((skill) => {
      const state = stateFor(skill);
      return state.available && !state.enabled;
    }).length,
  );

  const availableCount = createMemo(() =>
    skills().filter((skill) => stateFor(skill).available).length,
  );

  const filteredSkills = createMemo(() => {
    const search = query().trim().toLowerCase();
    return skills().filter((skill) => {
      const state = stateFor(skill);
      if (!state.available) return false;
      if (view() === 'active' && !state.enabled) return false;
      if (view() === 'disabled' && state.enabled) return false;
      if (!search) return true;
      return [skill.name, skill.description, skill.category, skill.plugin, ...skill.tags]
        .join(' ')
        .toLowerCase()
        .includes(search);
    });
  });

  const currentAgent = createMemo(() =>
    agents().find((agent) => agent.name === selectedAgent()) || null,
  );

  const openSkill = async (skillName: string) => {
    if (!skillName) {
      setSelectedSkill('');
      setDetail(null);
      return;
    }
    setSelectedSkill(skillName);
    setDetailTab('overview');
    setDetailLoading(true);
    setError('');
    const requestId = ++detailRequest;
    try {
      const data = await getSkillDetail(skillName, props.workspaceId);
      if (requestId === detailRequest) setDetail(data);
    } catch (err) {
      if (requestId === detailRequest) {
        setDetail(null);
        setError(err instanceof Error ? err.message : '无法加载技能详情');
      }
    } finally {
      if (requestId === detailRequest) setDetailLoading(false);
    }
  };

  const selectAgent = (agentName: string) => {
    setSelectedAgent(agentName);
    setView('active');
    setQuery('');
    const first = skills().find((skill) => stateFor(skill, agentName).enabled);
    void openSkill(first?.name || '');
  };

  const loadCatalog = async () => {
    setLoading(true);
    setError('');
    try {
      const catalog = await getSkillCatalog(props.workspaceId);
      setAgents(catalog.agents);
      setSkills(catalog.skills);
      const requested = String(props.initialAgent || '').trim();
      const initial = catalog.agents.some((agent) => agent.name === requested)
        ? requested
        : (catalog.agents.find((agent) => agent.name === 'build')?.name || catalog.agents[0]?.name || '');
      setSelectedAgent(initial);
      const first = catalog.skills.find((skill) => skill.agent_states?.[initial]?.enabled);
      if (props.initialSkill && catalog.skills.some(skill => skill.name === props.initialSkill)) {
        setView('all'); setQuery(props.initialSkill); await openSkill(props.initialSkill);
      } else await openSkill(first?.name || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法加载技能目录');
    } finally {
      setLoading(false);
    }
  };

  const toggleSkill = async (skill: SkillCatalogItem, enabled: boolean) => {
    const agentName = selectedAgent();
    if (!agentName || mutating()) return;
    setMutating(skill.name);
    setError('');
    try {
      const result = await setSkillActivation(skill.name, agentName, enabled, props.workspaceId);
      setSkills((current) => current.map((item) => (
        item.name === skill.name
          ? { ...item, agent_states: result.agent_states }
          : item
      )));
      setDetail((current) => (
        current?.name === skill.name
          ? { ...current, agent_states: result.agent_states }
          : current
      ));

      if (!enabled && view() === 'active' && selectedSkill() === skill.name) {
        const next = skills().find((item) => (
          item.name !== skill.name && stateFor(item, agentName).enabled
        ));
        await openSkill(next?.name || '');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法更新技能状态');
    } finally {
      setMutating('');
    }
  };

  const selectView = (nextView: SkillView) => {
    setView(nextView);
    const first = skills().find((skill) => {
      const state = stateFor(skill);
      if (!state.available) return false;
      if (nextView === 'active') return state.enabled;
      if (nextView === 'disabled') return !state.enabled;
      return true;
    });
    void openSkill(first?.name || '');
  };

  onMount(() => {
    void loadCatalog();
  });

  return (
    <div class="skill-workspace fixed inset-0 z-[80] flex flex-col bg-[#0c1115] text-[#dee3e8]">
      <header class="flex h-16 shrink-0 items-center justify-between border-b border-[#2a3136] bg-[#0f1418]/96 px-5 backdrop-blur-xl">
        <div class="flex min-w-0 items-center gap-4">
          <button
            type="button"
            onClick={props.onClose}
            class="inline-flex h-10 items-center gap-2 rounded-xl border border-[#303539] bg-[#171c20] px-3 text-sm font-semibold text-[#bdc8d1] transition hover:border-[#7bd0ff]/40 hover:text-[#8ed5ff]"
          >
            <span aria-hidden="true">←</span>
            返回控制台
          </button>
          <div class="min-w-0">
            <div class="truncate text-lg font-black tracking-tight text-[#f7fafc]">Skill Center</div>
            <div class="text-[10px] uppercase tracking-[0.26em] text-[#6f7d87]">多 Agent 技能管理</div>
          </div>
        </div>
        <div class="flex items-center gap-2 text-xs text-[#8b99a3]">
          <button type="button" onClick={()=>setInstallOpen(value=>!value)} class="skill-install-button rounded-lg border border-[#486442] bg-[#486442] px-3 py-2 font-semibold text-white hover:bg-[#375532]">从网址安装</button>
          <span class="rounded-full border border-[#2f3a41] bg-[#151b1f] px-3 py-1.5">
            {skills().length} 个技能
          </span>
          <span class="hidden rounded-full border border-[#2f3a41] bg-[#151b1f] px-3 py-1.5 sm:inline">
            {agents().length} 个 Agent
          </span>
        </div>
      </header>

      <Show when={installOpen()}><SkillInstallForm workspaceId={props.workspaceId} onClose={() => setInstallOpen(false)}
        onInstalled={async name => { await loadCatalog(); await openSkill(name); setView('all'); }} /></Show>

      <Show when={error()}>
        <div class="flex shrink-0 items-center justify-between border-b border-[#7b3d3a] bg-[#2a1718] px-5 py-2 text-xs text-[#ffb3ad]">
          <span>{error()}</span>
          <button type="button" class="font-semibold" onClick={() => setError('')}>关闭</button>
        </div>
      </Show>

      <main class="skill-center-layout min-h-0 flex-1">
        <aside class="skill-agent-column min-h-0 overflow-y-auto border-r border-[#2a3136] bg-[#11171b] p-4">
          <div class="mb-3 px-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-[#6f7d87]">Agent 视图</div>
          <div class="space-y-1.5">
            <For each={agents()}>
              {(agent) => (
                <button
                  type="button"
                  onClick={() => selectAgent(agent.name)}
                  class={`w-full rounded-2xl border px-3 py-3 text-left transition ${
                    selectedAgent() === agent.name
                      ? 'border-[#38bdf8]/35 bg-[#38bdf8]/10 text-[#f7fafc]'
                      : 'border-transparent text-[#aab6be] hover:border-[#2f3a41] hover:bg-[#171d21]'
                  }`}
                >
                  <div class="flex items-center justify-between gap-3">
                    <span class="truncate text-sm font-semibold">{agent.display_name}</span>
                    <span class="rounded-full border border-[#344149] bg-[#0f1418] px-2 py-0.5 text-[10px] text-[#8ed5ff]">
                      {activeCountFor(agent.name)}
                    </span>
                  </div>
                  <div class="mt-1 truncate text-[10px] text-[#75838d]">
                    {modeLabels[agent.mode] || agent.mode} · {agent.name}
                  </div>
                </button>
              )}
            </For>
          </div>
        </aside>

        <section class="skill-list-column flex min-h-0 flex-col border-r border-[#2a3136] bg-[#0f1418]">
          <div class="shrink-0 border-b border-[#2a3136] p-4">
            <div class="mb-3">
              <div class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#7bd0ff]">
                {currentAgent()?.display_name || selectedAgent()}
              </div>
              <div class="mt-1 text-lg font-black text-[#f7fafc]">可用 Skills</div>
            </div>
            <label class="flex items-center gap-2 rounded-xl border border-[#30383e] bg-[#11171b] px-3 py-2.5 focus-within:border-[#38bdf8]/50">
              <span class="text-[#64727c]" aria-hidden="true">⌕</span>
              <input
                value={query()}
                onInput={(event) => setQuery(event.currentTarget.value)}
                placeholder="搜索名称、描述或标签"
                class="min-w-0 flex-1 bg-transparent text-sm text-[#dee3e8] outline-none placeholder:text-[#59666f]"
              />
            </label>
            <div class="mt-3 grid grid-cols-3 gap-2 rounded-xl border border-[#2a3136] bg-[#11171b] p-1">
              <button type="button" class={`skill-view-tab ${view() === 'active' ? 'active' : ''}`} onClick={() => selectView('active')}>
                已加载 {availableCount() - disabledCount()}
              </button>
              <button type="button" class={`skill-view-tab ${view() === 'disabled' ? 'active' : ''}`} onClick={() => selectView('disabled')}>
                已关闭 {disabledCount()}
              </button>
              <button type="button" class={`skill-view-tab ${view() === 'all' ? 'active' : ''}`} onClick={() => selectView('all')}>
                全部 {availableCount()}
              </button>
            </div>
          </div>

          <div class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto p-3">
            <Show when={!loading()} fallback={<div class="p-8 text-center text-sm text-[#7f8b93]">正在加载技能目录...</div>}>
              <Show
                when={filteredSkills().length > 0}
                fallback={
                  <div class="m-2 rounded-2xl border border-dashed border-[#344149] bg-[#13191d] px-5 py-10 text-center">
                    <div class="text-sm font-semibold text-[#bdc8d1]">当前视图没有技能</div>
                    <div class="mt-2 text-xs leading-5 text-[#75838d]">可切换状态筛选，或为该 Agent 重新开启技能。</div>
                  </div>
                }
              >
                <div class="space-y-2">
                  <For each={filteredSkills()}>
                    {(skill) => {
                      const enabled = () => stateFor(skill).enabled;
                      return (
                        <button
                          type="button"
                          onClick={() => void openSkill(skill.name)}
                          class={`group w-full rounded-2xl border p-3 text-left transition ${
                            selectedSkill() === skill.name
                              ? 'border-[#38bdf8]/40 bg-[#17232a] shadow-[0_10px_30px_rgba(0,0,0,0.2)]'
                              : 'border-[#293137] bg-[#13191d] hover:border-[#3b4a53] hover:bg-[#171e22]'
                          }`}
                        >
                          <div class="flex items-start justify-between gap-3">
                            <div class="min-w-0">
                              <div class="truncate text-sm font-bold text-[#eaf0f4]">{skill.name}</div>
                              <div class="mt-1 line-clamp-2 text-xs leading-5 text-[#8f9da6]">{skill.description || '暂无描述'}</div>
                            </div>
                            <span
                              role="switch"
                              aria-checked={enabled()}
                              tabIndex={0}
                              onClick={(event) => {
                                event.stopPropagation();
                                void toggleSkill(skill, !enabled());
                              }}
                              onKeyDown={(event) => {
                                if (event.key !== 'Enter' && event.key !== ' ') return;
                                event.preventDefault();
                                event.stopPropagation();
                                void toggleSkill(skill, !enabled());
                              }}
                              class={`skill-toggle ${enabled() ? 'enabled' : ''} ${mutating() === skill.name ? 'opacity-50' : ''}`}
                              title={enabled() ? '关闭后从当前 Agent 隐藏' : '为当前 Agent 动态加载'}
                            >
                              <span />
                            </span>
                          </div>
                          <div class="mt-3 flex items-center gap-2 overflow-hidden text-[10px]">
                            <span class="shrink-0 rounded-full border border-[#344149] bg-[#0f1418] px-2 py-1 text-[#9cabb4]">
                              {sourceLabels[skill.source_kind] || skill.source_kind}
                            </span>
                            <span class="truncate text-[#65737c]">{skill.plugin || skill.category}</span>
                          </div>
                        </button>
                      );
                    }}
                  </For>
                </div>
              </Show>
            </Show>
          </div>
        </section>

        <article class="skill-detail-column min-h-0 overflow-y-auto bg-[#0c1115]">
          <Show
            when={selectedSkill()}
            fallback={
              <div class="flex h-full min-h-[340px] items-center justify-center p-8 text-center">
                <div>
                  <div class="text-4xl text-[#33414a]">✶</div>
                  <div class="mt-4 text-base font-semibold text-[#bdc8d1]">选择一个 Skill 查看详情</div>
                  <div class="mt-2 text-xs text-[#6f7d87]">SKILL.md 会在这里直接渲染。</div>
                </div>
              </div>
            }
          >
            <Show when={!detailLoading() && detail()} fallback={<div class="p-10 text-sm text-[#7f8b93]">正在读取 SKILL.md...</div>}>
              {(resolved) => {
                const item = () => resolved();
                const enabled = () => stateFor(item()).enabled;
                return (
                  <div class="mx-auto w-full max-w-[1180px] px-5 py-6 md:px-8 md:py-8">
                    <div class="border-b border-[#2a3136] pb-6">
                      <div class="flex flex-wrap items-start justify-between gap-5">
                        <div class="min-w-0 flex-1">
                          <div class="text-[11px] font-semibold uppercase tracking-[0.2em] text-[#7bd0ff]">Skill 详情</div>
                          <h1 class="skill-detail-title mt-3 break-words font-['Space_Grotesk'] text-3xl font-black tracking-tight text-[#f7fafc] md:text-4xl">
                            {item().name}
                          </h1>
                          <p class="mt-4 max-w-4xl text-sm leading-7 text-[#9faeb7]">{item().description || '暂无描述'}</p>
                          <div class="mt-4 flex flex-wrap gap-2 text-[11px]">
                            <span class="rounded-full border border-[#3a4850] bg-[#151b1f] px-3 py-1.5 text-[#bdc8d1]">{item().category}</span>
                            <span class="rounded-full border border-[#3a4850] bg-[#151b1f] px-3 py-1.5 text-[#bdc8d1]">{sourceLabels[item().source_kind] || item().source_kind}</span>
                            <Show when={item().plugin}>
                              <span class="rounded-full border border-[#594a35] bg-[#211b14] px-3 py-1.5 text-[#ffc176]">{item().plugin}</span>
                            </Show>
                            <For each={item().tags}>{(tag) => <span class="rounded-full border border-[#344149] px-3 py-1.5 text-[#8f9da6]">{tag}</span>}</For>
                          </div>
                        </div>
                        <div class="rounded-2xl border border-[#303a40] bg-[#13191d] p-4">
                          <div class="text-[10px] uppercase tracking-[0.16em] text-[#75838d]">当前 Agent</div>
                          <div class="mt-1 text-sm font-semibold text-[#e6edf3]">{currentAgent()?.display_name || selectedAgent()}</div>
                          <button
                            type="button"
                            disabled={mutating() === item().name}
                            onClick={() => void toggleSkill(item(), !enabled())}
                            class={`mt-4 flex min-w-[156px] items-center justify-between gap-4 rounded-xl border px-3 py-2.5 text-xs font-semibold transition ${
                              enabled()
                                ? 'border-[#5cae7b]/35 bg-[#1b2a21] text-[#9ee6bb]'
                                : 'border-[#3b454b] bg-[#171c20] text-[#9aa7af]'
                            }`}
                          >
                            <span>{enabled() ? '已动态加载' : '已关闭'}</span>
                            <span class={`skill-toggle ${enabled() ? 'enabled' : ''}`}><span /></span>
                          </button>
                        </div>
                      </div>
                    </div>

                    <div class="mt-6 inline-flex rounded-2xl border border-[#2a3136] bg-[#11171b] p-1.5">
                      <button type="button" class={`skill-detail-tab ${detailTab() === 'overview' ? 'active' : ''}`} onClick={() => setDetailTab('overview')}>
                        ◉ 概览
                      </button>
                      <button type="button" class={`skill-detail-tab ${detailTab() === 'files' ? 'active' : ''}`} onClick={() => setDetailTab('files')}>
                        ⌘ 文件 {item().files.length}
                      </button>
                    </div>

                    <Show when={detailTab() === 'overview'}>
                      <section class="mt-6 overflow-hidden rounded-[26px] border border-[#2a3136] bg-[#11171b] shadow-[0_24px_70px_rgba(0,0,0,0.25)]">
                        <div class="border-b border-[#2a3136] px-6 py-4">
                          <div class="text-sm font-bold text-[#e6edf3]">Primary markdown</div>
                          <div class="mt-1 text-xs text-[#7f8b93]">从 skill 包加载并安全渲染的主指南。</div>
                        </div>
                        <div class="skill-markdown px-6 py-7 md:px-8 md:py-9">
                          <MarkdownContent content={item().content} role="tool" renderKey={`skill:${item().name}`} />
                        </div>
                      </section>
                    </Show>

                    <Show when={detailTab() === 'files'}>
                      <section class="mt-6 overflow-hidden rounded-[26px] border border-[#2a3136] bg-[#11171b]">
                        <div class="border-b border-[#2a3136] px-6 py-4">
                          <div class="text-sm font-bold text-[#e6edf3]">Skill package</div>
                          <div class="mt-1 break-all font-mono text-[11px] text-[#6f7d87]">{item().location}</div>
                        </div>
                        <div class="divide-y divide-[#252d32]">
                          <For each={item().files}>
                            {(file) => (
                              <div class="flex items-center justify-between gap-4 px-6 py-4">
                                <div class="min-w-0">
                                  <div class="flex items-center gap-2">
                                    <span class="text-[#7bd0ff]">◇</span>
                                    <span class="truncate font-mono text-xs text-[#d7dfe4]">{file.relative_path}</span>
                                    <Show when={file.primary}><span class="rounded-full bg-[#38bdf8]/10 px-2 py-0.5 text-[9px] text-[#8ed5ff]">主文件</span></Show>
                                  </div>
                                </div>
                                <span class="shrink-0 text-[10px] text-[#65737c]">{formatFileSize(file.size)}</span>
                              </div>
                            )}
                          </For>
                        </div>
                      </section>
                    </Show>
                  </div>
                );
              }}
            </Show>
          </Show>
        </article>
      </main>
    </div>
  );
}
