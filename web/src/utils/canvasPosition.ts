// Same restricted translate subset as the platform validator. Offsets are SVG units.
export function canvasPosition(transform = ''): [number, number] | null {
  if (!transform) return [0, 0];
  const match = /^translate\(\s*([-+0-9.eE]+)(?:[ ,]+([-+0-9.eE]+))?\s*\)$/.exec(transform);
  if (!match) return null;
  const values: [number, number] = [Number(match[1]), Number(match[2] || 0)];
  return values.every(value => Number.isFinite(value) && Math.abs(value) <= 100_000) ? values : null;
}

export function canvasTranslation(x: string, y: string): string | null {
  if (!x.trim() || !y.trim()) return null;
  const values = [Number(x), Number(y)];
  return values.every(value => Number.isFinite(value) && Math.abs(value) <= 100_000)
    ? `translate(${values[0]} ${values[1]})` : null;
}
