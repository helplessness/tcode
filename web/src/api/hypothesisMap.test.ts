import {it,expect} from 'vitest';
import {hypothesisMapLink,hypothesisMapLinks} from './hypothesisMap';
it('accepts only successful platform hypothesis map receipts and keeps distinct focus targets',()=>{
 const link={schema:'tcode.hypothesis-map-link.v1',research_id:'p',workspace_id:'w',hypothesis_id:'h'};
 expect(hypothesisMapLink(link)).toEqual(link);
 expect(hypothesisMapLink({...link,workspace_id:''})).toBeNull();
 const tool={tool_name:'research_workspace',state:'completed',metadata:{hypothesis_map:link}};
 expect(hypothesisMapLinks([tool,tool,{...tool,state:'failed'},{...tool,error:'failed'}])).toEqual([link]);
});
