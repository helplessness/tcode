import { For, Show } from 'solid-js';
import type { SubagentItem } from '../api/subagents';
import { buildSubagentTree, normalizeSubagentStatus, summarizeSubagentTree, type SubagentTreeNode } from '../utils/subagentTree';

interface SubagentDelegationTreeProps {
  items: SubagentItem[];
  rootLabel: string;
  statusOf: (item: SubagentItem) => string;
  activityOf: (item: SubagentItem) => string;
  onSelect: (item: SubagentItem) => void;
  onDiscuss: (item: SubagentItem) => void;
}

export default function SubagentDelegationTree(props: SubagentDelegationTreeProps) {
  const tree = () => buildSubagentTree(props.items);
  const stats = () => summarizeSubagentTree(props.items, props.statusOf);

  const Branch = (branchProps: { node: SubagentTreeNode }) => {
    const item = () => branchProps.node.item;
    const status = () => normalizeSubagentStatus(props.statusOf(item()));
    const displayName = () => item().display_name || item().title || item().agent_name;
    return <li class="subagent-tree-branch">
      <article class={`subagent-tree-node subagent-tree-node--${status()}`}>
        <button class="subagent-tree-node-main" type="button" onClick={() => props.onSelect(item())} title="打开该子代理详情">
          <span class={`subagent-pixel-avatar subagent-pixel-avatar--${status()}`} aria-hidden="true"><span /></span>
          <span class="min-w-0 flex-1">
            <span class="block truncate text-xs font-semibold text-[#f5f7fa]">{displayName()}</span>
            <span class="mt-0.5 block truncate text-[10px] text-[#8b949e]">{props.activityOf(item())}</span>
          </span>
          <span class="subagent-tree-status-dot" aria-label={status()} />
        </button>
        <button class="subagent-tree-discuss" type="button" onClick={() => props.onDiscuss(item())}>讨论</button>
      </article>
      <Show when={branchProps.node.children.length > 0}>
        <ul class="subagent-tree-children">
          <For each={branchProps.node.children}>{child => <Branch node={child} />}</For>
        </ul>
      </Show>
    </li>;
  };

  return <section class="subagent-tree-panel" aria-label="子代理委托树">
    <div class="subagent-tree-stats">
      <span><b>{props.items.length}</b> 子代理</span>
      <span class="text-[#ffe1b3]"><b>{stats().running}</b> 运行中</span>
      <span><b>{stats().idle}</b> 待处理</span>
      <span class="text-[#9ee6bb]"><b>{stats().completed}</b> 完成</span>
      <span class="text-[#ffd0cb]"><b>{stats().failed}</b> 异常</span>
    </div>
    <div class="tcode-scrollbar overflow-auto rounded-2xl border border-[#2a3136] bg-[#0d1216] p-5">
      <div class="subagent-tree-root">
        <span class="subagent-pixel-avatar subagent-pixel-avatar--root" aria-hidden="true"><span /></span>
        <div class="min-w-0"><div class="truncate text-sm font-semibold text-[#f5f7fa]">{props.rootLabel}</div><div class="text-[10px] text-[#7bd0ff]">主代理</div></div>
      </div>
      <ul class="subagent-tree-roots">
        <For each={tree()}>{node => <Branch node={node} />}</For>
      </ul>
    </div>
  </section>;
}

