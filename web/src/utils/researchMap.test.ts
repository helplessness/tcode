import { expect, it } from 'vitest';
import { experimentParents, researchMapAncestors, researchMapDescendantCounts, researchMapLayout, type MapPoint } from './researchMap';
import type { ResearchExperiment } from '../api/research';
it('keeps explicit ancestry and makes missing or cyclic parents visible at the root', () => {
  const rows = [['a', null], ['b', 'a'], ['missing', 'unknown'], ['self', 'self'], ['x', 'y'], ['y', 'x']].map(([id, parent]) => ({ experiment_id: id, parent_experiment_id: parent } as ResearchExperiment));
  expect(Object.fromEntries(experimentParents(rows))).toEqual({ a: '', b: 'a', missing: '', self: '', x: '', y: '' });
});
it('reveals the same nested object in either view without losing other expanded branches', () => {
  const rows: MapPoint[] = [
    { id: 'project', parent: '', kind: 'project' },
    { id: 's', parent: 'project', kind: 'session' },
    { id: 'a', parent: 's', kind: 'experiment' },
    { id: 'b', parent: 'a', kind: 'experiment' },
  ];
  const opened = new Set(['another-branch', ...researchMapAncestors(rows, 'b')]);
  expect([...opened]).toEqual(['another-branch', 'a', 's', 'project']);
  for (const analysis of [false, true]) expect(researchMapLayout(rows, opened, analysis).points.get('b')?.visible).toBe(true);
  expect(researchMapAncestors(rows, 'missing').size).toBe(0);
  expect(researchMapAncestors(rows, 'project').size).toBe(0);
});
it('keeps analysis in a stable tree even with every branch collapsed', () => {
  const rows: MapPoint[] = [{ id: 'project', parent: '', kind: 'project' }, ...['a', 'b', 'c'].map(id => ({ id, parent: 'project', kind: 'session' as const }))];
  const layout = researchMapLayout(rows, new Set(['project']), true);
  expect(['a', 'b', 'c'].map(id => layout.points.get(id)?.x)).toEqual([450, 450, 450]);
  expect(layout.points.get('b')!.y - layout.points.get('a')!.y).toBe(200);
  expect(layout.points.get('c')!.y - layout.points.get('b')!.y).toBe(200);
  expect(layout.points.get('project')!.x).toBe(130);
});
it('retracts hidden branches, expands recorded descendants, and separates visible nodes', () => {
  const rows: MapPoint[] = [
    { id: 'project', parent: '', kind: 'project' },
    { id: 's', parent: 'project', kind: 'session' },
    { id: 'unlinked', parent: 'project', kind: 'group' },
    { id: 'a', parent: 's', kind: 'experiment' },
    { id: 'b', parent: 'a', kind: 'experiment' },
    { id: 'c', parent: 'a', kind: 'experiment' },
    { id: 'd', parent: 'unlinked', kind: 'experiment' },
  ];
  const overview = researchMapLayout(rows, new Set(['project']));
  expect(overview.points.get('a')).toEqual({ ...overview.points.get('s'), visible: false });
  const full = researchMapLayout(rows, new Set(rows.map(n => n.id)));
  expect([...full.points.values()].every(p => p.visible)).toBe(true);
  expect(full.points.get('b')!.y).not.toBe(full.points.get('c')!.y);
  expect(full.points.get('a')!.x).toBeLessThan(full.points.get('b')!.x);
  const closed = researchMapLayout(rows, new Set());
  expect([...closed.points.values()].filter(p => p.visible)).toHaveLength(1);
  expect([...closed.points.values()].every(p => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
  expect(researchMapLayout([], new Set()).points.size).toBe(0);
});

it('does not reserve horizontal space for deep collapsed descendants',()=>{
  const rows:MapPoint[]=[{id:'p',parent:'',kind:'project'},...Array.from({length:12},(_,i)=>({id:String(i),parent:i?String(i-1):'p',kind:'experiment' as const}))];
  const closed=researchMapLayout(rows,new Set(['p']),true);
  const open=researchMapLayout(rows,new Set(rows.map(n=>n.id)),true);
  expect(closed.width).toBeLessThan(open.width);
  expect([...closed.points.values()].filter(n=>n.visible)).toHaveLength(2);
});
it('counts all collapsed descendants, not only direct children, without counting itself', () => {
  const rows: MapPoint[] = [
    {id:'p',parent:'',kind:'project'}, {id:'s',parent:'p',kind:'session'},
    {id:'a',parent:'s',kind:'experiment'}, {id:'b',parent:'a',kind:'experiment'},
    {id:'c',parent:'p',kind:'finding'}, {id:'orphan',parent:'missing',kind:'finding'},
  ];
  expect(Object.fromEntries(researchMapDescendantCounts(rows))).toEqual({p:4,s:2,a:1,b:0,c:0,orphan:0});
  expect(Object.fromEntries(researchMapDescendantCounts([{id:'a',parent:'b',kind:'experiment'},{id:'b',parent:'a',kind:'experiment'}]))).toEqual({a:1,b:1});
});
