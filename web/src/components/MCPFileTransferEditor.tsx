import { For, Show, createSignal } from 'solid-js';
import { discoverMCPFiles, type MCPFileDiscovery, type MCPFileTransfer } from '../api/capabilities';

export default function MCPFileTransferEditor(props: {
  value: MCPFileTransfer | null;
  onChange: (value: MCPFileTransfer | null) => void;
  connection: () => {name: string; url: string; transport: string; headers: Record<string, string>};
  disabled: boolean;
}) {
  const [tools, setTools] = createSignal<MCPFileDiscovery>([]);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  const discover = async () => {
    setBusy(true); setError('');
    try { setTools((await discoverMCPFiles(props.connection())).tools); }
    catch (err) { setError(err instanceof Error ? err.message : '工具发现失败'); }
    finally { setBusy(false); }
  };
  const changeTool = (name: string, checked: boolean) => {
    if (!props.value) return;
    props.onChange({...props.value, tools: checked ? [...props.value.tools, {name, files: []}] : props.value.tools.filter(t => t.name !== name)});
  };
  const changeField = (name: string, pointer: string, kind: string) => {
    if (!props.value) return;
    props.onChange({...props.value, tools: props.value.tools.map(t => t.name !== name ? t : {...t,
      files: [...t.files.filter(f => f.pointer !== pointer), ...(kind ? [{pointer, kind: kind as 'file' | 'directory', transport: 'artifact' as const}] : [])]})});
  };
  return <fieldset class="mcp-file-transfer" disabled={props.disabled || busy()}>
    <legend>输入文件传输（可选）</legend>
    <label><input type="checkbox" checked={Boolean(props.value)} onChange={e => props.onChange(e.currentTarget.checked ? {receiver: 'artifact_refs', tools: []} : null)}/> 为自定义异步 MCP 配置文件字段</label>
    <p class="settings-muted">不配置时保持原有行为。文件声明不会授予读取或计算权限；返回普通 JSON 的工具也可接入。</p>
    <Show when={props.value}>
      <label>远端接收方式<select value={props.value!.receiver} onChange={e => props.onChange({...props.value!, receiver: e.currentTarget.value as MCPFileTransfer['receiver']})}>
        <option value="artifact_refs">已支持产物引用（ArtifactRef / BundleRef）</option>
        <option value="server_paths">仅支持服务器文件路径（需要适配器）</option>
      </select></label>
      <Show when={props.value!.receiver === 'server_paths'}><p class="settings-callout" role="status">文件传输尚未接通。可以保存配置，但不会上传或提交；请先自行部署服务器适配器，客户端不会自动修改服务。</p></Show>
      <button type="button" onClick={() => void discover()}>{busy() ? '正在读取工具定义…' : '发现工具与参数（不提交计算）'}</button>
      <Show when={!tools().length && props.value!.tools.length}><p class="settings-muted">已保存 {props.value!.tools.length} 个工具的声明；点击发现可修改参数，保存时会再次核对远端 Schema。</p></Show>
      <For each={tools()}>{tool => {
        const selection = () => props.value?.tools.find(t => t.name === tool.name);
        return <details class="mcp-file-tool"><summary>{tool.name} · {selection()?.files.length || 0} 个文件字段</summary>
          <label><input type="checkbox" checked={Boolean(selection())} onChange={e => changeTool(tool.name, e.currentTarget.checked)}/> 启用此工具的异步接入</label>
          <Show when={selection()}><For each={tool.fields}>{field => <label class="mcp-file-field"><span>{field.label}</span><select aria-label={`${tool.name} ${field.label} 的文件类型`} value={selection()?.files.find(f => f.pointer === field.pointer)?.kind || ''} onChange={e => changeField(tool.name, field.pointer, e.currentTarget.value)}>
            <option value="">普通参数（不传输）</option><option value="file">文件 / 文件列表</option><option value="directory">目录 / 目录列表</option>
          </select></label>}</For></Show>
        </details>;
      }}</For>
      <p class="settings-muted">支持命名嵌套字段和文件列表；对象列表中的通配字段暂不支持。连接检查不代表已验证远端能下载文件。仅同步返回结果的服务无法在网络中断后确认远端状态，不会自动重提。</p>
    </Show>
    <Show when={error()}><p role="alert">{error()}</p></Show>
  </fieldset>;
}
