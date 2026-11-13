import { Show, createResource, createSignal, onCleanup } from 'solid-js';
import {
  AppBridge,
  PostMessageTransport,
  buildAllowAttribute,
} from '@modelcontextprotocol/ext-apps/app-bridge';
import { sanitizeMessageContentBlocks, type McpAppContentBlock } from '../utils/contentBlocks';
import { sessionStore } from '../stores/session';

type McpAppHostProps = { block: McpAppContentBlock };

type ResourceCsp = {
  connectDomains?: string[];
  resourceDomains?: string[];
  frameDomains?: string[];
  baseUriDomains?: string[];
};

type ResourcePermissions = {
  clipboardWrite?: Record<string, never>;
};

type ResourceMeta = {
  csp?: ResourceCsp;
  permissions?: ResourcePermissions;
  prefersBorder?: boolean;
};

type AppResolution = {
  resource_token: string;
  selections?: Array<Record<string, unknown>>;
  tools?: Array<Record<string, unknown>>;
  resources?: Array<Record<string, unknown>>;
};

type HostedResource = AppResolution & {
  document: string;
  resourceMeta: ResourceMeta;
};

export const HOST_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; media-src data: blob:; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";

export function redactToolArguments(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactToolArguments);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => [
    key,
    /(?:token|secret|password|authorization|api[_-]?key)/i.test(key)
      ? '[已隐藏]'
      : redactToolArguments(item),
  ]));
}

const safeSources = (value: unknown, allowWebSocket = false): string[] => {
  if (!Array.isArray(value)) return [];
  const pattern = allowWebSocket
    ? /^(?:https|wss):\/\/(?:\*\.)?[A-Za-z0-9.-]+(?::\d+)?\/?$/
    : /^https:\/\/(?:\*\.)?[A-Za-z0-9.-]+(?::\d+)?\/?$/;
  return [...new Set(value.map(String).filter((item) => pattern.test(item)))].slice(0, 32);
};

export function buildHostCsp(meta: ResourceMeta = {}): string {
  const connect = safeSources(meta.csp?.connectDomains, true);
  const resources = safeSources(meta.csp?.resourceDomains);
  const frames = safeSources(meta.csp?.frameDomains);
  const bases = safeSources(meta.csp?.baseUriDomains);
  const sourceList = (base: string[], extra: string[]) => [...base, ...extra].join(' ');
  return [
    "default-src 'none'",
    `script-src ${sourceList(["'unsafe-inline'"], resources)}`,
    `style-src ${sourceList(["'unsafe-inline'"], resources)}`,
    `img-src ${sourceList(['data:', 'blob:'], resources)}`,
    `font-src ${sourceList(['data:'], resources)}`,
    `connect-src ${connect.length ? connect.join(' ') : "'none'"}`,
    `media-src ${sourceList(['data:', 'blob:'], resources)}`,
    `frame-src ${frames.length ? frames.join(' ') : "'none'"}`,
    "object-src 'none'",
    `base-uri ${bases.length ? bases.join(' ') : "'none'"}`,
    "form-action 'none'",
  ].join('; ');
}

export function isolatedDocument(source: string, resourceMeta: ResourceMeta = {}): string {
  const policy = `<meta http-equiv="Content-Security-Policy" content="${buildHostCsp(resourceMeta)}">`;
  if (/<head(?:\s[^>]*)?>/i.test(source)) {
    return source.replace(/<head(?:\s[^>]*)?>/i, (head) => `${head}${policy}`);
  }
  return `<!doctype html><html><head>${policy}</head><body>${source}</body></html>`;
}

const resolveApp = async (block: McpAppContentBlock): Promise<AppResolution> => {
  const response = await fetch('/api/mcp-apps/v1/resolve', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({
      app_id: block.app_id,
      session_id: block.session_id,
      server_name: block.server_name,
      tool_name: block.tool_name,
      resource_uri: block.resource_uri,
    }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = typeof payload.detail === 'string' ? `：${payload.detail}` : '';
    throw new Error(`MCP App 无法恢复访问权限 (${response.status})${detail}`);
  }
  return await response.json() as AppResolution;
};

export default function McpAppHost(props: McpAppHostProps) {
  let bridge: AppBridge | undefined;
  let transport: PostMessageTransport | undefined;
  let connectedFrame: HTMLIFrameElement | undefined;
  let connectedToken = '';
  const [height, setHeight] = createSignal(360);
  const [bridgeError, setBridgeError] = createSignal('');
  const [selectionNotice, setSelectionNotice] = createSignal('');
  type ToolResult = {
    content: Array<Record<string, unknown>>;
    isError?: boolean;
    [key: string]: unknown;
  };
  type PendingToolCall = {
    name: string;
    arguments: Record<string, unknown>;
    resolve: (result: ToolResult) => void;
    reject: (reason: unknown) => void;
  };
  const [pendingToolCall, setPendingToolCall] = createSignal<PendingToolCall | null>(null);
  const [resource] = createResource(
    () => `${props.block.app_id}:${props.block.session_id}:${props.block.resource_uri}`,
    async (): Promise<HostedResource> => {
      const resolution = await resolveApp(props.block);
      const response = await fetch(
        `/api/mcp-apps/v1/resources/${encodeURIComponent(resolution.resource_token)}`,
        { credentials: 'same-origin', headers: { Accept: 'application/json' } },
      );
      if (!response.ok) throw new Error(`MCP App resource unavailable (${response.status})`);
      const payload = await response.json() as {
        contents?: Array<{ text?: string }>;
        resourceMeta?: ResourceMeta;
        resources?: Array<Record<string, unknown>>;
      };
      const text = payload.contents?.find((item) => typeof item.text === 'string')?.text;
      if (!text) throw new Error('MCP App resource did not contain HTML');
      const resourceMeta = payload.resourceMeta || {};
      return {
        ...resolution,
        resources: payload.resources || resolution.resources || [],
        resourceMeta,
        document: isolatedDocument(text, resourceMeta),
      };
    },
  );

  // A rejected Solid resource throws when read. Check error first so a stale
  // historical App shows its fallback instead of interrupting chat rendering.
  const availableResource = () => resource.error ? undefined : resource();

  const connectBridge = async (frame: HTMLIFrameElement) => {
    const hosted = availableResource();
    if (!frame.contentWindow || !hosted) return;
    if (connectedFrame === frame && connectedToken === hosted.resource_token) return;
    pendingToolCall()?.reject(new Error('MCP App 权限已刷新'));
    setPendingToolCall(null);
    if (bridge) await bridge.teardownResource({}).catch(() => undefined);
    if (transport) await transport.close().catch(() => undefined);
    connectedFrame = frame;
    connectedToken = hosted.resource_token;
    try {
      bridge = new AppBridge(
        null,
        { name: 'TCode MCP Apps Host', version: '0.2.0' },
        {
          serverTools: {},
          serverResources: {},
          sandbox: {
            csp: hosted.resourceMeta.csp,
            permissions: hosted.resourceMeta.permissions,
          },
        },
        {
          hostContext: {
            theme: 'light',
            displayMode: 'inline',
            availableDisplayModes: ['inline'],
            locale: document.documentElement.lang || navigator.language || 'zh-CN',
            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            userAgent: 'TCode Web',
            platform: 'web',
            containerDimensions: {
              maxHeight: 1200,
              maxWidth: frame.clientWidth || 960,
            },
          },
        },
      );
      bridge.onsizechange = ({ height: requested }) => {
        if (Number.isFinite(requested)) {
          setHeight(Math.max(160, Math.min(Number(requested), 1200)));
        }
      };
      bridge.onlistresources = async () => ({ resources: hosted.resources || [] }) as any;
      bridge.onreadresource = async (params) => {
        const response = await fetch(
          `/api/mcp-apps/v1/resources/${encodeURIComponent(hosted.resource_token)}/read`,
          {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ uri: params.uri }),
          },
        );
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(String(result.detail || `MCP App 资源读取失败 (${response.status})`));
        }
        return result as any;
      };
      bridge.oncalltool = async (params) => {
        if (pendingToolCall()) {
          throw new Error('已有 MCP App 工具调用等待确认');
        }
        return await new Promise<ToolResult>((resolve, reject) => {
          setPendingToolCall({
            name: String(params.name || ''),
            arguments: params.arguments && typeof params.arguments === 'object'
              ? params.arguments as Record<string, unknown>
              : {},
            resolve,
            reject,
          });
        }) as any;
      };
      bridge.oninitialized = () => {
        void bridge?.sendToolInput({ arguments: props.block.tool_input });
        void bridge?.sendToolResult(
          {content: [], ...props.block.tool_result, tcode_selections: hosted.selections || []} as Parameters<AppBridge['sendToolResult']>[0],
        );
      };
      transport = new PostMessageTransport(frame.contentWindow, frame.contentWindow);
      await bridge.connect(transport);
    } catch (error) {
      setBridgeError(error instanceof Error ? error.message : 'MCP App bridge failed');
    }
  };

  onCleanup(() => {
    pendingToolCall()?.reject(new Error('MCP App 已关闭'));
    if (bridge) void bridge.teardownResource({}).catch(() => undefined);
    if (transport) void transport.close().catch(() => undefined);
  });

  const rejectToolCall = () => {
    const pending = pendingToolCall();
    if (!pending) return;
    setPendingToolCall(null);
    pending.resolve({
      content: [{ type: 'text', text: '用户拒绝了 MCP App 工具调用' }],
      isError: true,
    });
  };

  const allowToolCall = async () => {
    const pending = pendingToolCall();
    const hosted = availableResource();
    if (!pending || !hosted) return;
    setPendingToolCall(null);
    try {
      const response = await fetch(
        `/api/mcp-apps/v1/tools/${encodeURIComponent(hosted.resource_token)}/call`,
        {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ name: pending.name, arguments: pending.arguments }),
        },
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(String(result.detail || `MCP App 工具调用失败 (${response.status})`));
      }
      if (result.tcode_selection) {
        const selection = result.tcode_selection;
        setSelectionNotice(selection.fallback || '选择已写入当前会话');
        const messages = sessionStore.currentSessionId() === props.block.session_id
          ? sessionStore.messages()
          : sessionStore.sessions().find(s => s.session_id === props.block.session_id)?.messages || [];
        if (typeof selection.event_id === 'string' && !messages.some(m => m.id === selection.event_id)) {
          sessionStore.addMessageForSession(props.block.session_id, {
            id: selection.event_id, role: 'system', content: '',
            content_blocks: sanitizeMessageContentBlocks([selection]),
            metadata: {event_type: 'result_selection', platform_event: true},
          });
        }
      }
      pending.resolve(result as ToolResult);
    } catch (error) {
      pending.reject(error);
    }
  };

  return (
    <div
      class={`overflow-hidden rounded-2xl bg-[#0f1418] ${
        availableResource()?.resourceMeta.prefersBorder === false
          ? 'border border-transparent'
          : 'border border-[#2a3136]'
      }`}
    >
      <div class="border-b border-[#2a3136] px-4 py-2 text-xs text-[#9faeb7]">
        交互式分析界面
      </div>
      <Show when={selectionNotice()}><div role="status" class="px-4 py-2 text-sm text-green-300">{selectionNotice()}</div></Show>
      <Show when={pendingToolCall()}>
        {(pending) => (
          <div class="border-b border-[#f1a02b]/30 bg-[#211a0f] px-4 py-3 text-xs text-[#ffe1b3]">
            <div class="font-semibold">交互界面请求调用：{pending().name}</div>
            <pre class="mt-2 max-h-28 overflow-auto whitespace-pre-wrap text-[11px] text-[#c9b997]">
              {JSON.stringify(redactToolArguments(pending().arguments), null, 2)}
            </pre>
            <div class="mt-3 flex gap-2">
              <button
                type="button"
                class="rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-4 py-2 font-semibold text-[#9ee6bb]"
                onClick={() => void allowToolCall()}
              >
                允许一次
              </button>
              <button
                type="button"
                class="rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 px-4 py-2 font-semibold text-[#ffcbc7]"
                onClick={rejectToolCall}
              >
                拒绝
              </button>
            </div>
          </div>
        )}
      </Show>
      <Show
        when={availableResource()}
        fallback={(
          <div class="px-4 py-4 text-sm text-[#ffb6b0]">
            {resource.error
              ? `${props.block.fallback}（${resource.error.message}）`
              : '正在加载交互界面…'}
          </div>
        )}
      >
        {(hosted) => (
          <iframe
            title="交互式分析界面"
            sandbox="allow-scripts"
            allow={buildAllowAttribute(hosted().resourceMeta.permissions as any) || undefined}
            referrerPolicy="no-referrer"
            srcdoc={hosted().document}
            onLoad={(event) => { void connectBridge(event.currentTarget); }}
            style={{ width: '100%', height: `${height()}px`, border: '0' }}
          />
        )}
      </Show>
      <Show when={bridgeError()}>
        <div class="border-t border-[#2a3136] px-4 py-2 text-xs text-[#ffb6b0]">
          {props.block.fallback}（{bridgeError()}）
        </div>
      </Show>
    </div>
  );
}
