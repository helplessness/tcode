import { describe, expect, it } from 'vitest';
import { ontologyQualifierFilters } from './ontology';

describe('ontology condition values', () => {
  it('keeps numeric, boolean, text and quoted numeric values distinct', () => {
    expect(ontologyQualifierFilters([
      {key:'pH',value:'7'}, {key:'temperature',value:'37 °C'},
      {key:'verified',value:'true'}, {key:'numeric_text',value:'"7"'}, {key:'',value:''},
    ])).toEqual({pH:7,temperature:'37 °C',verified:true,numeric_text:'7'});
  });
  it('rejects incomplete, duplicate and non-scalar conditions', () => {
    expect(()=>ontologyQualifierFilters([{key:'pH',value:''}])).toThrow('名称和值');
    expect(()=>ontologyQualifierFilters([{key:'pH',value:'7'},{key:'pH',value:'8'}])).toThrow('重复');
    for (const value of ['null', '[]', '{}', '1e999']) {
      expect(()=>ontologyQualifierFilters([{key:'pH',value}])).toThrow('条件值');
    }
  });
});
