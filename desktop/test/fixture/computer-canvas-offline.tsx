// Loaded only by the isolated offline renderer test, never the production app.
import { render } from 'solid-js/web';
import type { ComputerState } from '../../../web/src/desktop';
import type { CanvasData } from '../../../web/src/api/canvas';
import '../../../web/src/studio.css';

const host = window as any;
let statusListener: (state: ComputerState) => void;
let resolveStatus: (state: ComputerState) => void;
const previews: Array<(frame: unknown) => void> = [];
host.tcodeDesktop = {
  computerStatus: () => new Promise(resolve => { resolveStatus = resolve; }),
  onComputerStatus: (listener: typeof statusListener) => { statusListener = listener; return () => {}; },
  computerPanelRequest: async () => ({request_id:null}),
  computerPreview: () => new Promise(resolve => previews.push(resolve)),
};
const requests: string[] = [];
const source = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1800 1000">
 <rect id="背景" x="5" y="5" width="1790" height="990" fill="#f9f7fc"/>
 <g id="candidate"><rect x="80" y="100" width="620" height="220" rx="20" fill="#eee0f5"/><text x="120" y="200" font-size="40">关键候选 · 保留来源</text></g>
 <path d="M700 210 C900 210 900 700 1100 700" fill="none" stroke="#64547f" stroke-width="8"/>
 <g id="counter"><rect x="1100" y="600" width="620" height="220" rx="20" fill="#ffe2dd"/><text x="1140" y="710" font-size="40">反证与错误 · 待复核</text></g>
 <script>parent.__unsafeCanvasScript=true</script>
</svg>`;
const canvas: CanvasData = {
  canvas_id:'offline',session_id:'offline-session',title:'候选与反证关系图',revision_id:'revision-one',
  current_revision_id:'revision-one',parent_revision_id:null,source,source_digest:'fixture',display_style:'clay',
  parts:[{element_id:'candidate',label:'关键候选',description:'保留候选身份及原始来源。'},
    {element_id:'counter',label:'反证与错误',description:'反证不能因为图形简化而丢失。'}],
  elements:[{element_id:'candidate',tag:'g',text:'',fill:''},{element_id:'counter',tag:'g',text:'',fill:''}],
  layout_quality:{status:'needs_review',coverage:'partial',counts:{error:0,warning:1},
    issues:[{code:'label_overlap_estimated',severity:'warning',message:'标签可能重叠，请检查实际显示。',element_ids:['counter']}],
    unchecked:['曲线路径边界及路径连线避让'],note:'静态检查不是视觉验收。'},
};
host.fetch = async (url: string, options?: RequestInit) => {
  requests.push(`${options?.method || 'GET'} ${url}`);
  if (!url.startsWith('/api/intelligent-ui/v1/canvases/offline') || options?.method !== 'GET') throw new Error('Unexpected offline request');
  return new Response(JSON.stringify(url.includes('/research') ? {enabled:false,projects:[],record:null} : url.includes('/revisions') ? {items:[],next_before:'',current_revision_id:'revision-one'} : canvas));
};
const {default: ComputerUsePanel} = await import('../../../web/src/components/ComputerUsePanel');
const {default: CanvasBlock} = await import('../../../web/src/components/CanvasBlock');
render(() => <><ComputerUsePanel/><main style={{'margin-top':'24px'}}><CanvasBlock allowSessionActions={false} block={{
  type:'canvas',canvas_id:'offline',session_id:'offline-session',revision_id:'revision-one',title:canvas.title,fallback:'离线画布',
} as any}/></main></>, document.getElementById('root')!);
host.offline = {
  push: (state: ComputerState) => statusListener(state),
  resolveStatus: (state: ComputerState) => resolveStatus(state),
  preview: (index: number, frame: unknown) => previews[index]?.(frame),
  previewCount: () => previews.length,
  requests, canvas,
};
