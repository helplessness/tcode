import { describe, expect, it } from 'vitest';
import {
  autoEmbedStandaloneImagePaths,
  getInlineFileUrl,
  isImageFileRef,
  normalizeInlineFilePath,
  toInlineImageMarkdown,
} from './inlineFiles';

describe('inline workspace images', () => {
  it('maps the sandbox workspace root to a browser-visible workspace path', () => {
    expect(normalizeInlineFilePath('/workspace/docs/figure.png')).toBe('docs/figure.png');
    expect(getInlineFileUrl('/workspace/docs/figure.png', 'research')).toBe(
      '/api/files/inline?path=docs%2Ffigure.png&workspace_id=research',
    );
  });

  it('recognizes local images but not remote images', () => {
    expect(isImageFileRef('artifacts/plot.webp')).toBe(true);
    expect(isImageFileRef('https://example.com/plot.webp')).toBe(false);
  });

  it('embeds a standalone image path without changing code fences', () => {
    const content = [
      '结果如下：',
      '/workspace/artifacts/plot.png',
      '```text',
      '/workspace/artifacts/debug.png',
      '```',
    ].join('\n');

    const rendered = autoEmbedStandaloneImagePaths(content);

    expect(rendered).toContain('![plot.png](/workspace/artifacts/plot.png)');
    expect(rendered).toContain('```text\n/workspace/artifacts/debug.png\n```');
  });

  it('does not mistake a prose sentence ending in an image name for a complete path', () => {
    const content = '文件已验证：page_2_figure_1.png';
    expect(autoEmbedStandaloneImagePaths(content)).toBe(content);
  });

  it('builds safe Markdown for an uploaded image path', () => {
    expect(toInlineImageMarkdown('/workspace/upload/my plot.png', 'my [plot].png')).toBe(
      '![my \\[plot\\].png](</workspace/upload/my plot.png>)',
    );
    expect(toInlineImageMarkdown('/workspace/upload/report.pdf', 'report')).toBe('');
  });
});
