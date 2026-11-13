import { For, Show, createMemo, type JSX } from 'solid-js';
import type { Message } from '../stores/session';
import { collectProcessTools, presentChatTurns } from '../utils/chatPresentation';
import { MainProcessGroup } from './ChatView';
import MarkdownContent from './MarkdownContent';
import StructuredContentBlocks from './StructuredContentBlocks';

function SideChatBubble(props: { message: Message }) {
  const user = () => props.message.role === 'user';
  return (
    <article class={`side-chat-bubble ${user() ? 'side-chat-bubble--user' : 'side-chat-bubble--agent'}`} aria-label={user() ? '用户问题' : 'Agent 回复'}>
      <Show when={!user()}><div class="side-chat-speaker">Agent</div></Show>
      <MarkdownContent content={props.message.content} role={props.message.role}
        renderKey={`side:${props.message.id}`} allowSessionActions={false}/>
      <StructuredContentBlocks blocks={props.message.content_blocks} allowSessionActions={false}/>
    </article>
  );
}

export default function SideChatTranscript(props: {
  messages: Message[];
  running: boolean;
  sessionId: string;
  renderTool: (entry: ReturnType<typeof collectProcessTools>[number]) => JSX.Element;
}) {
  const turns = createMemo(() => presentChatTurns(props.messages, props.running));
  return <For each={turns()}>{turn => (
    <div class="side-chat-turn">
      <Show when={turn.user}>{message => <SideChatBubble message={message()}/>}</Show>
      <Show when={turn.processMessages.length > 0}>
        <div class="side-chat-process">
          <MainProcessGroup messages={turn.processMessages} running={turn.running}
            hasFinalResult={Boolean(turn.result)}
            preferenceId={`side:${props.sessionId}:${turn.key}`} narrationTitle="旁白"
            defaultToolsCollapsed={true} includeOrphanOutputs={true}
            allowSessionActions={false} renderTool={props.renderTool}/>
        </div>
      </Show>
      <Show when={turn.result}>{message => <SideChatBubble message={message()}/>}</Show>
    </div>
  )}</For>;
}
