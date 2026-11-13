import { createEffect, createSignal, lazy, onCleanup, onMount, Show, Suspense, type JSX } from 'solid-js';
import { sessionStore } from '../stores/session';
import { dismissDetailsOnOutsideClick } from '../utils/dismissDetails';
import TaskLifecycleStrip from './TaskLifecycleStrip';
const SubagentsWorkspace = lazy(() => import('./SubagentsWorkspace'));
const TeamWorkspace = lazy(() => import('./TeamWorkspace'));
const SideChatWorkspace = lazy(() => import('./SideChatWorkspace'));
export type CollaborationTab = 'tasks' | 'tree' | 'office' | 'records' | 'discussion';
type Props = {
  active: boolean; tab: CollaborationTab; onTab: (tab: CollaborationTab) => void;
  discussion: boolean; selectedSessionId: string; pending: boolean;
  onDiscussion: (open: boolean) => void; onSelected: (id: string) => void;
  onQuote: (text: string) => void; context: JSX.Element;
};
export default function CollaborationPanel(props: Props) {
  const [treeVisited, setTreeVisited] = createSignal(false);
  const [officeVisited, setOfficeVisited] = createSignal(false);
  const [teamVisited, setTeamVisited] = createSignal(false);
  const [discussionVisited, setDiscussionVisited] = createSignal(false);
  const [returnTab, setReturnTab] = createSignal<Exclude<CollaborationTab, 'discussion'>>('office');
  createEffect(() => {
    if (props.tab === 'tree') setTreeVisited(true);
    if (props.tab === 'office') setOfficeVisited(true);
    if (props.tab === 'records') setTeamVisited(true);
    if (props.tab !== 'discussion') setReturnTab(props.tab);
    if (props.discussion) {
      setDiscussionVisited(true);
      if (props.tab !== 'discussion') props.onTab('discussion');
    }
  });
  let sessionInfo!: HTMLDetailsElement;
  const fallback = <p role="status">正在加载协作…</p>;
  onMount(() => onCleanup(dismissDetailsOnOutsideClick(() => sessionInfo)));
  const selectOverview = (tab: Exclude<CollaborationTab, 'discussion'>) => {
    props.onDiscussion(false);
    props.onTab(tab);
  };
  const openDiscussion = () => {
    setDiscussionVisited(true);
    props.onDiscussion(true);
    props.onTab('discussion');
  };
  const closeDiscussion = () => { props.onDiscussion(false); props.onTab(returnTab()); };
  return <section class="collaboration-panel" aria-label="当前会话协作" data-testid="collaboration-panel">
    <nav class="collaboration-tabs" aria-label="协作视图">
      <button aria-pressed={props.tab === 'office'} onClick={() => selectOverview('office')}>办公室</button>
      <button aria-pressed={props.tab === 'tasks'} onClick={() => selectOverview('tasks')}>执行任务</button>
      <button aria-pressed={props.tab === 'tree'} onClick={() => selectOverview('tree')}>委托树</button>
      <button aria-pressed={props.tab === 'records'} onClick={() => selectOverview('records')}>协作记录</button>
      <button aria-pressed={props.tab === 'discussion'} onClick={openDiscussion}>讨论</button>
    </nav>
    <TaskLifecycleStrip />
    <div class="collaboration-toolbar">
      <Show when={props.tab === 'discussion'} fallback={<button class="collaboration-discussion-toggle" onClick={openDiscussion}>打开子代理对话</button>}>
        <button class="collaboration-discussion-toggle" onClick={closeDiscussion}>返回{returnTab() === 'office' ? '办公室' : returnTab() === 'tree' ? '委托树' : returnTab() === 'records' ? '协作记录' : '执行任务'}</button>
      </Show>
      <details ref={sessionInfo}><summary>会话信息</summary>{props.context}</details>
    </div>
    <div class="collaboration-body">
      <div class="collaboration-overview">
        <Show when={officeVisited()}><div hidden={props.tab !== 'office'}><Suspense fallback={fallback}><SubagentsWorkspace office active={props.active && props.tab === 'office'}/></Suspense></div></Show>
        <div hidden={props.tab !== 'tasks'}><Suspense fallback={fallback}><SubagentsWorkspace compact active={props.active && props.tab === 'tasks'}/></Suspense></div>
        <Show when={treeVisited()}><div hidden={props.tab !== 'tree'}><Suspense fallback={fallback}><SubagentsWorkspace active={props.active && props.tab === 'tree'}/></Suspense></div></Show>
        <Show when={teamVisited()}><div hidden={props.tab !== 'records'}><Suspense fallback={fallback}><TeamWorkspace active={props.active && props.tab === 'records'}/></Suspense></div></Show>
        <Show when={discussionVisited()}><div hidden={props.tab !== 'discussion'}><Suspense fallback={fallback}>
          <SideChatWorkspace embedded active={props.active && props.tab === 'discussion'} selectedSessionId={props.selectedSessionId} pending={props.pending}
            onSelectedSessionChange={props.onSelected} onClose={closeDiscussion} onQuote={props.onQuote}/>
        </Suspense></div></Show>
      </div>
    </div>
    <Show when={!sessionStore.currentSessionId()}><p class="collaboration-empty">选择主会话后查看它的执行任务与讨论。</p></Show>
  </section>;
}
