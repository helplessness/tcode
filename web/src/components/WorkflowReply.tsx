import { Index, Show, createMemo } from 'solid-js';
import MarkdownContent from './MarkdownContent';

/** A single durable reply, with stage results that stay readable after completion. */
export default function WorkflowReply(props: { content: string; messageId: string }) {
  const sections = createMemo(() => {
    const [progress, ...conclusion] = props.content.split('\n## 整体结论\n');
    const chunks = progress.split(/\n(?=### \d+\.)/);
    return {
      introduction: chunks[0].replace(/^## Workflow 进展\s*/, '').trim(),
      stages: chunks.slice(1).map((chunk) => {
        const newline = chunk.indexOf('\n');
        return { title: chunk.slice(0, newline).replace(/^### /, ''), content: chunk.slice(newline + 1).trim() };
      }),
      conclusion: conclusion.join('\n## 整体结论\n').trim(),
    };
  });
  return (
    <div class="space-y-4">
      <Show when={sections().introduction}>
        <MarkdownContent content={sections().introduction} role="assistant" renderKey={`${props.messageId}:intro`} />
      </Show>
      <div class="space-y-2 border-l border-[#526756]/40 pl-4">
        <Index each={sections().stages}>
          {(stage, index) => (
            <details open class="group py-1">
              <summary class="cursor-pointer text-sm font-medium">
                {stage().title}
              </summary>
              <div class="pt-3 pb-2 text-sm leading-7">
                <MarkdownContent content={stage().content} role="assistant" renderKey={`${props.messageId}:stage:${index}`} />
              </div>
            </details>
          )}
        </Index>
      </div>
      <Show when={sections().conclusion}>
        <div class="border-t border-[#526756]/30 pt-4">
          <div class="mb-3 text-sm font-semibold">整体结论</div>
          <MarkdownContent content={sections().conclusion} role="assistant" renderKey={`${props.messageId}:conclusion`} />
        </div>
      </Show>
    </div>
  );
}
