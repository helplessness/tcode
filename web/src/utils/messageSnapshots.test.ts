import { describe, expect, it } from 'vitest';
import type { Message } from '../stores/session';
import {
  mergeAndStabilizeVisibleMessages,
  shouldApplyHydratedSession,
  shouldApplySessionSelection,
  toVisibleMessages,
} from './messageSnapshots';

describe('messageSnapshots', () => {
  it('refreshes a goal reply when delayed verification arrives without changing ordinary replies', () => {
    const final: Message = {id:'goal-final',role:'assistant',content:'研究结果',
      verification:{verification_status:'UNVERIFIED',confidence:0.5}};
    const ordinary: Message = {id:'ordinary',role:'assistant',content:'普通回答',
      verification:{verification_status:'VERIFIED',confidence:0.8}};
    const current = [final, ordinary];
    const next = mergeAndStabilizeVisibleMessages(current, [{...final,
      verification:{verification_status:'VERIFIED',confidence:0.96,criteria_total:2,criteria_passed:2}},ordinary]);
    expect(next[0].verification).toMatchObject({verification_status:'VERIFIED',confidence:0.96,criteria_passed:2});
    expect(next[1]).toBe(ordinary);
    expect(final.verification?.confidence).toBe(0.5);
  });
  it('keeps a recovered old async result before newer user and assistant messages', () => {
    const first: Message = {id:'old',role:'user',content:'旧任务'};
    const receipt: Message = {id:'receipt',role:'assistant',presentation:'async_completion',content:'[Symphony:LOCAL-1] 完成'};
    const question: Message = {id:'new-user',role:'user',content:'新问题'};
    const answer: Message = {id:'new-answer',role:'assistant',content:'最新回复'};
    for (const current of [[first,receipt],[first,receipt,question,answer]]) {
      const merged = mergeAndStabilizeVisibleMessages(current,[first,question,answer]);
      expect(merged.map(item=>item.id)).toEqual(['old','receipt','new-user','new-answer']);
      expect(mergeAndStabilizeVisibleMessages(merged,[first,question,answer])).toBe(merged);
    }
  });

  it('rejects late session responses after A to B to A switching', () => {
    expect(shouldApplySessionSelection('session-a', 'session-a', 1, 3)).toBe(false);
    expect(shouldApplySessionSelection('session-a', 'session-b', 2, 3)).toBe(false);
    expect(shouldApplySessionSelection('session-a', 'session-a', 3, 3)).toBe(true);
  });
  it('does not apply a stale hydration response after the user switches sessions', () => {
    expect(shouldApplyHydratedSession('session-b', 'session-a')).toBe(false);
    expect(shouldApplyHydratedSession('session-a', 'session-a')).toBe(true);
    expect(shouldApplyHydratedSession(null, 'session-a')).toBe(true);
  });

  it('preserves the async final presentation marker from server snapshots', () => {
    const messages = toVisibleMessages([
      {
        id: 'a1',
        role: 'assistant',
        content: 'Round1 完成',
        presentation: 'async_final',
      },
    ]);

    expect(messages[0].presentation).toBe('async_final');
  });

  it('preserves applied guidance metadata for the compact conversation status', () => {
    const messages = toVisibleMessages([
      {
        id: 'g1',
        role: 'user',
        content: '改为只输出新结果',
        metadata: {
          guidance: {
            id: 'guidance-1',
            mode: 'steer',
            applied_safe_point: 'after_model_call',
          },
        },
      },
    ]);

    expect(messages[0].metadata?.guidance).toMatchObject({
      id: 'guidance-1',
      mode: 'steer',
      applied_safe_point: 'after_model_call',
    });
  });

  it('preserves the public verification score and refreshes its components', () => {
    const messages = toVisibleMessages([
      {
        id: 'a-score',
        role: 'assistant',
        content: 'NEW_R5',
        verification: {
          verification_status: 'VERIFIED',
          confidence: 0.94,
          confidence_components: {
            objective_checks: 1,
            evidence_coverage: 1,
            independent_validator: 0.75,
            agreement: 1,
          },
          required_proof_level: 'P1',
          achieved_proof_level: 'P1',
          criteria_passed: 2,
          criteria_total: 3,
          contract_id: 'private-contract',
        },
      },
    ]);

    expect(messages[0].verification).toEqual({
      verification_status: 'VERIFIED',
      confidence: 0.94,
      confidence_components: {
        objective_checks: 1,
        evidence_coverage: 1,
        independent_validator: 0.75,
        agreement: 1,
      },
      required_proof_level: 'P1',
      achieved_proof_level: 'P1',
      criteria_passed: 2,
      criteria_total: 3,
    });
  });

  it('refreshes a guidance message when its application metadata changes', () => {
    const current: Message[] = [{
      id: 'g1',
      role: 'user',
      content: '改为只输出新结果',
      metadata: {guidance: {id: 'guidance-1', mode: 'steer'}},
    }];

    const next = mergeAndStabilizeVisibleMessages(current, [{
      id: 'g1',
      role: 'user',
      content: '改为只输出新结果',
      metadata: {
        guidance: {
          id: 'guidance-1',
          mode: 'steer',
          applied_safe_point: 'after_model_call',
        },
      },
    }]);

    expect(next).not.toBe(current);
    expect(next[0].metadata?.guidance?.applied_safe_point).toBe('after_model_call');
  });

  it('returns the same array when the upstream snapshot is unchanged', () => {
    const current: Message[] = [
      { id: 'u1', role: 'user', content: 'hello' },
      { id: 'a1', role: 'assistant', content: 'hi' },
    ];

    const next = mergeAndStabilizeVisibleMessages(current, [
      { id: 'u1', role: 'user', content: 'hello' },
      { id: 'a1', role: 'assistant', content: 'hi' },
    ]);

    expect(next).toBe(current);
  });

  it('preserves unchanged message object identity when appending a new message', () => {
    const first: Message = { id: 'u1', role: 'user', content: 'hello' };
    const second: Message = { id: 'a1', role: 'assistant', content: 'hi' };
    const current: Message[] = [first, second];

    const next = mergeAndStabilizeVisibleMessages(current, [
      { id: 'u1', role: 'user', content: 'hello' },
      { id: 'a1', role: 'assistant', content: 'hi' },
      { id: 's1', role: 'system', content: 'workflow run created' },
    ]);

    expect(next).not.toBe(current);
    expect(next[0]).toBe(first);
    expect(next[1]).toBe(second);
    expect(next[2]).toMatchObject({ id: 's1', role: 'system' });
  });

  it('preserves local process notes when a completed server snapshot omits them', () => {
    const current: Message[] = [
      { id: 'u1', role: 'user', content: '分析' },
      {
        id: 'a1',
        role: 'assistant',
        content: '完成',
        process_notes: [
          {
            id: 'note-1',
            phase: 'model_wait',
            message: '可用工具已经整理好，我来判断下一步',
            timestamp: 1,
          },
        ],
      },
    ];

    const next = mergeAndStabilizeVisibleMessages(current, [
      { id: 'u1', role: 'user', content: '分析' },
      {
        id: 'a1',
        role: 'assistant',
        content: '完成',
        tool_calls: [
          {
            tool_name: 'read',
            arguments: { path: 'paper.pdf' },
          },
        ],
      },
    ]);

    expect(next).not.toBe(current);
    expect(next[1].tool_calls?.[0].tool_name).toBe('read');
    expect(next[1].process_notes?.[0].phase).toBe('model_wait');
  });

  it('keeps streamed process notes when the final server snapshot uses new message ids', () => {
    const current: Message[] = [
      { id: 'local-u1', role: 'user', content: '分析' },
      {
        id: 'local-a1',
        role: 'assistant',
        content: '',
        process_notes: [
          {
            id: 'note-1',
            phase: 'tool_start',
            message: '我先读取 `paper.pdf` 这一段，确认里面的关键信息。',
            timestamp: 1,
          },
        ],
      },
    ];

    const next = mergeAndStabilizeVisibleMessages(current, [
      { id: 'server-u1', role: 'user', content: '分析' },
      {
        id: 'server-a1',
        role: 'assistant',
        content: '完成',
        tool_calls: [
          {
            tool_name: 'read',
            arguments: { path: 'paper.pdf' },
          },
        ],
      },
    ]);

    expect(next).toHaveLength(2);
    expect(next[1].id).toBe('server-a1');
    expect(next[1].tool_calls?.[0].tool_name).toBe('read');
    expect(next[1].process_notes?.[0].message).toContain('paper.pdf');
  });

  it('does not move TCode process notes onto an external Harness reply', () => {
    const current: Message[] = [
      { id: 'native-u', role: 'user', content: '先做原生回合' },
      {
        id: 'native-local-a',
        role: 'assistant',
        content: '',
        metadata: { source_runtime_id: 'tcode-native' },
        process_notes: [{ id: 'native-note', phase: 'model_wait', message: 'TCode 正在思考', timestamp: 1 }],
      },
    ];
    const next = mergeAndStabilizeVisibleMessages(current, [
      { id: 'native-u', role: 'user', content: '先做原生回合' },
      { id: 'external-u', role: 'user', content: '切到 OpenCode', metadata: { source_runtime_id: 'opencode' } },
      { id: 'external-a', role: 'assistant', content: '完成', metadata: { source_runtime_id: 'opencode' } },
    ]);

    expect(next.find(message => message.id === 'external-a')?.process_notes).toBeUndefined();
  });

  it('preserves structured image blocks when a later snapshot omits them', () => {
    const current: Message[] = [
      { id: 'u1', role: 'user', content: '展示图片' },
      {
        id: 'a1',
        role: 'assistant',
        content: '见下图',
        content_blocks: [{
          type: 'image',
          asset_id: 'img_12345678',
          session_id: 'session-1',
          path: 'artifacts/figure.png',
          mime_type: 'image/png',
          size: 128,
          alt: 'Figure 1',
          caption: '论文图 1',
        }],
      },
    ];

    const next = mergeAndStabilizeVisibleMessages(current, [
      { id: 'u1', role: 'user', content: '展示图片' },
      { id: 'a1', role: 'assistant', content: '见下图' },
    ]);

    const block = next[1].content_blocks?.[0];
    expect(block?.type).toBe('image');
    expect(block?.type === 'image' ? block.asset_id : '').toBe('img_12345678');
  });

  it('keeps a consumed async completion result and its structured blocks', () => {
    const identifier = 'LOCAL-20260811091611002410';
    const completion: Message = {
      id: `msg-async-continuation-${identifier}`,
      role: 'assistant',
      presentation: 'async_completion',
      content: `[Symphony:${identifier}]\ngnina-mcp.gnina_mcp_server 异步任务已完成。`,
      content_blocks: [{
        type: 'data_table',
        schema: 'tcode.table.v1',
        id: 'ranking',
        title: 'Pose 排名',
        columns: [{ key: 'rank', title: '排名', type: 'integer' }],
        rows: [{ rank: 1 }],
        total_rows: 1,
        primary_key: 'rank',
        fallback: '排名 1',
      }],
    };
    const continuation: Message = {
      id: 'continue-user',
      role: 'user',
      content: `继续异步任务 ${identifier}。`,
    };

    const next = mergeAndStabilizeVisibleMessages(
      [completion, continuation],
      [continuation],
    );

    expect(next).toHaveLength(2);
    const preserved = next.find(message => message.id === completion.id);
    expect(preserved?.content_blocks?.[0].type).toBe('data_table');
    expect(next.some(message => message.id === 'continue-user')).toBe(true);
  });

  it('keeps an unconsumed async completion card available', () => {
    const identifier = 'LOCAL-PENDING';
    const completion: Message = {
      id: `msg-async-continuation-${identifier}`,
      role: 'assistant',
      presentation: 'async_completion',
      content: `[Symphony:${identifier}]\ngeneric-mcp.long_task 异步任务已完成。`,
    };

    const next = mergeAndStabilizeVisibleMessages([completion], []);

    expect(next).toHaveLength(1);
    expect(next[0].id).toBe(completion.id);
  });
});
