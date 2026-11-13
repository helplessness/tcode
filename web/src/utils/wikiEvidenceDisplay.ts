import type { WikiClaim } from '../api/paperWiki';

const HEADING_PATTERN = /^(#{1,6})\s+(.+?)\s*$/;
const LIST_ITEM_PATTERN = /^(\s*[-*+]\s+)(.*?)(\s*)$/;
const EVIDENCE_MARKER_PATTERN = /\s*\[evidence:[A-Za-z0-9_.:-]+\]/g;

export const WIKI_CLAIM_EVIDENCE_PREFIX = 'wiki-claim:';

const normalizedClaimText = (value: unknown) => (
  String(value || '').replace(EVIDENCE_MARKER_PATTERN, '').trim()
);

/**
 * Add display-only claim markers to the machine-authored comprehensive conclusion.
 *
 * Wiki storage intentionally keeps Evidence IDs out of editable Markdown. These
 * synthetic markers exist only in the rendered copy and are intercepted by the
 * Paper Wiki UI before they can reach the generic Evidence endpoint.
 */
export const buildWikiAgentDisplayContent = (
  content: unknown,
  claims: WikiClaim[] | undefined,
) => {
  const source = String(content || '');
  const claimByStatement = new Map(
    (claims || [])
      .filter((claim) => String(claim.claim_id || '').trim() && (claim.evidence_ids || []).length > 0)
      .map((claim) => [normalizedClaimText(claim.statement), claim]),
  );
  if (!source || claimByStatement.size === 0) return source;

  let inComprehensiveConclusion = false;
  return source
    .split('\n')
    .map((line) => {
      const heading = HEADING_PATTERN.exec(line.trim());
      if (heading) {
        const level = heading[1].length;
        const title = heading[2].trim();
        if (level <= 2) {
          inComprehensiveConclusion = level === 2 && title === '综合结论';
        }
        return line;
      }
      if (!inComprehensiveConclusion) return line;

      const listItem = LIST_ITEM_PATTERN.exec(line);
      if (!listItem) return line;
      const statement = normalizedClaimText(listItem[2]);
      const claim = claimByStatement.get(statement);
      if (!claim || listItem[2].includes('[evidence:')) return line;

      const marker = `[evidence:${WIKI_CLAIM_EVIDENCE_PREFIX}${claim.claim_id}]`;
      return `${listItem[1]}${listItem[2].trimEnd()} ${marker}${listItem[3]}`;
    })
    .join('\n');
};

