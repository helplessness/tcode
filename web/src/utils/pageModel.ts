import type { ModelOutput, ModelParameter, ModelSection } from '../api/intelligentPage';

// Display interpolation without binary floating-point tails; calculations and saved values stay unrounded.
export const modelNumber = (value: number) => value !== 0 && Math.abs(value) < 1e-6 ? String(value) : value.toLocaleString('zh-CN', { maximumSignificantDigits: 12 });
export const modelDefaults = (section: ModelSection) => Object.fromEntries(section.parameters.map(p => [p.id, p.value]));

export function parameterValue(parameter: ModelParameter, raw: string): number | null {
  if (!raw.trim()) return null;
  const value = Number(raw);
  return Number.isFinite(value) && value >= parameter.min && value <= parameter.max ? value : null;
}

// Closed arithmetic set: no expressions, generated scripts, cross-output dependencies or eval.
export function evaluateModel(section: ModelSection, values: Record<string, number>) {
  return section.outputs.map(output => {
    const args = output.args.map(a => typeof a === 'number' ? a : values[a]);
    const valid = section.parameters.every(p => Number.isFinite(values[p.id]) && values[p.id] >= p.min && values[p.id] <= p.max);
    const [a, b, c] = args;
    let result = NaN;
    if (valid && args.every(Number.isFinite)) {
      switch (output.operation) {
        case 'sum': result = a + b; break;
        case 'difference': result = a - b; break;
        case 'product': result = a * b; break;
        case 'ratio': result = b === 0 ? NaN : a / b; break;
        case 'linear': result = a * b + c; break;
      }
    }
    const available = Number.isFinite(result) && Math.abs(result) <= 1e12;
    const state = available && output.threshold ? (result < output.threshold.value ? output.threshold.below : output.threshold.at_or_above) : null;
    const symbols: Record<ModelOutput['operation'], string> = { sum: '+', difference: '−', product: '×', ratio: '÷', linear: '×' };
    const terms = output.args.map(arg => typeof arg === 'number' ? modelNumber(arg) : `${section.parameters.find(p => p.id === arg)?.label}（${modelNumber(values[arg])}）`);
    const equation = `${terms[0]} ${symbols[output.operation]} ${terms[1]}${output.operation === 'linear' ? ` + ${terms[2]}` : ''}`;
    return {
      output, value: available ? result : null, state, equation,
      position: available ? Math.max(0, Math.min(100, (result - output.min) / (output.max - output.min) * 100)) : 0,
      outside: available && (result < output.min || result > output.max),
      error: available ? '' : output.operation === 'ratio' && b === 0 ? '除数为零，请调整参数。' : '结果无法计算或超出支持范围，请调整参数。',
    };
  });
}

export function evaluateGraphics(section: ModelSection, results: ReturnType<typeof evaluateModel>) {
  return (section.graphics ?? []).map((graphic, index) => {
    let available = true;
    const notes: string[] = [];
    const coordinate = (key: 'x' | 'y' | 'width' | 'height') => {
      const binding = graphic[key];
      if (typeof binding === 'number') return binding;
      const result = results[binding.output_index];
      if (!result || result.value === null) { available = false; return 0; }
      if (result.outside) notes.push(`${result.output.label}超出映射范围，图形使用边界值`);
      return binding.min + result.position / 100 * (binding.max - binding.min);
    };
    const x = coordinate('x'), y = coordinate('y'), width = coordinate('width'), height = coordinate('height');
    let color = typeof graphic.color === 'string' ? graphic.color : graphic.color.below;
    let state = '';
    if (typeof graphic.color !== 'string') {
      const result = results[graphic.color.output_index];
      if (!result || result.value === null || !result.output.threshold) available = false;
      else {
        color = result.value < result.output.threshold.value ? graphic.color.below : graphic.color.at_or_above;
        state = result.state?.label ?? '';
      }
    }
    for (const binding of graphic.bindings ?? []) {
      const result = results[binding.output_index];
      if (!result || result.value === null) notes.push('部位关联结果暂不可计算，相关部位暂不显示');
      else if (result.outside) notes.push(`${result.output.label}超出映射范围，图形使用边界值`);
      if (result?.state && (binding.attribute === 'fill' || binding.attribute === 'stroke')) notes.push(`部位颜色状态：${result.state.label}`);
    }
    return { index, label: graphic.label, shape: graphic.shape, x, y, width, height, color, state, available, notes: [...new Set(notes)] };
  });
}
