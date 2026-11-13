import { Index, Show, createSignal } from 'solid-js';
import type { DynamicHeaders } from '../api/capabilities';
import './DynamicHeadersEditor.css';

export default function DynamicHeadersEditor(props: { value: DynamicHeaders; onChange: (value: DynamicHeaders) => void; disabled?: boolean }) {
  const [error, setError] = createSignal('');
  const rows = () => Object.entries(props.value);
  function update(index: number, patch: Record<string, unknown>, name?: string) {
    const entries = rows().map(([key, rule], i) => i === index ? [name ?? key, { ...rule, ...patch }] as const : [key, rule] as const);
    if (new Set(entries.map(([key]) => key.toLowerCase())).size !== entries.length) {
      setError('Header 名称不能重复'); return;
    }
    setError(''); props.onChange(Object.fromEntries(entries));
  }
  function add() {
    let count = rows().length + 1;
    while (rows().some(([name]) => name.toLowerCase() === `x-business-${count}`)) count++;
    props.onChange({ ...props.value, [`X-Business-${count}`]: { variable: `business_${count}`, description: '', extraction_hint: '', required: true, max_length: 128 } });
  }
  return <details class="dynamic-headers-editor"><summary>本次调用变量 Header（{rows().length}）</summary>
    <p class="settings-muted">Agent 根据业务说明从用户需求提取变量，只用于本次调用或异步任务，不修改全局值。身份、Token 和 uid 不可由自然语言设置。</p>
    <Index each={rows()}>{(row, index) => <fieldset disabled={props.disabled}>
      <legend>动态请求头 {index + 1}</legend>
      <label>Header 名称<input value={row()[0]} onInput={e => update(index, {}, e.currentTarget.value)} /></label>
      <label>业务变量名<input value={row()[1].variable} onInput={e => update(index, { variable: e.currentTarget.value })} /></label>
      <label>业务含义（必填，提供给 Agent）<textarea value={row()[1].description} onInput={e => update(index, { description: e.currentTarget.value })} placeholder="例如：本次操作所属的项目编号，用于选择项目数据" maxLength={1000} /></label>
      <label>提取说明<input value={row()[1].extraction_hint} onInput={e => update(index, { extraction_hint: e.currentTarget.value })} placeholder="例如：从用户明确指定的项目中提取，缺失或歧义时先询问" maxLength={1000} /></label>
      <label class="dynamic-header-required"><input type="checkbox" checked={row()[1].required} onChange={e => update(index, { required: e.currentTarget.checked })} />必填，缺失时阻止调用并询问用户</label>
      <details><summary>高级：值长度限制</summary><label>最大字符数<input type="number" min={1} max={4096} value={row()[1].max_length} onInput={e => update(index, { max_length: Number(e.currentTarget.value) })} /></label></details>
      <button type="button" onClick={() => props.onChange(Object.fromEntries(rows().filter((_, i) => i !== index)))}>移除此动态规则</button>
    </fieldset>}</Index>
    <Show when={error()}><p role="alert">{error()}</p></Show>
    <button type="button" disabled={props.disabled || rows().length >= 32} onClick={add}>添加动态请求头</button>
  </details>;
}
