import { createSignal } from 'solid-js';

export type AttachedFile = {
  id: string;
  name: string;
  path?: string;
  file?: File;
  mode: 'path' | 'content';
  content?: string;
  source?: 'upload' | 'mention';
  sessionId?: string;
  portableRef?: { kind: 'artifact'; artifact_id: string } | { kind: 'artifact_bundle'; bundle_id: string };
};
export type StructureRef = {
  id: string; label: string; path: string; sha256: string; workspaceId: string; sessionId: string;
  jobId?: string; artifactRoot?: string;
  model_index: number; chain: string; residue_number: number; insertion_code: string; name: string; operator: string;
};
export type ComposerDraft = { text: string; files: AttachedFile[]; structureRefs?: StructureRef[]; selectedSkills?: string[] };
export type FailedSubmission = ComposerDraft & { id: string; error: string };
export type StagedGuidance = ComposerDraft & { id: string; mode: 'steer' | 'queue' };
type GuidanceSubmission = ComposerDraft & { id: string; mode: 'steer' | 'queue' };
type Entry = ComposerDraft & {
  failed?: FailedSubmission;
  guidance?: GuidanceSubmission;
  stagedGuidance?: StagedGuidance[];
};
const [entries, setEntries] = createSignal<Record<string, Entry>>({});
const prefix = 'tcode-composer-v1:';

export function composerIdentity(): string {
  try { return localStorage.getItem('tcode_agent_identity_scope') || 'local'; }
  catch { return 'local'; }
}

export function composerKey(sessionId: string | null): string {
  return JSON.stringify([composerIdentity(), sessionId || 'new']);
}

function sanitizeFiles(files: unknown, key: string): AttachedFile[] {
  const sessionId = JSON.parse(key)[1];
  return Array.isArray(files) ? files.filter(file => file && typeof file === 'object'
    && file.sessionId === sessionId && typeof file.name === 'string'
    && (typeof file.path === 'string' || typeof file.content === 'string')) : [];
}

function sanitizeStructureRefs(refs: unknown, key: string): StructureRef[] {
  const sessionId = JSON.parse(key)[1];
  return Array.isArray(refs) ? refs.filter(ref => ref && typeof ref === 'object'
    && ref.sessionId === sessionId && typeof ref.id === 'string' && typeof ref.label === 'string'
    && typeof ref.path === 'string' && typeof ref.sha256 === 'string' && ref.sha256.length === 64
    && typeof ref.workspaceId === 'string' && typeof ref.chain === 'string'
    && Number.isInteger(ref.model_index) && Number.isInteger(ref.residue_number)).slice(0, 8) : [];
}

function sanitizeStagedGuidance(items: unknown, key: string): StagedGuidance[] {
  return Array.isArray(items) ? items.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const candidate = item as Partial<StagedGuidance>;
    if (typeof candidate.id !== 'string' || typeof candidate.text !== 'string'
      || !['steer', 'queue'].includes(String(candidate.mode || ''))) return [];
    return [{
      id: candidate.id,
      mode: candidate.mode as 'steer' | 'queue',
      text: candidate.text,
      files: sanitizeFiles(candidate.files, key),
      structureRefs: sanitizeStructureRefs(candidate.structureRefs, key),
    }];
  }).slice(-12) : [];
}

export const composerDrafts = {
  get(key: string): Entry {
    const cached = entries()[key];
    if (cached) return cached;
    try {
      const value = JSON.parse(localStorage.getItem(prefix + key) || '{}');
      return {
        text: typeof value.text === 'string' ? value.text : '',
        selectedSkills: Array.isArray(value.selectedSkills) ? value.selectedSkills.filter((name: unknown) => typeof name === 'string').slice(0, 8) : [],
        files: sanitizeFiles(value.files, key),
        structureRefs: sanitizeStructureRefs(value.structureRefs, key),
        failed: value.failed && typeof value.failed.text === 'string'
          ? { ...value.failed, files: sanitizeFiles(value.failed.files, key), structureRefs: sanitizeStructureRefs(value.failed.structureRefs, key) } : undefined,
        guidance: value.guidance && typeof value.guidance.id === 'string'
          && typeof value.guidance.text === 'string'
          && ['steer', 'queue'].includes(value.guidance.mode)
          ? { ...value.guidance, files: sanitizeFiles(value.guidance.files, key), structureRefs: sanitizeStructureRefs(value.guidance.structureRefs, key) } : undefined,
        stagedGuidance: sanitizeStagedGuidance(value.stagedGuidance, key),
      };
    } catch { return { text: '', files: [], structureRefs: [] }; }
  },
  update(key: string, patch: Partial<Entry>): void {
    const value = { ...this.get(key), ...patch };
    setEntries(previous => ({ ...previous, [key]: value }));
    try {
      // Binary File objects stay in memory; durable references are revalidated
      // by the session-owned file/artifact boundary when submitted.
      localStorage.setItem(prefix + key, JSON.stringify(value, (name, field) => name === 'file' ? undefined : field));
    } catch { /* A full/disabled browser store must not erase the live draft. */ }
  },
  accepted(key: string, sent: ComposerDraft): void {
    const current = this.get(key);
    if (current.text === sent.text && JSON.stringify(current.files) === JSON.stringify(sent.files)
      && JSON.stringify(current.structureRefs || []) === JSON.stringify(sent.structureRefs || [])) {
      this.update(key, { text: '', files: [], structureRefs: [], failed: undefined });
    }
  },
  guidanceRequest(key: string, sent: ComposerDraft, mode: 'steer' | 'queue', newId: string): string {
    const previous = this.get(key).guidance;
    if (previous && previous.mode === mode && previous.text === sent.text
      && JSON.stringify(previous.files) === JSON.stringify(sent.files)
      && JSON.stringify(previous.structureRefs || []) === JSON.stringify(sent.structureRefs || [])) return previous.id;
    this.update(key, { guidance: { ...sent, mode, id: newId } });
    return newId;
  },
  guidanceAccepted(key: string, id: string): void {
    const current = this.get(key);
    if (current.guidance?.id === id) {
      this.update(key, { guidance: undefined, failed: current.failed?.id === id ? undefined : current.failed });
    }
  },
  stageGuidance(key: string, sent: ComposerDraft, mode: 'steer' | 'queue', id: string): string {
    const current = this.get(key);
    this.update(key, {
      text: '',
      files: [],
      structureRefs: [],
      stagedGuidance: [...(current.stagedGuidance || []), { ...sent, mode, id }].slice(-12),
    });
    return id;
  },
  removeStagedGuidance(key: string, id: string): void {
    const current = this.get(key);
    this.update(key, {
      stagedGuidance: (current.stagedGuidance || []).filter(item => item.id !== id),
    });
  },
  restoreStagedGuidance(key: string, id: string): void {
    const current = this.get(key);
    const staged = (current.stagedGuidance || []).find(item => item.id === id);
    if (!staged) return;
    this.update(key, {
      text: [staged.text, current.text].filter(Boolean).join('\n\n'),
      files: [...staged.files, ...current.files.filter(file => !staged.files.some(old => old.id === file.id))],
      structureRefs: [
        ...(staged.structureRefs || []),
        ...(current.structureRefs || []).filter(ref => !(staged.structureRefs || []).some(old => old.id === ref.id)),
      ],
      stagedGuidance: (current.stagedGuidance || []).filter(item => item.id !== id),
    });
  },
  restoreFailed(key: string): void {
    const current = this.get(key);
    if (!current.failed) return;
    const failed = current.failed;
    this.update(key, {
      text: current.text === failed.text ? current.text : [failed.text, current.text].filter(Boolean).join('\n\n'),
      files: [...failed.files, ...current.files.filter(file => !failed.files.some(old => old.id === file.id))],
      structureRefs: [...(failed.structureRefs || []), ...(current.structureRefs || []).filter(ref => !(failed.structureRefs || []).some(old => old.id === ref.id))],
      failed: undefined,
    });
  },
};
