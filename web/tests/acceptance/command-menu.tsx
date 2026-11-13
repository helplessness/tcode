import { render } from 'solid-js/web';
import { createSignal, For, Show } from 'solid-js';
import SlashCommandMenu from '../../src/components/composer/SlashCommandMenu';
import { builtinCommands, filterCommands, type CommandItem } from '../../src/components/composer/slashCommands';
import '../../src/index.css';
import '../../src/paper-theme.css';
import '../../src/interface.css';

// Match the real light shell instead of testing only the legacy dark palette.
document.documentElement.classList.add('studio-theme');
document.documentElement.dataset.desktop = 'true';

// Uses the production menu and styles; no model calls or real session writes.
const skills: CommandItem[] = [
  {id: 'boltz', kind: 'skill', label: '$boltz-runner', description: '预测蛋白质结构，准备输入并提交异步计算任务。'},
  {id: 'gnina', kind: 'skill', label: '$gnina-runner', description: '执行分子对接，分析结合位点与亲和力评分。'},
  {id: 'long', kind: 'skill', label: '$multi-model-sequence-mutation-scoring', description: '多个模型联合评估序列突变；较长名称与说明应该完整显示。'},
  {id: 'disabled', kind: 'skill', label: '$simulation-runner', description: '模拟计算', disabled: '当前 Harness 不支持此 Skill，请切换原生 TCode。'},
];
function Demo() {
  const [view, setView] = createSignal('命令与 Skills');
  const [query, setQuery] = createSignal('');
  const [index, setIndex] = createSignal(0);
  const [open, setOpen] = createSignal(true);
  const [selected, setSelected] = createSignal('');
  const items = () => filterCommands(view() === '选择 Skill' ? skills : [...builtinCommands, ...skills], query());
  const select = (item: CommandItem) => {
    if (item.disabled) return;
    if (item.id === 'skills') { setView('选择 Skill'); setQuery(''); setIndex(0); }
    else { setSelected(item.label); setOpen(false); }
  };
  const keys = (event: KeyboardEvent) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); setIndex(i => (i + (event.key === 'ArrowDown' ? 1 : -1) + items().length) % items().length);
    } else if (event.key === 'Enter' && items()[index()]) { event.preventDefault(); select(items()[index()]); }
    else if (event.key === 'Escape') setOpen(false);
  };
  return <main style={{display: 'flex', 'flex-direction': 'column', 'min-height': '100dvh', padding: '16px'}}>
    <div style={{flex: 1}}>命令菜单视觉验收</div>
    <div style={{width: '100%', 'max-width': '900px', margin: '0 auto'}}>
      <div style={{display: 'flex', gap: '16px', 'margin-bottom': '16px'}}><For each={['命令与 Skills', '选择 Skill']}>{name => <button onClick={() => {setView(name); setQuery(''); setIndex(0); setOpen(true);}}>{name}</button>}</For></div>
      <Show when={selected()}><span class="composer-skill-chip" data-testid="selected-skill"><span>{selected()}</span><button aria-label="移除技能" onClick={() => setSelected('')}>×</button></span></Show>
      <div style={{position: 'relative', 'margin-top': '12px'}}>
        <Show when={open()}><SlashCommandMenu title={view()} query={query()} secondary={view() === '选择 Skill'} items={items()} index={index()} loading={false} error=""
          onQuery={value => {setQuery(value); setIndex(0);}} onHighlight={setIndex} onSelect={select} onBack={() => setView('命令与 Skills')}
          onKeyDown={keys} onManageSkills={view() === '选择 Skill' ? () => setSelected('管理技能') : undefined} /></Show>
        <textarea aria-label="指令输入" onKeyDown={keys} style={{width: '100%', height: '72px', padding: '12px', border: '1px solid var(--tcode-border)', 'border-radius': '12px', background: 'var(--tcode-surface)'}} placeholder="输入 / 选择命令，$ 选择 Skill" />
      </div>
    </div>
  </main>;
}
render(() => <Demo />, document.getElementById('root')!);
