export type PaperLibraryItem = {
  doc_id: string;
  title: string;
  authors: string[];
  year: string | number;
  journal: string;
  identifiers: Record<string, unknown>;
  summary: string;
  keywords: string[];
  page_span: number[];
  passage_count: number;
  fact_count: number;
  visual_counts: Record<string, number>;
  source_path: string;
  pdf_available: boolean;
  reference_only: boolean;
  updated_at: string;
};

export type PaperLibraryDocument = {
  doc_id: string;
  title: string;
  metadata: Record<string, any>;
  summary: Record<string, any>;
  passages: Array<Record<string, any>>;
  facts: Array<Record<string, any>>;
  available_layers: string[];
  missing_layers: string[];
  source_path: string;
  pdf_available: boolean;
  reference_only: boolean;
  updated_at: string;
};

async function json<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(payload.detail || `请求失败 (${response.status})`));
  return payload as T;
}

export async function listPaperLibrary(workspaceId: string, query = '', offset = 0, limit = 50) {
  const params = new URLSearchParams({workspace_id: workspaceId, q: query, offset: String(offset), limit: String(limit)});
  return json<{enabled: boolean; items: PaperLibraryItem[]; total: number; offset: number; limit: number}>(await fetch(`/api/paper-library?${params}`));
}

export async function getPaperLibraryDocument(workspaceId: string, docId: string) {
  const params = new URLSearchParams({workspace_id: workspaceId});
  return json<{enabled: boolean; document: PaperLibraryDocument}>(await fetch(`/api/paper-library/${encodeURIComponent(docId)}?${params}`));
}

export function paperLibraryPdfUrl(workspaceId: string, docId: string) {
  const params = new URLSearchParams({workspace_id: workspaceId});
  return `/api/paper-library/${encodeURIComponent(docId)}/pdf?${params}`;
}
