export type HighlightSegment = {
  text: string;
  highlighted: boolean;
};

export const fuzzySearchTokens = (query: unknown) => (
  String(query || '')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
);

export const fuzzyMatchIndexes = (value: unknown, query: unknown): number[] | null => {
  const source = Array.from(String(value || ''));
  const needle = Array.from(String(query || '').trim());
  if (needle.length === 0) return [];
  if (source.length === 0 || needle.length > source.length) return null;

  const normalizedSource = source.map((character) => character.toLocaleLowerCase());
  const normalizedNeedle = needle.map((character) => character.toLocaleLowerCase());

  for (let start = 0; start <= normalizedSource.length - normalizedNeedle.length; start += 1) {
    if (normalizedNeedle.every((character, offset) => normalizedSource[start + offset] === character)) {
      return normalizedNeedle.map((_, offset) => start + offset);
    }
  }

  const indexes: number[] = [];
  let sourceIndex = 0;
  for (const character of normalizedNeedle) {
    while (sourceIndex < normalizedSource.length && normalizedSource[sourceIndex] !== character) {
      sourceIndex += 1;
    }
    if (sourceIndex >= normalizedSource.length) return null;
    indexes.push(sourceIndex);
    sourceIndex += 1;
  }
  return indexes;
};

export const fuzzyMatchesFields = (fields: unknown[], query: unknown) => {
  const tokens = fuzzySearchTokens(query);
  if (tokens.length === 0) return true;
  return tokens.every((token) => fields.some((field) => fuzzyMatchIndexes(field, token) !== null));
};

export const fuzzyHighlightSegments = (value: unknown, query: unknown): HighlightSegment[] => {
  const source = Array.from(String(value || ''));
  if (source.length === 0) return [];
  const highlightedIndexes = new Set<number>();
  for (const token of fuzzySearchTokens(query)) {
    for (const index of fuzzyMatchIndexes(source.join(''), token) || []) {
      highlightedIndexes.add(index);
    }
  }

  const segments: HighlightSegment[] = [];
  source.forEach((character, index) => {
    const highlighted = highlightedIndexes.has(index);
    const previous = segments[segments.length - 1];
    if (previous?.highlighted === highlighted) {
      previous.text += character;
    } else {
      segments.push({ text: character, highlighted });
    }
  });
  return segments;
};
