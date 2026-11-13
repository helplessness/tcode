import { normalizeSubagentStatus } from './subagentTree';

// Only a live transition triggers a visit; persisted completed tasks stay at their desks.
export function shouldReport(previous: string | undefined, next: string) {
  return previous !== undefined && normalizeSubagentStatus(previous) === 'running'
    && normalizeSubagentStatus(next) === 'completed';
}

export const idleActions = ['休息', '吃点心', '哼歌', '打盹'] as const;

export const reportLines = [
  '汇报完毕！',
  '任务完成啦！',
  '结果已送达！',
  '工作顺利完成！',
  '请查收成果！',
  '本次任务完成！',
  '材料都整理好了！',
  '负责人，我来汇报！',
] as const;

export const clampOfficeZoom = (zoom: number) => Math.min(4, Math.max(0.25, zoom));

export type OfficeActorState = 'idle' | 'thinking' | 'tooling' | 'asking' | 'done' | 'failed' | 'interrupted';

export function officeActorState(status: string, activity = '', tool = ''): OfficeActorState {
  const raw = String(status || '').toLowerCase();
  const note = String(activity || '').toLowerCase();
  if (raw === 'interrupted' || raw === 'terminated' || note.includes('中止') || note.includes('中断')) return 'interrupted';
  const normalized = normalizeSubagentStatus(raw);
  if (normalized === 'failed') return 'failed';
  if (normalized === 'completed') return 'done';
  if (normalized !== 'running') return 'idle';
  if (note.includes('批准') || note.includes('确认') || note.includes('approval')) return 'asking';
  return tool ? 'tooling' : 'thinking';
}

export function officeToolGlyph(tool = '') {
  const name = tool.toLowerCase();
  if (/shell|bash|terminal|exec|command/.test(name)) return '▣';
  if (/search|browser|web|fetch/.test(name)) return '⌕';
  if (/file|write|edit|patch/.test(name)) return '✎';
  if (/image|canvas|draw/.test(name)) return '▧';
  if (/agent|task|delegate/.test(name)) return '◇';
  return tool ? '⚙' : '';
}

const actorPalettes = [
  ['#638c80','#493f40','#465464'], ['#6e82ad','#394866','#48536c'],
  ['#b77958','#5b4038','#654a43'], ['#8c70a0','#473c59','#594761'],
  ['#4f8d9b','#334d59','#405b65'], ['#8f8156','#514a35','#61573d'],
] as const;

export function officeActorPalette(sessionId: string) {
  let hash = 0;
  for (const char of sessionId) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  const [shirt, hair, trousers] = actorPalettes[hash % actorPalettes.length];
  return { shirt, hair, trousers };
}
