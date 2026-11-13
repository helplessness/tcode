import type { SkillCatalogItem } from '../../api/capabilities';

export type CommandItem = {
  id: string;
  label: string;
  description: string;
  kind: 'builtin' | 'plugin' | 'skill' | 'model';
  value?: string;
  disabled?: string;
  current?: boolean;
};

export const builtinCommands: CommandItem[] = [
  { id: 'help', label: '/help', description: '查看命令和使用帮助', kind: 'builtin' },
  { id: 'new', label: '/new', description: '在当前工作空间开始新会话', kind: 'builtin' },
  { id: 'model', label: '/model', description: '选择当前会话的模型', kind: 'builtin' },
  { id: 'compact', label: '/compact', description: '压缩上下文，保留历史归档', kind: 'builtin' },
  { id: 'skills', label: '/skills', description: '选择本次任务使用的 Skill', kind: 'builtin' },
];

export function findCommandTrigger(text: string, cursor = text.length) {
  const match = text.slice(0, cursor).match(/(?:^|\s)([/$])([^\s/$]*)$/);
  return match ? { prefix: match[1], query: match[2], start: cursor - match[2].length - 1, end: cursor } : null;
}

export function filterCommands(items: CommandItem[], query: string) {
  const needle = query.toLowerCase().trim();
  return items.filter(item => `${item.label} ${item.description}`.toLowerCase().includes(needle));
}

export function skillCommandItems(skills: Pick<SkillCatalogItem, 'name' | 'description' | 'required_agent' | 'agent_states'>[], agent: string, runtime: string): CommandItem[] {
  return skills.map(skill => {
    const state = skill.agent_states[agent];
    const disabled = runtime !== 'tcode-native' ? '当前 Harness 不支持 TCode Skill 选择'
      : !state?.available ? (skill.required_agent ? `此技能绑定专用 Agent：${skill.required_agent}` : '当前 Agent 不支持此技能')
      : !state.enabled ? '此技能已禁用，请在管理技能中启用' : undefined;
    return {id: `skill:${skill.name}`, label: `$${skill.name}`, value: skill.name, description: skill.description, kind: 'skill', disabled};
  });
}

export function parseBuiltinCommand(text: string) {
  const match = text.trim().match(/^\/(help|new|model|compact|skills|skill)(?:\s+([\s\S]*))?$/);
  return match ? { name: match[1], arguments: (match[2] || '').trim() } : null;
}
