import type { CanvasStyle } from './canvas';

export type PageItem = { label: string; description: string };
type Section = { id: string; title: string; hidden: boolean };
export type ModelParameter = { id: string; label: string; unit: string; min: number; max: number; step: number; value: number };
export type ModelOutput = {
  label: string; unit: string; operation: 'sum' | 'difference' | 'product' | 'ratio' | 'linear';
  args: Array<string | number>; min: number; max: number; description: string;
  threshold: { value: number; below: PageItem; at_or_above: PageItem } | null;
};
export type GraphicBinding = { output_index: number; min: number; max: number };
export type GraphicColor = 'accent' | 'positive' | 'warning' | 'danger' | 'muted';
export type VectorNode = { tag: string; attrs: Record<string, string>; text: string; tail: string; children: VectorNode[] };
export type VectorBinding = {
  element_id: string; output_index: number;
  attribute: 'x' | 'y' | 'cx' | 'cy' | 'r' | 'rx' | 'ry' | 'width' | 'height' | 'opacity' | 'stroke-width' | 'translate_x' | 'translate_y' | 'rotate' | 'scale' | 'fill' | 'stroke' | 'd';
  min: number; max: number; origin_x: number; origin_y: number;
  below: GraphicColor; at_or_above: GraphicColor; start_path: string; end_path: string;
};
export type ModelGraphic = {
  label: string; shape: 'rect' | 'ellipse' | 'svg'; source?: string; bindings?: VectorBinding[];
  x: number | GraphicBinding; y: number | GraphicBinding; width: number | GraphicBinding; height: number | GraphicBinding;
  color: GraphicColor | { output_index: number; below: GraphicColor; at_or_above: GraphicColor };
};
export type ModelAnimation = { duration_seconds: number; tracks: Array<{ parameter_id: string; start: number; end: number }> };
export type ModelSection = Section & { type: 'model'; description: string; parameters: ModelParameter[]; outputs: ModelOutput[]; graphics?: ModelGraphic[]; animation?: ModelAnimation | null };
export type PageSection =
  | ModelSection
  | (Section & { type: 'html'; html: string; css: string })
  | (Section & { type: 'card'; text: string; items: PageItem[] })
  | (Section & { type: 'chart'; chart_type: 'bar' | 'line'; unit: string; description: string; points: Array<PageItem & { value: number }> })
  | (Section & { type: 'steps'; items: PageItem[] })
  | (Section & { type: 'button'; action: 'show' | 'ask'; target_id: string; prompt: string });
export type PageData = {
  page_id: string; revision_id: string; current_revision_id: string; session_id: string;
  title: string; parent_revision_id: string | null;
  style: CanvasStyle; display_style: CanvasStyle;
  document: { title: string; description: string; sections: PageSection[] };
  graphic_trees?: Record<string, Array<VectorNode | null>>;
  is_deleted?: boolean;
};
export async function pageRequest<T>(pageId: string, sessionId: string, revisionId: string, body?: unknown, suffix = '', before = '', researchId = ''): Promise<T> {
  const query = new URLSearchParams({ session_id: sessionId });
  if (!body && revisionId) query.set('revision_id', revisionId);
  if (before) query.set('before', before);
  if (researchId) query.set('research_id', researchId);
  const response = await fetch(`/api/intelligent-ui/v1/pages/${encodeURIComponent(pageId)}${suffix || (body ? '/actions' : '')}${suffix.includes('?')?'&':'?'}${query}`, {
    credentials: 'same-origin', method: body ? 'POST' : 'GET',
    headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail === 'revision_conflict' ? '页面已更新，请查看最新版本后再操作。' : typeof result.detail === 'string' ? result.detail : '页面请求失败');
  return result;
}

export function chartScale(values: number[]) {
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const span = max - min || 1;
  return { min, max, position: (value: number) => ((value - min) / span) * 100 };
}
