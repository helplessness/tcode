import { Show, createMemo } from 'solid-js';
import type { ModelSection, VectorNode } from '../api/intelligentPage';
import type { CanvasStyle } from '../api/canvas';
import { diagramPalette, modelDiagramMarkup } from '../utils/modelDiagram';
import { evaluateModel } from '../utils/pageModel';

export default function ModelDiagram(props: { section: ModelSection; results: ReturnType<typeof evaluateModel>; trees?: Array<VectorNode | null>; style?: CanvasStyle }) {
  const content = createMemo(() => {
    try { return { markup: modelDiagramMarkup(props.section, props.results, props.trees ?? [], diagramPalette(props.style ?? 'clay')), error: '' }; }
    catch (reason) { return { markup: '', error: reason instanceof Error ? reason.message : '图形暂时无法显示' }; }
  });
  return <Show when={!content().error} fallback={<p role="alert">{content().error}</p>}>
    <svg viewBox="0 0 400 240" role="img" aria-label={`${props.section.title}联动图形，大小、位置、颜色和状态详见下方图形说明`} innerHTML={content().markup} />
  </Show>;
}
