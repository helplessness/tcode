export type CitationRef =
  | { kind: 'evidence'; id: string }
  | { kind: 'knowledge'; id: string }
  | { kind: 'ontology_assertion' | 'ontology_evidence'; id: string };

export type CitationOpenDetail = { ref: CitationRef; trigger?: HTMLElement | null };
export const CITATION_OPEN_EVENT = 'tcode-open-citation';

export const openCitation = (ref: CitationRef, trigger?: HTMLElement | null) => {
  window.dispatchEvent(new CustomEvent<CitationOpenDetail>(CITATION_OPEN_EVENT, { detail: { ref, trigger } }));
};
