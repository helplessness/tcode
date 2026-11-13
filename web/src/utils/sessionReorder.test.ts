import {expect,it} from 'vitest';
import {reorderSessions} from './sessionReorder';
it('moves a row while retaining hidden and other-group slots',()=>{
  expect(reorderSessions(['pin','a','hidden','b','c'],['a','b','c'],'a','c')).toEqual(['pin','b','hidden','c','a']);
  expect(reorderSessions(['a','b','c'],['a','b','c'],'c','a')).toEqual(['c','a','b']);
});
it('ignores missing ids, pinned boundaries and no-op moves',()=>{
  const order=['pin','a','b'];
  expect(reorderSessions(order,['a','b'],'a','pin')).toBe(order);
  expect(reorderSessions(order,['a','b'],'missing','b')).toBe(order);
  expect(reorderSessions(order,['a','b'],'a','a')).toBe(order);
});
