import { describe, expect, it } from 'vitest';
import type { ModelSection } from '../api/intelligentPage';
import { evaluateGraphics, evaluateModel, modelDefaults, modelNumber, parameterValue } from './pageModel';

const model: ModelSection = {
  type: 'model', id: 'budget', title: '预算试算', hidden: false, description: '示例',
  parameters: [{ id: 'quantity', label: '数量', unit: '件', min: 0, max: 20, step: 1, value: 2 }],
  outputs: [{ label: '总价', unit: '元', operation: 'product', args: ['quantity', 50], min: 0, max: 1000, description: '',
    threshold: { value: 500, below: { label: '预算内', description: '可继续' }, at_or_above: { label: '达到预算线', description: '请复核' } } }],
};

describe('controlled parameter linkage', () => {
  it('updates the value, graphic, equation and state together, including equality and return', () => {
    const initial = evaluateModel(model, modelDefaults(model))[0];
    expect(initial).toMatchObject({ value: 100, position: 10, state: { label: '预算内' } });
    const changed = evaluateModel(model, { quantity: 10 })[0];
    expect(changed).toMatchObject({ value: 500, position: 50, state: { label: '达到预算线', description: '请复核' } });
    expect(changed.equation).toBe('数量（10） × 50');
    expect(evaluateModel(model, { quantity: 9 })[0].state?.label).toBe('预算内');
    expect(evaluateModel(model, modelDefaults(model))).toEqual([initial]);
    expect(model.parameters[0].value).toBe(2);
  });

  it.each([
    ['sum', [2, -3], -1], ['difference', [2, 3], -1], ['product', [-2, 3], -6],
    ['ratio', [3, 2], 1.5], ['linear', [-2, 3, 4], -2],
  ] as const)('computes %s without executing expressions', (operation, args, value) => {
    const section = { ...model, outputs: [{ ...model.outputs[0], operation, args: [...args], min: -10, max: 10 }] };
    expect(evaluateModel(section, modelDefaults(model))[0].value).toBe(value);
  });

  it('does not invent zero or retain an old state when division is unavailable', () => {
    const section = { ...model, outputs: [{ ...model.outputs[0], operation: 'ratio' as const, args: [10, 'quantity'] }] };
    expect(evaluateModel(section, { quantity: 0 })[0]).toMatchObject({ value: null, state: null, position: 0, error: '除数为零，请调整参数。' });
    expect(evaluateModel(section, { quantity: 2 })[0].value).toBe(5);
  });

  it('clips only the graphic and retains actual negative and oversized values', () => {
    const section = { ...model, outputs: [{ ...model.outputs[0], max: 50 }] };
    expect(evaluateModel(section, modelDefaults(model))[0]).toMatchObject({ value: 100, position: 100, outside: true });
    section.outputs[0] = { ...section.outputs[0], operation: 'difference', args: [0, 'quantity'] };
    expect(evaluateModel(section, modelDefaults(model))[0]).toMatchObject({ value: -2, position: 0, outside: true });
  });

  it('rejects empty, nonfinite and out of range inputs and results', () => {
    for (const raw of ['', ' ', 'NaN', 'Infinity', '-1', '21', '1e309']) expect(parameterValue(model.parameters[0], raw)).toBeNull();
    for (const raw of ['0', '20', '2.5']) expect(parameterValue(model.parameters[0], raw)).toBe(Number(raw));
    for (const quantity of [NaN, Infinity, -1, 21]) expect(evaluateModel(model, { quantity })[0].value).toBeNull();
    const huge = { ...model, outputs: [{ ...model.outputs[0], operation: 'linear' as const, args: [1e6, 1e6, 1] }] };
    expect(evaluateModel(huge, modelDefaults(model))[0].value).toBeNull();
    expect(modelNumber(1e-8)).not.toBe('0');
    expect(modelNumber(0.1 + 0.2)).toBe('0.3');
    expect(modelNumber(8.05549999999998)).toBe('8.0555');
  });
});

const graphical: ModelSection = {
  ...model,
  graphics: [
    { label: '负载块', shape: 'rect', x: { output_index: 0, min: 20, max: 180 }, y: 30,
      width: { output_index: 0, min: 40, max: 140 }, height: { output_index: 0, min: 40, max: 100 },
      color: { output_index: 0, below: 'positive', at_or_above: 'danger' } },
    { label: '指示圆', shape: 'ellipse', x: 330, y: 150, width: 40, height: 40, color: 'accent' },
  ],
};

describe('parameter-linked shape geometry', () => {
  it('drives size, position, color and state from the same result and resets without mutating the definition', () => {
    const original = JSON.stringify(graphical);
    const evaluate = (quantity: number) => evaluateGraphics(graphical, evaluateModel(graphical, { quantity }));
    expect(evaluate(2)[0]).toMatchObject({ available: true, x: 36, y: 30, width: 50, height: 46, color: 'positive', state: '预算内' });
    expect(evaluate(10)[0]).toMatchObject({ x: 100, width: 90, height: 70, color: 'danger', state: '达到预算线' });
    expect(evaluate(20)[0]).toMatchObject({ x: 180, width: 140, height: 100, color: 'danger' });
    expect(evaluate(9)[0]).toMatchObject({ x: 92, width: 85, height: 67, color: 'positive' });
    expect(evaluate(2)[0]).toEqual(evaluateGraphics(graphical, evaluateModel(graphical, modelDefaults(graphical)))[0]);
    expect(evaluate(20)[1]).toEqual(evaluate(2)[1]);
    expect(JSON.stringify(graphical)).toBe(original);
  });

  it('supports reverse mapping and clips geometry at the declared output range, retaining actual results', () => {
    const section: ModelSection = { ...graphical, outputs: [{ ...model.outputs[0], max: 100 }], graphics: [
      { ...graphical.graphics![0], x: { output_index: 0, min: 180, max: 20 } },
    ] };
    const results = evaluateModel(section, { quantity: 10 });
    expect(results[0].value).toBe(500);
    expect(evaluateGraphics(section, results)[0]).toMatchObject({ x: 20, width: 140, available: true });
    expect(evaluateGraphics(section, results)[0].notes).toEqual(['总价超出映射范围，图形使用边界值']);
    expect(evaluateGraphics(section, evaluateModel(section, { quantity: 0 }))[0].x).toBe(180);
  });

  it('hides dependent shapes on division by zero but keeps unrelated shapes and recovers', () => {
    const section = { ...graphical, outputs: [{ ...model.outputs[0], operation: 'ratio' as const, args: [10, 'quantity'] }] };
    const invalid = evaluateGraphics(section, evaluateModel(section, { quantity: 0 }));
    expect(invalid[0].available).toBe(false);
    expect(invalid[1].available).toBe(true);
    expect(evaluateGraphics(section, evaluateModel(section, { quantity: 2 }))[0].available).toBe(true);
    const colorOnly = { ...section, graphics: [{ ...graphical.graphics![1], color: graphical.graphics![0].color }] };
    expect(evaluateGraphics(colorOnly, evaluateModel(colorOnly, { quantity: 0 }))[0].available).toBe(false);
  });

  it('leaves older sections without shapes compatible', () => {
    expect(evaluateGraphics(model, evaluateModel(model, modelDefaults(model)))).toEqual([]);
  });
});
