import type { ModelSection, VectorNode, VectorBinding } from '../../api/intelligentPage';

const node = (tag: string, attrs: Record<string, string>, children: VectorNode[] = [], text = ''): VectorNode => ({ tag, attrs, children, text, tail: '' });
export const vectorTree = node('svg', { id: 'root', viewBox: '0 0 400 240' }, [
  node('g', { id: 'wheel', transform: 'translate(5 0)' }, [node('path', { id: 'blade', d: 'M 100 60 C 120 40 140 60 120 80 Z' })]),
  node('path', { id: 'membrane', d: 'M 200 60 Q 240 30 280 60' }),
  node('text', { x: '20', y: '220' }, [], '曲线装置 <示意>'),
]);
const binding = (element_id: string, attribute: VectorBinding['attribute'], extra: Partial<VectorBinding> = {}): VectorBinding => ({
  element_id, attribute, output_index: 0, min: 0, max: 1, origin_x: 0, origin_y: 0,
  below: 'positive', at_or_above: 'danger', start_path: '', end_path: '', ...extra,
});
export const vectorModel: ModelSection = {
  type: 'model', id: 'vector', title: '曲线动画', hidden: false, description: '示例',
  parameters: [{ id: 'load', label: '负载', min: 0, max: 10, step: 1, value: 0, unit: '' }],
  outputs: [{ label: '负载值', operation: 'product', args: ['load', 1], min: 0, max: 10, unit: '', description: '',
    threshold: { value: 5, below: { label: '较低', description: '继续观察' }, at_or_above: { label: '较高', description: '请检查' } } }],
  graphics: [{ label: '曲线装置', shape: 'svg', x: 0, y: 0, width: 400, height: 240, color: 'accent', bindings: [
    binding('wheel', 'rotate', { min: 0, max: 180, origin_x: 100, origin_y: 80 }),
    binding('wheel', 'translate_x', { max: 20 }),
    binding('blade', 'fill'),
    binding('membrane', 'd', { start_path: 'M 200 60 Q 240 30 280 60', end_path: 'M 200 60 Q 240 100 280 60' }),
  ] }],
  animation: { duration_seconds: 1, tracks: [{ parameter_id: 'load', start: 0, end: 10 }] },
};

