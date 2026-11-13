import { renderToString } from 'solid-js/web';
import { expect, it } from 'vitest';
import FileUpload from './FileUpload';

it('keeps main uploads compatible and offers only files for a private side chat', () => {
  expect(renderToString(() => <FileUpload/>)).toContain('选择文件夹');
  const side = renderToString(() => <FileUpload sessionId="side-upload" allowFolders={false}/>);
  expect(side).toContain('浏览选择');
  expect(side).not.toContain('选择文件夹');
});
