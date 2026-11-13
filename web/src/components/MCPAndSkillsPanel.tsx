import { For, Show, createSignal, onMount } from 'solid-js';
import {
  getMCPServers,
  getMCPTools,
  getAgentMCPMapping,
  type AgentMCPMapping,
  type MCPServerStatus,
} from '../api/client';

interface MCPTool {
  name: string;
  description: string;
  server: string;
}

interface MCPAndSkillsPanelProps {
  width: number;
  embedded?: boolean;
  onWidthChange?: (width: number) => void;
  onHide?: () => void;
}

export default function MCPAndSkillsPanel(props: MCPAndSkillsPanelProps) {
  const [mcpServers, setMcpServers] = createSignal<MCPServerStatus[]>([]);
  const [mcpTools, setMcpTools] = createSignal<MCPTool[]>([]);
  const [mcpToolsMessage, setMcpToolsMessage] = createSignal('');
  const [agentMCPMap, setAgentMCPMap] = createSignal<Record<string, AgentMCPMapping>>({});
  const [agentMCPSource, setAgentMCPSource] = createSignal('');
  const [loading, setLoading] = createSignal(false);
  const [expandedServer, setExpandedServer] = createSignal<string | null>(null);
  const [expandedAgent, setExpandedAgent] = createSignal<string | null>(null);

  const loadData = async () => {
    setLoading(true);
    try {
      const [servers, agentMapData] = await Promise.all([
        getMCPServers({ refreshFailed: true }),
        getAgentMCPMapping(),
      ]);
      setMcpServers(servers);
      setAgentMCPMap(agentMapData.agents || {});
      setAgentMCPSource(agentMapData.source || '');
    } catch (e) {
      console.error('Failed to load data:', e);
    } finally {
      setLoading(false);
    }
  };


  const loadToolsForServer = async (serverName: string) => {
    try {
      return await getMCPTools(serverName);
    } catch (e) {
      console.error('Failed to load tools:', e);
      return {
        tools: [],
        direct_call_allowed: false,
        route_mode: '',
        submit_via: null,
        message: '加载 MCP 工具失败',
      };
    }
  };

  const toggleServer = async (serverName: string) => {
    const current = expandedServer();
    if (current === serverName) {
      setExpandedServer(null);
      return;
    }

    setExpandedServer(serverName);
    setMcpTools([]);
    setMcpToolsMessage('');

    const server = mcpServers().find((item) => item.name === serverName);
    if (server?.route_mode === 'job_protocol') {
      setMcpToolsMessage(
        server.route_reason ||
          `该 MCP 只能通过 ${server.submit_via || 'job-protocol-mcp'} 提交任务，前端不提供直接工具列表。`,
      );
      return;
    }

    const result = await loadToolsForServer(serverName);
    setMcpTools(result.tools.map((tool) => ({
      name: tool.name || tool.tool || '',
      description: tool.description || '',
      server: tool.server || serverName,
    })));
    setMcpToolsMessage(result.message || (result.tools.length > 0 ? '' : '当前没有可展示的工具'));
  };

  const toggleAgent = (agentName: string) => {
    setExpandedAgent((prev) => (prev === agentName ? null : agentName));
  };

  const directServers = () => mcpServers().filter((server) => server.route_mode !== 'job_protocol');
  const routedServers = () => mcpServers().filter((server) => server.route_mode === 'job_protocol');

  onMount(() => {
    loadData();
  });

  return (
    <div class={`${props.embedded ? 'relative h-full w-full' : 'fixed bottom-0 right-0 top-14'} flex flex-col overflow-hidden border-l border-[#2a3136] bg-[#11171b]/96`} style={{ width: props.embedded ? '100%' : `${props.width}px` }}>
      <div class="border-b border-[#2a3136] px-4 py-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#7bd0ff]">检查器</div>
            <div class="mt-1 text-sm font-semibold text-[#f5f7fa]">MCP 连接状态</div>
          </div>
          <Show when={props.onHide}><button
            onClick={() => props.onHide?.()}
            class="inline-flex h-9 w-9 items-center justify-center rounded-full border border-[#303539] bg-[#171c20] text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
            title="隐藏面板"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
              <path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
            </svg>
          </button></Show>
        </div>
      </div>

      <div class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <Show when={loading()}>
          <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] py-6 text-center text-[#8b949e]">加载中...</div>
        </Show>

        <Show when={!loading()}>
          <div class="space-y-4">


            <Show
              when={mcpServers().length > 0}
              fallback={
                <div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] py-6 text-center text-sm text-[#8b949e]">
                  未配置 MCP 服务器
                </div>
              }
            >
              <div class="space-y-4">
                <Show when={directServers().length > 0}>
                  <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
                    <div class="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#7bd0ff]">可直接调用</div>
                    <div class="space-y-2">
                      <For each={directServers()}>
                        {(server) => (
                          <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0f1418]">
                            <button
                              class="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-[#171f24]"
                              onClick={() => toggleServer(server.name)}
                            >
                              <div class="min-w-0 flex items-center gap-3">
                                <span class={`h-2.5 w-2.5 rounded-full ${server.connected ? 'bg-[#78d49c]' : 'bg-[#ff9c96]'}`} />
                                <div class="min-w-0">
                                  <div class="truncate text-sm font-semibold text-[#e6edf3]">{server.name}</div>
                                  <div class="mt-1 text-[11px] text-[#8b949e]">{server.callable_tools || server.tools} 个工具</div>
                                </div>
                              </div>
                              <div class="flex items-center gap-2">
                                <span class="rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/10 px-2 py-1 text-[10px] text-[#8ed5ff]">
                                  {server.route_label || '允许直连'}
                                </span>
                                <svg
                                  xmlns="http://www.w3.org/2000/svg"
                                  class={`h-4 w-4 text-[#8b949e] transition-transform ${expandedServer() === server.name ? 'rotate-90' : ''}`}
                                  fill="none"
                                  viewBox="0 0 24 24"
                                  stroke="currentColor"
                                >
                                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                                </svg>
                              </div>
                            </button>
                            <Show when={expandedServer() === server.name}>
                              <div class="border-t border-[#2a3136] px-4 py-3">
                                <Show when={Boolean(server.route_reason)}>
                                  <div class="mb-3 text-xs text-[#9faeb7]">{server.route_reason}</div>
                                </Show>
                                <Show
                                  when={mcpTools().length > 0}
                                  fallback={<div class="text-xs text-[#8b949e] py-1">{mcpToolsMessage() || '正在加载工具...'}</div>}
                                >
                                  <div class="space-y-2">
                                    <For each={mcpTools()}>
                                      {(tool) => (
                                        <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-3">
                                          <div class="text-sm font-semibold text-[#8ed5ff]">{tool.name}</div>
                                          <div class="mt-1 text-xs leading-6 text-[#9faeb7]">{tool.description || '暂无描述'}</div>
                                        </div>
                                      )}
                                    </For>
                                  </div>
                                </Show>
                              </div>
                            </Show>
                          </div>
                        )}
                      </For>
                    </div>
                  </div>
                </Show>

                <Show when={routedServers().length > 0}>
                  <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
                    <div class="mb-3 flex items-center justify-between text-[11px] font-semibold uppercase tracking-[0.18em] text-[#ffc176]"><span>异步 MCP</span><span>{routedServers().filter(server => server.connected).length}/{routedServers().length} 已连接</span></div>
                    <div class="space-y-2">
                      <For each={routedServers()}>
                        {(server) => (
                          <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0f1418]">
                            <button
                              class="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-[#171f24]"
                              onClick={() => toggleServer(server.name)}
                            >
                              <div class="min-w-0 flex items-center gap-3">
                                <span class={`h-2.5 w-2.5 rounded-full ${server.connected ? 'bg-[#78d49c]' : 'bg-[#ff9c96]'}`} />
                                <div class="min-w-0">
                                  <div class="truncate text-sm font-semibold text-[#e6edf3]">{server.name}</div>
                                  <div class="mt-1 text-[11px] text-[#8b949e]">{server.connected ? 'MCP 握手已通过' : server.error || 'MCP 未连接'}</div>
                                </div>
                              </div>
                              <div class="flex items-center gap-2">
                                <span class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/10 px-2 py-1 text-[10px] text-[#ffc176]">
                                  {server.route_label || '经 job-protocol 提交'}
                                </span>
                                <svg
                                  xmlns="http://www.w3.org/2000/svg"
                                  class={`h-4 w-4 text-[#8b949e] transition-transform ${expandedServer() === server.name ? 'rotate-90' : ''}`}
                                  fill="none"
                                  viewBox="0 0 24 24"
                                  stroke="currentColor"
                                >
                                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                                </svg>
                              </div>
                            </button>
                            <Show when={expandedServer() === server.name}>
                              <div class="border-t border-[#2a3136] px-4 py-3">
                                <div class="rounded-2xl border border-[#f1a02b]/22 bg-[#f1a02b]/8 px-3 py-3 text-xs leading-6 text-[#ffe1b3]">
                                  {server.route_reason || `该 MCP 只能通过 ${server.submit_via || 'job-protocol-mcp'} 提交。`}
                                </div>
                                <div class="mt-3 text-xs text-[#9faeb7]">前端不会暴露直接调用入口。</div>
                              </div>
                            </Show>
                          </div>
                        )}
                      </For>
                    </div>
                  </div>
                </Show>
              </div>
            </Show>
            <Show when={Object.keys(agentMCPMap()).length > 0}>
              <details class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4"><summary class="cursor-pointer text-sm font-semibold">高级：代理 MCP 映射</summary>
                <div class="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#ffc176]">
                  代理 MCP 映射
                  <Show when={agentMCPSource()}>
                    <span class="ml-2 normal-case tracking-normal text-[#7f8b93]">来源 {agentMCPSource()}</span>
                  </Show>
                </div>
                <div class="space-y-2">
                  <For each={Object.entries(agentMCPMap())}>
                    {([agentName, cfg]) => (
                      <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0f1418]">
                        <button
                          class="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-[#171f24]"
                          onClick={() => toggleAgent(agentName)}
                        >
                          <span class="text-sm font-semibold text-[#e6edf3]">{agentName}</span>
                          <svg
                            xmlns="http://www.w3.org/2000/svg"
                            class={`h-4 w-4 text-[#8b949e] transition-transform ${expandedAgent() === agentName ? 'rotate-90' : ''}`}
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                          >
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                          </svg>
                        </button>
                        <Show when={expandedAgent() === agentName}>
                          <div class="border-t border-[#2a3136] px-4 py-3">
                            <div class="flex flex-wrap gap-2">
                              <For each={cfg.allowed_tools === 'all' ? ['all'] : cfg.allowed_tools}>
                                {(toolName) => (
                                  <span class="rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/10 px-3 py-1 text-[11px] text-[#8ed5ff]">
                                    {toolName}
                                  </span>
                                )}
                              </For>
                            </div>
                            <Show when={cfg.description}>
                              <div class="mt-3 text-xs leading-6 text-[#9faeb7]">{cfg.description}</div>
                            </Show>
                          </div>
                        </Show>
                      </div>
                    )}
                  </For>
                </div>
              </details>
            </Show>
          </div>
        </Show>

      </div>

      <Show when={!props.embedded}><div
        class="absolute bottom-0 left-0 top-0 z-50 w-1 cursor-col-resize transition-colors hover:bg-[#38bdf8]"
        onMouseDown={(e) => {
          e.preventDefault();
          const handleMouseMove = (moveEvent: MouseEvent) => {
            const newWidth = Math.max(250, Math.min(600, window.innerWidth - moveEvent.clientX));
            props.onWidthChange?.(newWidth);
          };
          const handleMouseUp = () => {
            document.removeEventListener('mousemove', handleMouseMove);
            document.removeEventListener('mouseup', handleMouseUp);
          };
          document.addEventListener('mousemove', handleMouseMove);
          document.addEventListener('mouseup', handleMouseUp);
        }}
        title="拖拽调整宽度"
      /></Show>

    </div>
  );
}
