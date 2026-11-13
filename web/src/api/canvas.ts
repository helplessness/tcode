export type SolidPiece = {id:string;label:string;description:string;kind:'plate'|'block'|'rail'|'cylinder'|'ring'|'bolt'|'knob'|'lathe'|'sphere';position:number[];size:number[];radius:number;inner_radius:number;length:number;profile?:number[][];axis:'x'|'y'|'z';material:'alloy'|'polymer'|'rubber'|'accent';color?:string;explode?:number[];order:number};
export type SolidScene = {id:string;title:string;description:string;bore_fits?:Array<{shaft_id:string;bore_id:string}>;assemblies:Array<{id:string;label:string;description:string;explode:number[];order:number;pieces:SolidPiece[]}>};
export type SpatialReport = {status:string;initial_count:number;motion_count:number;note:string;issues_truncated:boolean;bore_fits?:Array<{element_ids:string[];labels:string[];coaxial:boolean;bore_diameter:number;engaged_diameter:number|null;swept_diameter:number|null;diameter_ok:boolean|null;shoulder_ok:boolean|null}>;issues:Array<{element_ids:string[];labels:string[];message:string;phase:string}>;suggestions:Array<{element_id:string;label:string;vector:number[];order:number;message:string;remaining_motion_risks:number}>};
export type CanvasExplosion = { x: number; y: number; order: number };
export type CanvasAnimation = { duration_seconds: number; mode: 'explode' | 'assemble' | 'roundtrip' };
export type CanvasPart = { element_id: string; label: string; description: string; explode?: CanvasExplosion | null; illustration?: 'isometric' | null; animation?: CanvasAnimation | null; solid_scene?:SolidScene };
export type CanvasStyle = 'clay' | 'manual';
export type CanvasLayoutQuality = {
  status: 'passed' | 'needs_review' | 'failed'; coverage: 'partial';
  counts: { error: number; warning: number }; issues_truncated?: boolean;
  issues: Array<{ code: string; severity: 'error' | 'warning'; message: string; element_ids: Array<string | null> }>;
  unchecked?: string[]; note?: string;
};
export type CanvasPreference = { style: CanvasStyle | null; default_style: CanvasStyle; effective_style: CanvasStyle; enabled: boolean; effective_enabled: boolean };

export async function canvasPreference(preference?: Pick<CanvasPreference, 'style' | 'enabled'>): Promise<CanvasPreference> {
  const response = await fetch('/api/intelligent-ui/v1/preferences', {
    method: preference === undefined ? 'GET' : 'PUT', credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    ...(preference === undefined ? {} : { body: JSON.stringify(preference) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '智能 UI 设置失败');
  return result;
}
export type CanvasHistory = {
  current_revision_id: string; next_before: string;
  items: Array<{ revision_id: string; parent_revision_id: string | null; created: string; actor: string }>;
};
export type CanvasData = {
  canvas_id: string; session_id: string; title: string; revision_id: string;
  current_revision_id: string; parent_revision_id: string | null; source: string;
  source_digest: string; parts: CanvasPart[];
  style?: CanvasStyle;
  display_style?: CanvasStyle;
  layout_quality?: CanvasLayoutQuality;
  is_deleted?: boolean;
  elements: Array<{ element_id: string; tag: string; text: string; fill: string; transform?: string; parent_id?: string }>;
};
export type CanvasOperation = { op: 'set_attribute' | 'set_text' | 'set_description' | 'set_label'; element_id: string; name?: string; value: string }
  | { op: 'set_explode'; element_id: string; explode: CanvasExplosion | null }
  | { op: 'set_solid_direction';element_id:string;vector:number[];order:number }
  | { op: 'translate_solid';element_id:string;vector:number[] }
  | { op: 'set_animation'; element_id: string; animation: CanvasAnimation | null };

export async function packCanvasRequest<T>(assetId: string, sessionId: string, objectId?: string, kind: 'canvases' | 'pages' = 'canvases'): Promise<T> {
  const query = new URLSearchParams({ session_id: sessionId });
  const response = await fetch(`/api/intelligent-ui/v1/pack-assets/${encodeURIComponent(assetId)}/${kind}?${query}`, {
    method: objectId ? 'POST' : 'GET', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...(objectId ? { body: JSON.stringify({ object_id: objectId }) } : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '读取画布失败');
  return result;
}

export async function canvasRequest<T>(canvasId: string, sessionId: string, suffix = '', body?: unknown, revisionId = '', before = '', researchId = ''): Promise<T> {
  const query = new URLSearchParams({ session_id: sessionId });
  if (revisionId) query.set('revision_id', revisionId);
  if (before) query.set('before', before);
  if (researchId) query.set('research_id', researchId);
  const response = await fetch(`/api/intelligent-ui/v1/canvases/${encodeURIComponent(canvasId)}${suffix}${suffix.includes('?')?'&':'?'}${query}`, {
    method: body ? 'POST' : 'GET', credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  }).catch(()=>{throw new Error('无法连接本地服务，请确认TCode正在运行，然后重新加载画布。');});
  const result = await response.json();
  if (response.status===429) throw new Error(`画布读取过于频繁，请在${Math.min(60,Math.max(1,Number(response.headers.get('Retry-After'))||60))}秒后重新加载画布；已保存数据不会丢失。`);
  if (!response.ok) {
    const detail = typeof result.detail === 'string' ? result.detail : '画布请求失败';
    const messages:Record<string,string>={canvas_not_found:'当前工作区中找不到这份画布，请确认会话和工作区是否正确。',session_not_found:'当前会话无法读取，请重新打开会话后重试。',canvas_scope_required:'当前账号没有读取或修改这份画布的权限。'};
    throw new Error(messages[detail] || (detail === 'revision_conflict' ? '版本已经改变，请查看最新版本后重新修改。'
      : detail === 'research_decision_changed_reload_required' ? '课题快照已经改变，请核对刷新后的记录再保存。' : (/^[a-z_]+$/.test(detail)?'画布暂时无法读取或保存，请重新加载后重试。':detail)));
  }
  return result as T;
}
