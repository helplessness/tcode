// Development-only fixture entry; excluded from the production entry/build.
import { render } from 'solid-js/web';
import { createResource, Show } from 'solid-js';
import McpAppHost from '../../src/components/McpAppHost';
import StructuredContentBlocks from '../../src/components/StructuredContentBlocks';
import { sessionStore } from '../../src/stores/session';
import '../../src/index.css';
function Acceptance() {
  const [fixture] = createResource(async () => {
    const value = await (await fetch('/acceptance/fixture?tool=' + (new URLSearchParams(location.search).get('tool') || 'gnina'))).json();
    sessionStore.setCurrentSessionId(value.block.session_id);
    sessionStore.setMessages([]);
    return value;
  });
  return <main style={{ 'max-width': '1100px', margin: '20px auto' }}>
    <h1>科学结果选择 · 离线验收（无远端计算）</h1>
    <Show when={fixture()}>{value => <McpAppHost block={value().block} />}</Show>
  <section aria-label="即时会话选择"><StructuredContentBlocks blocks={sessionStore.messages().flatMap(m => m.content_blocks || [])} /></section>
  </main>;
}
render(() => <Acceptance />, document.getElementById('root')!);
