import { describe, expect, it } from 'vitest';

import { buildExpandablePreview } from './expandablePreview';

describe('expandable preview', () => {
  it('keeps short content unchanged', () => {
    expect(buildExpandablePreview('  完整内容  ', 10)).toEqual({
      fullText: '完整内容',
      previewText: '完整内容',
      truncated: false,
    });
  });

  it('marks truncated content with an ellipsis while retaining the full text', () => {
    expect(buildExpandablePreview('步骤一\n步骤二\n步骤三', 7)).toEqual({
      fullText: '步骤一\n步骤二\n步骤三',
      previewText: '步骤一\n步骤二…',
      truncated: true,
    });
  });

  it('does not split a unicode code point at the preview boundary', () => {
    expect(buildExpandablePreview('A🛠B', 2).previewText).toBe('A🛠…');
  });
});
