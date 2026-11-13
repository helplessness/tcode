import { describe, expect, it } from 'vitest';
import { composerDrafts, type StructureRef } from './composer';

const site = (id: string, residue_number: number): StructureRef => ({
  id, label: `@位点${id}`, path: 'upload/a.pdb', sha256: 'a'.repeat(64),
  workspaceId: 'ws', sessionId: 'session', model_index: 0, chain: 'A',
  residue_number, insertion_code: '', name: 'GLY', operator: '',
});

describe('structure references in composer drafts', () => {
  it('keeps three pinned sites independent and allows removing one', () => {
    const key = JSON.stringify(['test', 'session']);
    const refs = [site('1', 10), site('2', 20), site('3', 30)];
    composerDrafts.update(key, { text: '测量距离', files: [], structureRefs: refs });
    composerDrafts.update(key, { structureRefs: refs.filter(ref => ref.id !== '1') });
    expect(composerDrafts.get(key).structureRefs?.map(ref => ref.residue_number)).toEqual([20, 30]);
    composerDrafts.accepted(key, { text: '测量距离', files: [], structureRefs: refs.filter(ref => ref.id !== '1') });
    expect(composerDrafts.get(key).structureRefs).toEqual([]);
  });
});
