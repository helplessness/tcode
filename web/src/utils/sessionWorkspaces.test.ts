import {expect,it} from 'vitest';
import {parseWorkspaceList,sessionWorkspaceKey,workspaceAccentHue} from './sessionWorkspaces';
import {reorderSessions} from './sessionReorder';
it('keeps project accents stable across rename, filtering and sort',()=>{
  const before=sessionWorkspaceKey({workspace_id:'project-a',workdir_name:'旧名字'});
  const after=sessionWorkspaceKey({workspace_id:'project-a',workdir_name:'新名字'});
  expect(workspaceAccentHue(before)).toBe(workspaceAccentHue(after));
  expect(workspaceAccentHue('id:project-a')).not.toBe(workspaceAccentHue('id:project-b'));
  for(const id of ['', 'unassigned','path:/中文/课题', 'id:project-a']) {
    expect(workspaceAccentHue(id)).toBeGreaterThanOrEqual(0);
    expect(workspaceAccentHue(id)).toBeLessThan(360);
  }
});
it('groups by workspace identity, not a possibly duplicated display name',()=>{
  expect(sessionWorkspaceKey({workspace_id:'a',workdir_name:'相同名称'})).not.toBe(sessionWorkspaceKey({workspace_id:'b',workdir_name:'相同名称'}));
  expect(sessionWorkspaceKey({workdir:'/a/test'})).not.toBe(sessionWorkspaceKey({workdir:'/b/test'}));
  expect(sessionWorkspaceKey({})).toBe('unassigned');
});
it('round trips grouping, aliases, pins, fold and sort preferences safely',()=>{
  const value={grouped:true,order:['id:b','id:a'],pinned:['id:b'],collapsed:['id:a'],names:{'id:b':'新名字'}};
  expect(parseWorkspaceList(JSON.stringify(value))).toEqual(value);
  expect(parseWorkspaceList('bad')).toEqual({grouped:false,order:[],pinned:[],collapsed:[],names:{}});
  expect(parseWorkspaceList('{"order":["a",3,"a"],"names":{"a":5}}').order).toEqual(['a']);
});
it('cannot move a session across workspace boundaries and retains filtered-out slots',()=>{
  const order=['a1','b1','a2','b2'];
  expect(reorderSessions(order,['a1','a2'],'a1','b1')).toBe(order);
  expect(reorderSessions(order,['a1','a2'],'a1','a2')).toEqual(['a2','b1','a1','b2']);
  expect(reorderSessions(['w1','hidden','w2'],['w1','w2'],'w2','w1')).toEqual(['w2','hidden','w1']);
});
