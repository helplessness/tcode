import { describe, expect, it } from 'vitest';
import { diagramPalette, interpolatePath, modelDiagramMarkup } from './modelDiagram';
import { evaluateModel } from './pageModel';
import { animationValues } from './pageAnimation';

import { vectorModel, vectorTree } from './__fixtures__/modelDiagram';

describe('general vector linkage', () => {
  it('uses one frame for group rotation/translation, path morph, color and text, preserving source transforms', () => {
    const original = JSON.stringify(vectorTree);
    const results = evaluateModel(vectorModel, animationValues(vectorModel.animation!, .5, {}));
    const html = modelDiagramMarkup(vectorModel, results, [vectorTree], diagramPalette('clay'));
    expect(results[0].value).toBe(5);
    expect(html).toContain('translate(10 0) rotate(90 100 80) translate(5 0)');
    expect(html).toContain('d="M 200 60 Q 240 65 280 60"');
    expect(html).toContain('fill="#b95164"');
    expect(html).toContain('曲线装置 &lt;示意&gt;');
    expect(html).not.toContain('id="wheel"');
    expect(JSON.stringify(vectorTree)).toBe(original);
  });

  it('hides only dependent parts on unavailable results and preserves static text', () => {
    const section = { ...vectorModel, outputs: [{ ...vectorModel.outputs[0], operation: 'ratio' as const, args: [10, 0] }] };
    const html = modelDiagramMarkup(section, evaluateModel(section, { load: 0 }), [vectorTree], diagramPalette('manual'));
    expect(html).not.toContain('<path'); expect(html).toContain('曲线装置 &lt;示意&gt;');
  });

  it('rejects unknown tags, URLs and missing vector data rather than rendering executable markup', () => {
    const result = evaluateModel(vectorModel, { load: 0 });
    expect(() => modelDiagramMarkup(vectorModel, result, [{ ...vectorTree, tag: 'script' }], diagramPalette('clay'))).toThrow();
    expect(() => modelDiagramMarkup(vectorModel, result, [{ ...vectorTree, attrs: { ...vectorTree.attrs, fill: 'url(https://example.com)' } }], diagramPalette('clay'))).toThrow();
    expect(() => modelDiagramMarkup(vectorModel, result, [], diagramPalette('clay'))).toThrow('尚未加载');
    expect(() => interpolatePath('M 0 0 L 1 1', 'M 0 0 Q 1 1 2 2', .5)).toThrow();
  });
});
