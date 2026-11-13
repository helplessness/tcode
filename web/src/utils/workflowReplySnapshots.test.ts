import { describe, expect, it } from 'vitest';
import { mergeAndStabilizeVisibleMessages, toVisibleMessages } from './messageSnapshots';

describe('durable workflow reply', () => {
  it('updates one reply without losing stages and keeps a second run separate', () => {
    const first = toVisibleMessages([{ id: 'workflow-run:a:narration', role: 'assistant', presentation: 'workflow_narration', content: '### 1. 反应\n磷酸基减少，尚未验证。' }]);
    const completed = toVisibleMessages([
      { id: first[0].id, role: 'assistant', presentation: 'workflow_narration', content: `${first[0].content}\n### 2. 机制\n候选 A 存在底物冲突。\n## 整体结论\n保留为未验证方向。` },
      { id: 'workflow-run:b:narration', role: 'assistant', presentation: 'workflow_narration', content: '另一运行尚无公开结果。' },
    ]);
    const restored = mergeAndStabilizeVisibleMessages(first, completed);
    expect(restored).toHaveLength(2);
    expect(restored[0].content).toContain('磷酸基减少');
    expect(restored[0].content).toContain('底物冲突');
    expect(restored[0].content).toContain('整体结论');
    expect(restored[1].content).not.toContain('候选 A');
    expect(mergeAndStabilizeVisibleMessages(restored, completed)).toBe(restored);
  });
});
