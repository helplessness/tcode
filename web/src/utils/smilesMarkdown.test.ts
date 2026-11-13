import { describe, expect, it } from 'vitest';
import { marked } from 'marked';
import { isLiteralSmilesLink } from './smilesMarkdown';

const render = (text: string) => {
  const renderer = new marked.Renderer();
  const original = renderer.link;
  renderer.link = function (token) {
    return isLiteralSmilesLink(token) ? token.raw : original.call(this, token);
  };
  return String(marked.parse(text, { renderer }));
};

describe('literal SMILES bracket atoms', () => {
  it('preserves chirality and branch notation without turning them into a link', () => {
    const smiles = 'O=C[C@H](O)COP(=O)(O)O';
    const html = render(`反应物 SMILES：${smiles}`);
    expect(html).toContain(smiles);
    expect(html).not.toContain('<a ');
  });

  it('preserves isotope, charge and nested branches', () => {
    for (const smiles of ['C[13C@@H](O)N', 'C[NH+](CC)C', 'C[C@H](C(=O)O)N']) {
      expect(render(smiles)).toContain(smiles);
      expect(render(smiles)).not.toContain('<a ');
    }
  });

  it('keeps normal links, images, code and explicitly titled links', () => {
    for (const text of ['[文档](https://example.com)', '[C](guide.md)', '[C](https://example.com)', '[C](O "链接")']) {
      expect(render(text)).toContain('<a ');
    }
    expect(render('![C](image.png)')).toContain('<img ');
    expect(render('`O=C[C@H](O)C`')).toContain('<code>O=C[C@H](O)C</code>');
  });
});
