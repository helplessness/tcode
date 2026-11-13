import { describe, it, expect } from 'vitest';
import { builtinCommands, filterCommands, findCommandTrigger, parseBuiltinCommand, skillCommandItems } from './slashCommands';

describe('composer commands', () => {
  it('shows the complete skill catalog without allowing disabled or dedicated skills', () => {
    const skills = [
      {name: 'gnina-runner', description: '对接', required_agent: '', agent_states: {build: {available: true, enabled: true}}},
      {name: 'disabled', description: '暂停', required_agent: '', agent_states: {build: {available: true, enabled: false}}},
      {name: 'agent-browser', description: '浏览器', required_agent: 'browser', agent_states: {build: {available: false, enabled: false}}},
    ];
    const items = skillCommandItems(skills, 'build', 'tcode-native');
    expect(items).toHaveLength(3);
    expect(items[0].disabled).toBeUndefined();
    expect(items[1].disabled).toContain('已禁用');
    expect(items[2].disabled).toContain('browser');
    expect(skillCommandItems(skills, 'build', 'external').every(item => Boolean(item.disabled))).toBe(true);
  });
  it('finds slash and dollar prefixes at the caret without treating file paths as commands', () => {
    expect(findCommandTrigger('/')).toMatchObject({ prefix: '/', query: '', start: 0 });
    expect(findCommandTrigger('使用 $gnina')).toMatchObject({ prefix: '$', query: 'gnina', start: 3 });
    expect(findCommandTrigger('/mod after', 4)?.query).toBe('mod');
    expect(findCommandTrigger('/home/user/input.pdb')).toBeNull();
  });
  it('recognizes only registered standalone commands and preserves arguments', () => {
    expect(parseBuiltinCommand('/model local/model')).toEqual({ name: 'model', arguments: 'local/model' });
    expect(parseBuiltinCommand('/skill gnina-runner 请执行对接')).toEqual({ name: 'skill', arguments: 'gnina-runner 请执行对接' });
    expect(parseBuiltinCommand('请解释 /compact')).toBeNull();
    expect(parseBuiltinCommand('/home/test')).toBeNull();
    expect(parseBuiltinCommand('/example_plugin:example-plan test')).toBeNull();
  });
  it('provides builtins without plugins and keeps search order stable', () => {
    expect(filterCommands(builtinCommands, '').map(item => item.id)).toEqual(['help', 'new', 'model', 'compact', 'skills']);
    expect(filterCommands(builtinCommands, '上下文')[0].id).toBe('compact');
    expect(filterCommands(builtinCommands, 'missing')).toEqual([]);
  });
});
