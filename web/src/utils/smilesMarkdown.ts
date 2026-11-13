// Markdown sees a bracket atom plus branch, e.g. [C@H](O), as a link.
// Preserve that notation as literal text; this is display disambiguation, not
// chemical validation. Ordinary links, titles and code spans remain unchanged.
const ATOM = /^(?:\d{1,3})?(?:[A-Z][a-z]?|[bcnops]|\*)(?:@{1,2})?(?:H\d*)?(?:[+-]\d*|[+-]{2,3})?(?::\d+)?$/;
const BRANCH = /^(?:[A-Z][a-z]?|[bcnops]|\[[A-Za-z0-9@+:*\-]+\]|[0-9()%=#:+*\-./\\])+$/;

export function isLiteralSmilesLink(token: {
  text: string;
  href: string;
  title?: string | null;
}): boolean {
  return !token.title && ATOM.test(token.text) && BRANCH.test(token.href);
}
