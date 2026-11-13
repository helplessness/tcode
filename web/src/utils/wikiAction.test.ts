import { describe, expect, it } from 'vitest';

import { wikiActionFromTool, wikiActionOperationLabel } from './wikiAction';


describe('wikiActionFromTool', () => {
  it('accepts the versioned action emitted by the existing stage tool', () => {
    const action = wikiActionFromTool({
      tool_name: 'paper_wiki_stage_changes',
      arguments: {},
      metadata: {
        wiki_action: {
          schema: 'tcode.wiki-action.v1',
          kind: 'proposal_review',
          status: 'ready',
          title: '归档 Wiki 页面：A107N',
          proposal_id: 'proposal-1',
          proposal_hash: 'sha256:reviewed',
          proposal_status: 'pending_review',
          operations: [{
            type: 'archive_page',
            page_id: 'wiki-a107n',
            title: 'A107N',
          }],
          direct_confirm_allowed: true,
        },
      },
    });

    expect(action?.proposal_id).toBe('proposal-1');
    expect(action?.operations[0].page_id).toBe('wiki-a107n');
    expect(action?.direct_confirm_allowed).toBe(true);
  });

  it('rejects unrelated tools and incomplete envelopes', () => {
    expect(wikiActionFromTool({
      tool_name: 'paper_wiki_search',
      arguments: {},
      metadata: {},
    })).toBeNull();
    expect(wikiActionFromTool({
      tool_name: 'paper_wiki_stage_changes',
      arguments: {},
      metadata: {
        wiki_action: {
          schema: 'tcode.wiki-action.v0',
          kind: 'proposal_review',
        },
      },
    })).toBeNull();
  });

  it('uses readable action labels', () => {
    expect(wikiActionOperationLabel('archive_page')).toBe('归档');
    expect(wikiActionOperationLabel('restore_page')).toBe('恢复');
  });
});

