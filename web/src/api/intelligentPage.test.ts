import { describe, expect, it, vi } from 'vitest';
import { chartScale, pageRequest } from './intelligentPage';
import { sanitizeMessageContentBlocks } from '../utils/contentBlocks';

describe('interactive page boundaries', () => {
  it('keeps trash filters and owner session in the same query string', async () => {
    const fetcher=vi.fn().mockResolvedValue({ok:true,json:async()=>({items:[]})});
    vi.stubGlobal('fetch',fetcher);
    try {
      await pageRequest('page_a','chat','','', '/revisions?deleted=true&before=rev_a');
      const url=String(fetcher.mock.calls[0][0]);
      expect(url.split('?')).toHaveLength(2);
      const query=new URL(url,'https://local.invalid').searchParams;
      expect(query.get('deleted')).toBe('true');expect(query.get('session_id')).toBe('chat');expect(query.get('before')).toBe('rev_a');
    } finally {vi.unstubAllGlobals();}
  });
  it('scales negative, zero and equal points around a real zero baseline', () => {
    const scale = chartScale([-5, 0, 5]);
    expect(scale.position(-5)).toBe(0);
    expect(scale.position(0)).toBe(50);
    expect(scale.position(5)).toBe(100);
    expect(chartScale([0, 0]).position(0)).toBe(0);
    expect(chartScale([-3, -3]).position(-3)).toBe(0);
    expect(chartScale([-3, -3]).position(0)).toBe(100);
  });
  it('keeps deduplicated page receipts and rejects forged identifiers', () => {
    const block = { type: 'intelligent_page', schema: 'tcode.intelligent-page.v1', page_id: `page_${'a'.repeat(32)}`, revision_id: `rev_${'b'.repeat(32)}`, session_id: 'chat', title: '页面' };
    expect(sanitizeMessageContentBlocks([block, block])).toHaveLength(1);
    expect(sanitizeMessageContentBlocks([{ ...block, page_id: '../../secret' }])).toEqual([]);
  });
});
