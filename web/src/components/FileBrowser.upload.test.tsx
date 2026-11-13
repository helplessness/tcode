import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';
import FileBrowser from './FileBrowser';

describe('asset upload entry', () => {
  it('offers a multiple-file picker scoped to the selected workspace', () => {
    const html = renderToString(() => <FileBrowser workspaceId="test1" />);
    expect(html).toContain('上传文件');
    expect(html).not.toContain('任务改动');
    expect(html).toContain('aria-label="选择资产文件"');
    expect(html).toContain('multiple');
    expect(html).toContain('test1/upload/');
  });
  it('keeps job artifacts read-only and missing workspace uploads disabled', () => {
    const artifact = renderToString(() => <FileBrowser workspaceId="test1" jobId="job1" artifactRoot="outputs" />);
    expect(artifact).toContain('任务产物只读');
    expect(artifact).not.toContain('type="file"');
    expect(artifact).not.toContain('任务改动');
    const empty = renderToString(() => <FileBrowser />);
    expect(empty).toContain('disabled');
    expect(empty).toContain('选择工作区后');
  });
});
