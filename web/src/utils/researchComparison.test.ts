import { expect,it } from 'vitest';
import type { ResearchChange,ResearchSnapshotObject } from '../api/researchChanges';
import { comparisonChangeLabel,comparisonLayout,snapshotComparison } from './researchComparison';
import { snapshotMapNodes } from './researchSnapshots';

const object=(id:string,kind:ResearchSnapshotObject['kind'],title:string,data:Record<string,unknown>={}):ResearchSnapshotObject=>({id,kind,object_id:id.split(':').pop()!,title,data});
const before=[object('project','project','课题'),object('hypothesis:a','hypothesis','解释甲'),object('hypothesis:b','hypothesis','解释乙'),
  object('experiment:one','experiment','基线实验',{hypothesis_id:'a'}),
  object('experiment:two','experiment','验证实验',{parent_experiment_id:'one'}),object('finding:old','finding','原结论')];
const after=[...before.filter(o=>!['finding:old','experiment:two'].includes(o.id)),
  object('experiment:two','experiment','改名后的验证实验',{hypothesis_id:'b'}),object('finding:new','finding','新结论')];

it('uses identical anchors and canvas size for retained objects despite addition, deletion, rename and reparenting',()=>{
  const comparison=snapshotComparison(before,after);
  const nodesBefore=snapshotMapNodes(before),nodesAfter=snapshotMapNodes(after);
  const left=comparisonLayout(comparison,nodesBefore,new Set(nodesBefore.map(n=>n.id)));
  const right=comparisonLayout(comparison,nodesAfter,new Set(nodesAfter.map(n=>n.id)));
  for(const node of nodesBefore.filter(n=>nodesAfter.some(other=>other.id===n.id)))expect(left.points.get(node.id)).toEqual(right.points.get(node.id));
  expect([left.width,left.height]).toEqual([right.width,right.height]);
  expect(left.width).toBeGreaterThan(Math.max(...[...left.points.values()].map(p=>p.x))+240);
  expect(nodesBefore.find(n=>n.id==='experiment:two')?.parent).toBe('experiment:one');
  expect(nodesAfter.find(n=>n.id==='experiment:two')?.parent).toBe('hypothesis:b');
  expect(comparison.nodeIds.before.has('finding:new')).toBe(false);
  expect(comparison.nodeIds.after.has('finding:old')).toBe(false);
  expect(comparison.marks.get('finding:old')?.status).toBe('removed');
  expect(comparison.marks.get('finding:new')?.status).toBe('added');
});

it('keeps slots when branches collapse and expands the actual ancestry of either snapshot',()=>{
  const comparison=snapshotComparison(before,after), nodes=snapshotMapNodes(after);
  const collapsed=comparisonLayout(comparison,nodes,new Set(['project']));
  const expanded=comparisonLayout(comparison,nodes,new Set(nodes.map(n=>n.id)));
  expect(collapsed.points.get('experiment:two')?.visible).toBe(false);
  expect(expanded.points.get('experiment:two')?.visible).toBe(true);
  for(const n of nodes)expect([collapsed.points.get(n.id)?.x,collapsed.points.get(n.id)?.y]).toEqual([expanded.points.get(n.id)?.x,expanded.points.get(n.id)?.y]);
  expect(new Set([...expanded.points.values()].map(p=>`${p.x},${p.y}`)).size).toBe(expanded.points.size);
});

it('describes relationship changes using saved names and distinguishes metadata-only changes',()=>{
  const comparison=snapshotComparison(before,after);
  const change=comparison.marks.get('experiment:two')!;
  expect(change.parentChanged).toBe(true);
  expect(change.relationChanges).toContain('上级实验：基线实验 → 未关联');
  expect(change.relationChanges).toContain('关联假说：未关联 → 解释乙');
  expect(change.relationChanges.join('')).not.toMatch(/experiment:|hypothesis:|one|two/);
  const a=object('experiment:x','experiment','计算',{run_snapshot:{session_id:'s',score:1}});
  const b=object('experiment:x','experiment','计算',{run_snapshot:{score:2,session_id:'s'}});
  const mark=snapshotComparison([before[0],a],[before[0],b]).marks.get(a.id)!;
  expect(mark.relationChanges).toEqual([]);
  const delta={status:'changed',before:a.data,after:b.data} as ResearchChange;
  expect(comparisonChangeLabel(delta)).toBe('内容修改');
  expect(comparisonChangeLabel({...delta,after:{hypothesis_id:'b'}})).toBe('关系修改');
  expect(comparisonChangeLabel({...delta,status:'removed'})).toBe('删除');
  const reordered=object(a.id,a.kind,a.title,{run_snapshot:{score:1,session_id:'s'}});
  expect(snapshotComparison([before[0],a],[before[0],reordered]).marks.has(a.id)).toBe(false);
});

it('retains synthetic source sessions, empty versions and safe handling of cyclic experiment references',()=>{
  const a=object('experiment:x','experiment','计算',{run_snapshot:{session_id:'old'}});
  const b=object('experiment:x','experiment','计算',{run_snapshot:{session_id:'new'}});
  const cmp=snapshotComparison([before[0],a],[before[0],b]);
  expect(cmp.nodeIds.before.has('session:old')).toBe(true);
  expect(cmp.marks.get('session:new')?.status).toBe('added');
  expect(cmp.marks.get(a.id)?.relationChanges).toContain('来源会话：原来源会话 → 新的来源会话');
  expect(snapshotComparison([],[]).layout.points.size).toBe(0);
  const cycle=[before[0],object('experiment:x','experiment','甲',{parent_experiment_id:'y'}),object('experiment:y','experiment','乙',{parent_experiment_id:'x'})];
  const result=snapshotComparison(cycle,[before[0]]);
  expect([...result.layout.points.values()].every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y))).toBe(true);
});
