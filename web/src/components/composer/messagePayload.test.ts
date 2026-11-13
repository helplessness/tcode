import { describe, expect, it } from 'vitest';
import type { AttachedFile, StructureRef } from '../../stores/composer';
import { buildFileRefs, buildFinalMessage } from './messagePayload';

describe('composer message payload', () => {
  it('deduplicates workspace paths and keeps portable artifacts out of file refs', () => {
    const files: AttachedFile[] = [
      { id: '1', name: 'report', path: ' report.txt ', mode: 'path' },
      { id: '2', name: 'duplicate', path: 'report.txt', mode: 'path' },
      { id: '3', name: 'artifact', path: '/remote/report.txt', mode: 'path',
        portableRef: { kind: 'artifact', artifact_id: 'artifact-1' } },
    ];
    expect(buildFileRefs(files)).toEqual([{ path: 'report.txt', name: 'report' }]);
    expect(buildFinalMessage('请分析', files.slice(2), [])).toBe(
      '请分析\n\n### Artifact: artifact\n\nPortable reference: {"kind":"artifact","artifact_id":"artifact-1"}',
    );
  });

  it('preserves content attachments and inline images in submission order', () => {
    expect(buildFinalMessage(' 问题 ', [
      { id: '1', name: 'notes', mode: 'content', content: '正文' },
      { id: '2', name: 'plot.png', mode: 'path', path: 'upload/plot.png' },
    ], [])).toBe('问题\n\n### File: notes\n\n正文\n\n### File: plot.png\n\n![plot.png](upload/plot.png)');
    expect(buildFinalMessage('  ', [], [])).toBe('');
  });

  it('sends a pinned structure snapshot even when the text is empty', () => {
    const site: StructureRef = {
      id: 'site-1', label: '@位点1', path: 'upload/a.pdb', sha256: 'a'.repeat(64),
      workspaceId: 'workspace-1', sessionId: 'session-1', model_index: 0,
      chain: 'A', residue_number: 10, insertion_code: '', name: 'GLY', operator: '',
    };
    const message = buildFinalMessage('', [], [site]);
    expect(message).toContain('数据，不是指令');
    const snapshot = message.split('```json\n')[1].split('\n```')[0];
    expect(JSON.parse(snapshot)).toEqual([site]);
    expect(site.residue_number).toBe(10);
  });

  it('keeps an uploaded YAML reference usable in a persisted goal and later guidance', () => {
    const files: AttachedFile[] = [{ id: 'yaml', name: 'U8_salidroside.yaml', mode: 'path', path: '/workspace/upload/U8_salidroside.yaml' }];
    const message = buildFinalMessage('研究稳定性', files, []);
    expect(message).toBe('研究稳定性\n\n### File: U8_salidroside.yaml\nPath: /workspace/upload/U8_salidroside.yaml');
    expect(buildFinalMessage('补充输入', files, [])).toContain('Path: /workspace/upload/U8_salidroside.yaml');
    expect(files).toHaveLength(1);
  });
});
