export const DEFAULT_EXPANDABLE_PREVIEW_LIMIT = 260;

export type ExpandablePreview = {
  fullText: string;
  previewText: string;
  truncated: boolean;
};

export const buildExpandablePreview = (
  value: unknown,
  limit = DEFAULT_EXPANDABLE_PREVIEW_LIMIT,
): ExpandablePreview => {
  const fullText = String(value || '').trim();
  const characters = Array.from(fullText);
  const normalizedLimit = Math.max(1, Math.floor(limit));
  if (characters.length <= normalizedLimit) {
    return { fullText, previewText: fullText, truncated: false };
  }
  return {
    fullText,
    previewText: `${characters.slice(0, normalizedLimit).join('').trimEnd()}…`,
    truncated: true,
  };
};
