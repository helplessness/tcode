import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('goal composer attachment wiring', () => {
  const source = readFileSync(new URL('./MessageInput.tsx', import.meta.url), 'utf8');
  const branch = source.slice(source.indexOf('if (goalMode()) {', source.indexOf('const handleSubmit')), source.indexOf('// Capture sticky context'));

  it('sends the shared attachment payload without an attachment prohibition', () => {
    expect(branch).toContain('submitGoalMessage(finalMessage)');
    expect(branch).not.toContain('attachedFiles().length');
    expect(branch).toContain('selectedSkills().length');
    expect(branch).toContain('filter(isImageAttachment).length > 2');
  });

  it('clears only the successful unchanged draft in its original session', () => {
    expect(branch).toMatch(/if \(await submitGoalMessage\(finalMessage\)\) \{/);
    expect(branch).toContain('sessionId === sessionStore.currentSessionId()');
    expect(branch).toContain('buildFinalMessage() === finalMessage) resetComposer()');
    const panel = readFileSync(new URL('./GoalModePanel.tsx', import.meta.url), 'utf8');
    expect(panel).toContain('const limit = active() ? 2000 : 4000');
    expect(panel).toContain('将内容附件切换为路径模式');
  });
  it('keeps time and round extensions independent and explicitly submitted', () => {
    const panel = readFileSync(new URL('./GoalModePanel.tsx', import.meta.url), 'utf8');
    expect(panel).toContain('extra_rounds: extraRounds(), extra_minutes: extraMinutes()');
    expect(panel).toContain('增加轮次');
    expect(panel).toContain('增加时间（分钟）');
    expect(panel).toContain('增加预算并继续');
    expect(panel).not.toContain('extra_rounds: 4, extra_minutes: 15');
  });
});
