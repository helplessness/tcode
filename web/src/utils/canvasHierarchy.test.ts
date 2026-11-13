import { expect,it } from 'vitest';
import { canvasHierarchy } from './canvasHierarchy';
const parts=['motor','shaft','gear','body'].map(element_id=>({element_id,label:element_id,description:''}));
const elements=[['motor','root'],['wrap','motor'],['shaft','wrap'],['gear','motor'],['body','root']].map(([element_id,parent_id])=>({element_id,parent_id,tag:'g',text:'',fill:''}));
it('uses actual registered ancestry through wrappers and drills one level at a time',()=>{
  const h=canvasHierarchy(parts,elements);
  expect(h.children('').map(p=>p.element_id)).toEqual(['motor','body']);
  expect(h.navigation('motor').map(p=>p.element_id)).toEqual(['shaft','gear']);
  expect(h.navigation('shaft').map(p=>p.element_id)).toEqual(['shaft','gear']);
  expect(h.trail('shaft').map(p=>p.element_id)).toEqual(['motor','shaft']);
  expect(h.target('','shaft')).toBe('motor');
  expect(h.target('motor','shaft')).toBe('shaft');
  expect(h.target('shaft','body')).toBe('body');
  expect(h.assembly('motor')).toBe('motor');
  expect(h.assembly('shaft')).toBe('motor');
  expect(h.assembly('body')).toBe('');
});
it('preserves legacy flat parts and bounds malformed ancestry',()=>{
  const legacy=canvasHierarchy(parts,elements.map(({parent_id,...value})=>value));
  expect(legacy.children('')).toEqual(parts);
  expect(legacy.trail('shaft').map(p=>p.element_id)).toEqual(['shaft']);
  const broken=canvasHierarchy(parts,[...elements,{...elements[0],element_id:'wrap',parent_id:'wrap'}]);
  expect(broken.trail('shaft').map(p=>p.element_id)).toEqual(['shaft']);
  expect(legacy.target('','unknown')).toBe('unknown');
});
