import { describe, expect, it } from 'vitest';

import type { Message } from '../stores/session';
import { chatRunDirectory, markRecoveredWorkflowMessages, presentChatTurns } from './chatPresentation';

const message = (id: string, role: Message['role'], content: string): Message => ({
  id,
  role,
  content,
});

describe('chat presentation', () => {
  it('indexes instructions once and keeps terminal filters scoped to the turn', () => {
    const turns = presentChatTurns([
      {...message('first', 'user', '比对\n结构'), created_at: 1794355200},
      {...message('retry', 'assistant', ''), tool_calls: [{ tool_name: 'read', arguments: {}, state: 'error' }]},
      message('answer', 'assistant', '复核成功'),
      {...message('notice', 'assistant', '后台结果'), presentation: 'async_completion'},
      message('second', 'user', '运行第二次'),
      message('failure', 'assistant', '发生错误: 模型不可用'),
      message('third', 'user', '运行第三次'),
      message('cancelled', 'assistant', '部分回复\n\n[已中止]'),
      message('fourth', 'user', '运行第四次'),
    ], true);
    expect(chatRunDirectory(turns, '', 'all').map(row => [row.id, row.index, row.status])).toEqual([
      ['fourth', 4, 'running'], ['third', 3, 'interrupted'], ['second', 2, 'failed'], ['first', 1, 'completed'],
    ]);
    expect(chatRunDirectory(turns, '比对 成功', 'all')[0]).toMatchObject({ id: 'first', summary: '比对 结构', timestamp: 1794355200 });
    expect(chatRunDirectory(turns, '模型', 'failed').map(row => row.id)).toEqual(['second']);
    expect(chatRunDirectory(turns, '', 'interrupted').map(row => row.id)).toEqual(['third']);
    expect(chatRunDirectory(turns, '后台结果', 'all')).toEqual([]);
  });

  it('uses recorded tool start time for older turns without message timestamps', () => {
    const turns = presentChatTurns([
      message('legacy', 'user', '历史指令'),
      {...message('tool', 'assistant', ''), tool_calls: [
        {tool_name: 'read', arguments: {}, state: 'completed', start_time: 1794355210},
        {tool_name: 'write', arguments: {}, state: 'completed', start_time: 1794355200000},
      ]},
      message('final', 'assistant', '结果'),
      message('unknown', 'user', '没有时间'),
    ], false);
    expect(chatRunDirectory(turns, '', 'all').map(row => row.timestamp)).toEqual([undefined, 1794355200000]);
  });
  it('marks a recovered historical failure without mutating stored messages', () => {
    const blocked = message('workflow-status:wf-a:blocked:report:1', 'assistant', '处理暂时已阻塞。');
    const completed = message('workflow-status:wf-a:completed:none:0', 'assistant', '报告');
    const projected = markRecoveredWorkflowMessages([blocked, completed]);
    expect(projected[0].content).toContain('历史阻塞（已恢复）');
    expect(projected[0].content).toContain('查看当时的阻塞详情');
    expect(projected[1]).toBe(completed);
    expect(blocked.content).toBe('处理暂时已阻塞。');
  });

  it('does not hide a current failure, another run, or a rerun failure', () => {
    const a = message('workflow-status:wf-a:blocked:report:1', 'assistant', '阻塞 A');
    const b = message('workflow-status:wf-b:completed:none:0', 'assistant', '完成 B');
    expect(markRecoveredWorkflowMessages([a, b])[0]).toBe(a);
    const completed = message('workflow-status:wf-a:completed:none:0', 'assistant', '完成 A');
    const failed = message('workflow-status:wf-a:blocked:report:2', 'assistant', '重跑失败');
    expect(markRecoveredWorkflowMessages([a, completed, failed])).toEqual([a, completed, failed]);
  });

  it('separates completed process messages from the final result', () => {
    const turns = presentChatTurns([
      message('u1', 'user', '分析 PDF'),
      message('s1', 'system', '正在读取文件'),
      message('t1', 'tool', '表格提取完成'),
      message('a1', 'assistant', '最终 JSON'),
    ], false);

    expect(turns).toHaveLength(1);
    expect(turns[0].processMessages.map((item) => item.id)).toEqual(['s1', 't1']);
    expect(turns[0].result?.content).toBe('最终 JSON');
    expect(turns[0].running).toBe(false);
  });

  it('keeps the current assistant output inside the live process while running', () => {
    const turns = presentChatTurns([
      message('u1', 'user', '分析 PDF'),
      message('s1', 'system', '正在读取文件'),
      message('a1', 'assistant', '正在整理表格'),
    ], true);

    expect(turns[0].result).toBeUndefined();
    expect(turns[0].processMessages.map((item) => item.id)).toEqual(['s1', 'a1']);
    expect(turns[0].running).toBe(true);
  });

  it('moves final-answer tool calls into the collapsed process group', () => {
    const final = message('a1', 'assistant', '完成');
    final.tool_calls = [{
      tool_name: 'read',
      arguments: { path: 'paper.pdf' },
    }];

    const turns = presentChatTurns([message('u1', 'user', '读取'), final], false);

    expect(turns[0].result?.tool_calls).toBeUndefined();
    expect(turns[0].processMessages).toHaveLength(1);
    expect(turns[0].processMessages[0].tool_calls?.[0].tool_name).toBe('read');
  });

  it('does not replay legacy process notes as public narration', () => {
    const final = message('a1', 'assistant', '完成');
    final.process_notes = [{
      id: 'note-1',
      phase: 'model_wait',
      message: '已准备工具，正在等待模型选择下一步',
      timestamp: 1,
    }];

    const turns = presentChatTurns([message('u1', 'user', '分析'), final], false);

    expect(turns[0].result?.process_notes).toBeUndefined();
    expect(turns[0].processMessages).toHaveLength(0);
  });

  it('keeps public commentary in the process timeline instead of selecting it as final', () => {
    const commentary = message(
      'commentary-1',
      'assistant',
      '现有信息还不足，我先读取配置确认真正的入口。',
    );
    commentary.presentation = 'commentary';

    const turns = presentChatTurns([
      message('u1', 'user', '检查配置'),
      commentary,
      message('a1', 'assistant', '已经确认入口并完成修复。'),
    ], false);

    expect(turns[0].processMessages.map((item) => item.id)).toContain('commentary-1');
    expect(turns[0].result?.id).toBe('a1');
  });

  it('merges workflow narration into the same user turn commentary timeline', () => {
    const user = message('u-workflow', 'user', '开始');
    const ordinary = message('commentary-ordinary', 'assistant', '先核对输入。');
    ordinary.presentation = 'commentary';
    const workflow = message('workflow-run:narration', 'assistant', '工作流补充了证据。');
    workflow.presentation = 'workflow_narration';
    const final = message('final-workflow', 'assistant', '已完成。');

    const turns = presentChatTurns([user, ordinary, workflow, final], false);

    expect(turns).toHaveLength(1);
    expect(turns[0].processMessages.map((item) => item.id)).toEqual([
      'commentary-ordinary',
      'workflow-run:narration',
    ]);
    expect(turns[0].result?.id).toBe('final-workflow');
  });

  it('keeps workflow narration in the active turn while the workflow is running', () => {
    const narration = message(
      'workflow-stage-progress:run-1:research:1',
      'assistant',
      '我开始处理“信息收集”（第 1/3 步）。',
    );
    narration.presentation = 'workflow_narration';

    const turns = presentChatTurns([
      message('u1', 'user', '开始运行流程'),
      message('a1', 'assistant', '已经创建运行实例。'),
      narration,
    ], true);

    expect(turns).toHaveLength(1);
    expect(turns[0].result).toBeUndefined();
    expect(turns[0].processMessages.map((item) => item.id)).toEqual(['a1', narration.id]);
    expect(turns[0].running).toBe(true);
  });

  it('keeps the explicit async final visible when a completion notice arrives later', () => {
    const asyncFinal = message(
      'async-answer',
      'assistant',
      'Round1 已完成，Run ID: run-1，Top 12 候选如下。',
    );
    asyncFinal.presentation = 'async_final';

    const completion = message(
      'msg-async-continuation-LOCAL-1',
      'assistant',
      '[Symphony:LOCAL-1]\nEvolvePro 异步任务已完成。',
    );
    completion.presentation = 'async_completion';

    const turns = presentChatTurns([
      message('u1', 'user', '运行 EvolvePro'),
      asyncFinal,
      completion,
    ], false);

    expect(turns.find((turn) => turn.result?.id === 'async-answer')?.result?.content)
      .toContain('Top 12');
    expect(turns[turns.length - 1]?.result?.id).toBe('msg-async-continuation-LOCAL-1');
  });

  it('does not let a same-session completion replace a newer user answer', () => {
    const completion = message(
      'msg-async-continuation-LOCAL-2',
      'assistant',
      '[Symphony:LOCAL-2]\n后台任务已完成。',
    );
    completion.presentation = 'async_completion';

    const turns = presentChatTurns([
      message('u1', 'user', '提交异步任务'),
      message('a1', 'assistant', '任务已受理。'),
      message('u2', 'user', '解释一下当前配置'),
      message('a2', 'assistant', '当前配置使用隔离执行环境。'),
      completion,
    ], false);

    const newerTurn = turns.find((turn) => turn.user?.id === 'u2');
    expect(newerTurn?.result?.id).toBe('a2');
    expect(newerTurn?.result?.content).toContain('隔离执行环境');
    expect(turns[turns.length - 1]?.result?.id).toBe('msg-async-continuation-LOCAL-2');
    expect(turns[turns.length - 1]?.user).toBeUndefined();
  });

  it('uses a hidden async prompt as a real turn boundary', () => {
    const hiddenPrompt = message(
      'async-prompt',
      'user',
      'Async Symphony issue finished: LOCAL-3.',
    );
    hiddenPrompt.presentation = 'async_hidden_prompt';
    const asyncFinal = message('async-final', 'assistant', '异步结果解读完成。');
    asyncFinal.presentation = 'async_final';

    const turns = presentChatTurns([
      message('u1', 'user', '新的普通问题'),
      message('a1', 'assistant', '普通问题回答。'),
      hiddenPrompt,
      asyncFinal,
    ], false);

    expect(turns.find((turn) => turn.user?.id === 'u1')?.result?.id).toBe('a1');
    expect(turns[turns.length - 1]?.result?.id).toBe('async-final');
  });

  it('uses an image-only assistant message as the final result', () => {
    const imageOnly = message('a1', 'assistant', '');
    imageOnly.content_blocks = [{
      type: 'image',
      asset_id: 'img_12345678',
      session_id: 'session-1',
      path: 'artifacts/figure.png',
      mime_type: 'image/png',
      size: 128,
      alt: 'Figure 1',
      caption: '论文图 1',
    }];

    const turns = presentChatTurns([message('u1', 'user', '展示图 1'), imageOnly], false);

    expect(turns[0].result?.id).toBe('a1');
    const block = turns[0].result?.content_blocks?.[0];
    expect(block?.type).toBe('image');
    expect(block?.type === 'image' ? block.path : '').toBe('artifacts/figure.png');
  });
});
