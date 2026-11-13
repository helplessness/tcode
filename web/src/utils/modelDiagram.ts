import type { GraphicColor, ModelSection, VectorNode } from '../api/intelligentPage';
import { evaluateGraphics, evaluateModel } from './pageModel';

export type DiagramPalette = Record<GraphicColor, string> & { background: string; text: string; mutedText: string };
export const diagramPalette = (style: 'clay' | 'manual'): DiagramPalette => style === 'clay'
  ? { background: '#fff8fc', text: '#51435f', mutedText: '#786581', accent: '#76518c', positive: '#4d8269', warning: '#ac782d', danger: '#b95164', muted: '#8b8193' }
  : { background: '#ffffff', text: '#28332c', mutedText: '#647167', accent: '#2563eb', positive: '#24805a', warning: '#a66a12', danger: '#c24545', muted: '#78818c' };
export const xml = (value: unknown) => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');
const tags = new Set('svg g rect circle ellipse line polyline polygon path text tspan title desc'.split(' '));
const attributes = new Set('viewBox width height x y x1 y1 x2 y2 cx cy r rx ry d points fill stroke stroke-width stroke-dasharray stroke-linecap stroke-linejoin opacity fill-opacity stroke-opacity font-size font-family font-weight text-anchor dominant-baseline transform fill-rule'.split(' '));
const pathPattern = /[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?/g;

export function interpolatePath(start: string, end: string, progress: number) {
  const a = start.match(pathPattern) ?? [], b = end.match(pathPattern) ?? [];
  if (!a.length || a.length !== b.length) throw Error('路径形变结构不兼容');
  return a.map((token, i) => {
    if (/^[A-Za-z]$/.test(token)) { if (token !== b[i] || /a/i.test(token)) throw Error('路径形变结构不兼容'); return token; }
    const from = Number(token), to = Number(b[i]);
    if (!Number.isFinite(from) || !Number.isFinite(to)) throw Error('路径形变数值无效');
    return String(from + (to - from) * progress);
  }).join(' ');
}

function vectorMarkup(tree: VectorNode, section: ModelSection, index: number, results: ReturnType<typeof evaluateModel>, palette: DiagramPalette, width: number, height: number) {
  const bindings = section.graphics![index].bindings ?? [];
  const render = (node: VectorNode, depth: number): string => {
    if (!tags.has(node.tag) || depth > 32) throw Error('图形包含不支持的节点');
    const attrs = { ...node.attrs }, transforms: string[] = [];
    let tx = 0, ty = 0, translated = false;
    for (const binding of bindings.filter(b => b.element_id === node.attrs.id)) {
      const result = results[binding.output_index];
      if (!result || result.value === null) return xml(node.tail);
      const position = result.position / 100, value = binding.min + position * (binding.max - binding.min);
      if (binding.attribute === 'translate_x') { tx = value; translated = true; }
      else if (binding.attribute === 'translate_y') { ty = value; translated = true; }
      else if (binding.attribute === 'rotate') transforms.push(`rotate(${value} ${binding.origin_x} ${binding.origin_y})`);
      else if (binding.attribute === 'scale') transforms.push(`translate(${binding.origin_x} ${binding.origin_y}) scale(${value}) translate(${-binding.origin_x} ${-binding.origin_y})`);
      else if (binding.attribute === 'fill' || binding.attribute === 'stroke') attrs[binding.attribute] = palette[result.value < result.output.threshold!.value ? binding.below : binding.at_or_above];
      else if (binding.attribute === 'd') attrs.d = interpolatePath(binding.start_path, binding.end_path, position);
      else attrs[binding.attribute] = String(value);
    }
    if (translated) transforms.unshift(`translate(${tx} ${ty})`);
    if (transforms.length) attrs.transform = [...transforms, attrs.transform || ''].join(' ');
    if (depth === 0) { attrs.x = '0'; attrs.y = '0'; attrs.width = String(width); attrs.height = String(height); }
    const serialized = Object.entries(attrs).filter(([key]) => key !== 'id').map(([key, value]) => {
      if (!attributes.has(key) || /url\s*\(|[\\\u0000]/i.test(value)) throw Error('图形包含不支持的属性');
      return ` ${key}="${xml(value)}"`;
    }).join('');
    return `<${node.tag}${serialized}>${xml(node.text)}${node.children.map(child => render(child, depth + 1)).join('')}</${node.tag}>${xml(node.tail)}`;
  };
  return render(tree, 0);
}

export function modelDiagramMarkup(section: ModelSection, results: ReturnType<typeof evaluateModel>, trees: Array<VectorNode | null>, palette: DiagramPalette) {
  return evaluateGraphics(section, results).filter(g => g.available).map(g => {
    let body: string;
    if (g.shape === 'svg') {
      const tree = trees[g.index];
      if (!tree) throw Error('复杂图形数据尚未加载，请重新读取页面');
      body = `<g transform="translate(${g.x} ${g.y})">${vectorMarkup(tree, section, g.index, results, palette, g.width, g.height)}</g>`;
    } else if (g.shape === 'rect') body = `<rect x="${g.x}" y="${g.y}" width="${g.width}" height="${g.height}" rx="${Math.min(8, g.width / 4, g.height / 4)}"/>`;
    else body = `<ellipse cx="${g.x + g.width / 2}" cy="${g.y + g.height / 2}" rx="${g.width / 2}" ry="${g.height / 2}"/>`;
    return `<g fill="${xml(palette[g.color])}" stroke="${xml(palette.text)}" stroke-width="1" stroke-opacity=".5"><title>${xml(g.label)}：${xml(g.state)}</title>${body}</g>`;
  }).join('');
}
