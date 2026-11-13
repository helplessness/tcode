import { expect,it } from 'vitest';
import { snapshotMapNodes, researchFieldText } from './researchSnapshots';
import { researchMapAncestors } from './researchMap';
import { researchChangesLink,researchChangesLinks,type ResearchSnapshotObject } from '../api/researchChanges';
it('builds snapshot-only nodes for a removed experiment with recorded ancestry',()=>{
  const objects:ResearchSnapshotObject[]=[
    {id:'project',kind:'project',object_id:'p',title:'旧课题',data:{}},
    {id:'hypothesis:h',kind:'hypothesis',object_id:'h',title:'假说',data:{}},
    {id:'experiment:b',kind:'experiment',object_id:'b',title:'后续',data:{parent_experiment_id:'a',hypothesis_id:'h'}},
    {id:'experiment:a',kind:'experiment',object_id:'a',title:'基线',data:{run_snapshot:{session_id:'s'},hypothesis_id:'h'}},
    {id:'finding:f',kind:'finding',object_id:'f',title:'旧结论',data:{statement:'旧判断'}},
  ];
  const nodes=snapshotMapNodes(objects);
  expect(nodes.map(n=>n.id)).toEqual(['project','hypothesis:h','finding:f','session:s','experiment:a','experiment:b']);
  expect([...researchMapAncestors(nodes,'experiment:b')]).toEqual(['experiment:a','session:s','project']);
  expect(nodes.find(n=>n.id==='finding:f')?.text).toBe('旧判断');
  expect(nodes.find(n=>n.id==='experiment:b')?.fields).toMatchObject({'关联假说':'假说','上级实验':'基线'});
});
it('shows completed process receipts outside collapsed tool records and deduplicates a comparison',()=>{
  const link={schema:'tcode.research-changes-link.v1',workspace_id:'w',research_id:'p',base_snapshot_id:'a',head_snapshot_id:'b'};
  const tool={tool_name:'research_workspace',state:'completed',metadata:{research_changes:link}};
  expect(researchChangesLinks([tool,tool,{...tool,state:'error'},{...tool,tool_name:'other'}])).toEqual([link]);
  expect(researchChangesLinks([{...tool,error:'failed'}])).toEqual([]);
});
it('does not infer missing session titles or cyclic experiment ancestry',()=>{
  const nodes=snapshotMapNodes([{id:'project',kind:'project',object_id:'p',title:'P',data:{}},...['a','b'].map((id,i)=>({id:`experiment:${id}`,kind:'experiment' as const,object_id:id,title:id,data:{parent_experiment_id:i?'a':'b'}}))]);
  expect(nodes.filter(n=>n.kind==='experiment').map(n=>n.parent)).toEqual(['project','project']);
});
it('only accepts bounded complete platform comparison receipts',()=>{
  const valid={schema:'tcode.research-changes-link.v1',workspace_id:'w',research_id:'p',base_snapshot_id:'a',head_snapshot_id:'b'};
  expect(researchChangesLink(valid)).toEqual(valid);
  expect(researchChangesLink({...valid,base_snapshot_id:''})).toBeNull();
  expect(researchChangesLink({...valid,schema:'html'})).toBeNull();
  expect(researchChangesLink({...valid,workspace_id:'w'.repeat(161)})).toBeNull();
});

it('uses readable fallback when a referenced name was not saved',()=>{
  const nodes=snapshotMapNodes([{id:'project',kind:'project',object_id:'p',title:'P',data:{}},{id:'experiment:x',kind:'experiment',object_id:'x',title:'实验',data:{hypothesis_id:'missing',parent_experiment_id:'missing'}}]);
  expect(nodes.find(n=>n.id==='experiment:x')?.fields).toMatchObject({'关联假说':'未保存假说名称','上级实验':'未保存实验名称'});
});

it('keeps useful values without exposing internal references or serialized code',()=>{
  expect(researchFieldText('decision','inconclusive')).toBe('尚无定论');
  expect(researchFieldText('metrics',{score:0.9,custom:2})).toBe('评分：0.9；指标 2：2');
  expect(researchFieldText('source_event_ids',['evt-internal'])).toBe('已记录 1 项来源或关联信息');
  expect(researchFieldText('run_snapshot',{session_id:'internal'})).not.toContain('internal');
});
