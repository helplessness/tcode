import { describe, expect, it } from 'vitest';

import {
  sanitizeVisibleMessages,
  serializeSessionsForStorage,
  toSessionStorageSummary,
  type Session,
} from './session';

describe('session local storage', () => {
  it('restores delivery acceptance independently of failed verification', () => {
    const [message] = sanitizeVisibleMessages([{id:'accepted',role:'assistant',content:'结果',verification:{
      advisory:true,delivery_state:'accepted',verification_status:'PARTIAL',confidence:.23,
      findings:[{status:'failed',title:'报告完整性',reason:'报告缺少必要内容'}],
    }}]);
    expect(message.verification).toMatchObject({delivery_state:'accepted',verification_status:'PARTIAL',confidence:.23,
      findings:[{status:'failed',title:'报告完整性',reason:'报告缺少必要内容'}]});
  });
  it('keeps advisory confidence and issues without restoring acceptance controls', () => {
    const [message] = sanitizeVisibleMessages([{id:'score',role:'assistant',content:'已交付',verification:{
      verification_status:'PARTIAL',confidence:.3,advisory:true,verification_mode:'strict',
      needs_user_confirmation:true,confirmation_state:'pending',issues:['证据不足'],
    }}]);
    expect(message.verification).toMatchObject({advisory:true,verification_mode:'strict',needs_user_confirmation:false,issues:['证据不足']});
    expect(message.verification?.confirmation_state).toBeUndefined();
  });
  it('keeps failed and unverified criteria distinct across reloads', () => {
    const messages = sanitizeVisibleMessages([{id:'review',role:'assistant',content:'result',verification:{
      verification_status:'PARTIAL',confidence:.5,criteria_total:4,criteria_passed:1,
      criteria_failed:1,criteria_unverified:2,
    }}]);
    expect(messages[0].verification).toMatchObject({criteria_failed:1,criteria_unverified:2});
  });
  it('restores only the public message verification projection', () => {
    const messages = sanitizeVisibleMessages([
      {
        id: 'assistant-1',
        role: 'assistant',
        content: 'done',
        verification: {
          verification_status: 'VERIFIED',
          confidence: 0.91,
          criteria_passed: 7,
          criteria_total: 3,
          user_confirmed: true,
          contract_id: 'internal-contract-id',
        },
      },
    ]);

    expect(messages[0].verification).toEqual({
      verification_status: 'VERIFIED',
      confidence: 0.91,
      criteria_passed: 3,
      criteria_total: 3,
      user_confirmed: true,
    });
  });

  it('persists session summaries without message or tool output history', () => {
    const session = {
      session_id: 'session-1',
      title: 'A session',
      created_at: 1,
      updated_at: 2,
      messages: [
        {
          id: 'message-1',
          role: 'assistant',
          content: 'large response',
          tool_calls: [
            {
              tool_name: 'bash',
              arguments: {},
              output: 'x'.repeat(100_000),
            },
          ],
        },
      ],
    } as Session;

    expect(toSessionStorageSummary(session)).toMatchObject({
      session_id: 'session-1',
      title: 'A session',
      messages: [],
    });

    const serialized = serializeSessionsForStorage([session]);
    expect(serialized.length).toBeLessThan(500);
    expect(serialized).not.toContain('large response');
    expect(serialized).not.toContain('100000');
  });

  it('keeps only the public outcome confirmation state', () => {
    const messages = sanitizeVisibleMessages([
      {
        id: 'assistant-confirmation',
        role: 'assistant',
        content: 'result',
        verification: {
          verification_status: 'PARTIAL',
          confidence: 0.64,
          needs_user_confirmation: true,
          confirmation_state: 'pending',
          confirmation_request_id: 'internal-request-id',
          confirmation_guidance: { guidance_id: 'internal-guidance-id' },
        },
      },
    ]);

    expect(messages[0].verification).toEqual({
      verification_status: 'PARTIAL',
      confidence: 0.64,
      needs_user_confirmation: true,
      confirmation_state: 'pending',
    });
  });
});
