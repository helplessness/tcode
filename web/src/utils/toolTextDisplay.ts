export type ToolTextDisplayStats = {
  lineCount: number;
  charCount: number;
  isLong: boolean;
  label: string;
};

const DEFAULT_LONG_LINE_THRESHOLD = 24;
const DEFAULT_LONG_CHAR_THRESHOLD = 2400;

function formatCount(value: number): string {
  return Math.max(0, Math.round(value)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

export function getToolTextDisplayStats(
  value: unknown,
  options: {
    longLineThreshold?: number;
    longCharThreshold?: number;
  } = {},
): ToolTextDisplayStats {
  const text = String(value ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const lineCount = text ? text.split('\n').length : 0;
  const charCount = Array.from(text).length;
  const longLineThreshold = options.longLineThreshold ?? DEFAULT_LONG_LINE_THRESHOLD;
  const longCharThreshold = options.longCharThreshold ?? DEFAULT_LONG_CHAR_THRESHOLD;

  return {
    lineCount,
    charCount,
    isLong: lineCount > longLineThreshold || charCount > longCharThreshold,
    label: `${formatCount(lineCount)} 行 / ${formatCount(charCount)} 字符`,
  };
}
