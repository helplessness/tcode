import type { Message } from '../stores/session';

export type PresentedChatTurn = {
  key: string;
  user?: Message;
  processMessages: Message[];
  result?: Message;
  running: boolean;
};

export const runDirectoryLabels = {
  completed: '已完成', running: '运行中', failed: '失败', interrupted: '已中断', incomplete: '未完成',
} as const;

export function chatRunStatus(turn: PresentedChatTurn): keyof typeof runDirectoryLabels {
  if (turn.running) return 'running';
  const last = turn.result ?? turn.processMessages[turn.processMessages.length - 1];
  const status = last?.metadata?.outcome_status;
  const content = last?.content.trim() ?? '';
  if (['interrupted', 'cancelled', 'canceled'].includes(status)
      || content === '已中止' || content.endsWith('\n\n[已中止]')) return 'interrupted';
  if (['failed', 'error', 'blocked'].includes(status) || /^发生错误[:：]/.test(content)) return 'failed';
  if (turn.result) return 'completed';
  const tools = collectProcessTools(turn.processMessages);
  if (tools.some(({tool}) => tool.state === 'interrupted')) return 'interrupted';
  if (tools.some(({tool}) => tool.state === 'error')) return 'failed';
  return 'incomplete';
}

export function chatRunDirectory(turns: PresentedChatTurn[], query: string, filter: string) {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  let index = 0;
  return turns.flatMap(turn => {
    const user = turn.user;
    if (!user?.id || user.presentation === 'async_hidden_prompt') return [];
    index += 1;
    const status = chatRunStatus(turn);
    if (filter !== 'all' && status !== filter) return [];
    const instruction = user.content.trim();
    if (terms.length) {
      const searchable = [instruction, turn.result?.content, ...turn.processMessages.map(message => message.content)]
        .join(' ').toLocaleLowerCase();
      if (!terms.every(term => searchable.includes(term))) return [];
    }
    const messages = [...turn.processMessages, ...(turn.result ? [turn.result] : [])];
    const toolTimes = user.created_at ? [] : collectProcessTools(messages)
      .map(({tool}) => Number(tool.start_time))
      .filter(time => time > 0 && Number.isFinite(time))
      .map(time => time < 1e12 ? time * 1000 : time);
    return [{id: user.id, index, status, summary: instruction.replace(/\s+/g, ' ') || '附件或空白指令',
      timestamp: user.created_at || (toolTimes.length ? Math.min(...toolTimes) : undefined)
        || messages.find(message => message.created_at)?.created_at
        || turn.result?.metadata?.completed_at}];
  }).reverse();
}

const hasVisibleProcessContent = (message: Message) =>
  Boolean(String(message.content || '').trim()) ||
  Boolean(message.content_blocks && message.content_blocks.length > 0) ||
  Boolean(message.tool_calls && message.tool_calls.length > 0);

const isExplicitAsyncFinal = (message: Message) =>
  message.presentation === 'async_final' ||
  String(message.id || '').startsWith('msg-async-final-');

const isStandaloneAsyncMessage = (message: Message) =>
  message.presentation === 'async_completion' ||
  isExplicitAsyncFinal(message);

export const isPublicCommentaryMessage = (message: Message) =>
  message.presentation === 'commentary' ||
  message.presentation === 'workflow_narration' ||
  message.presentation === 'model_output';

export function collectProcessTools(messages: Message[], includeOrphanOutputs = false) {
  const entries = new Map<string, { messageId: string; tool: NonNullable<Message['tool_calls']>[number]; index: number }>();
  messages.forEach((message) => (message.tool_calls || []).forEach((tool, index) => {
    const key = String(tool.call_id || '').trim()
      || `${tool.tool_name}:${JSON.stringify(tool.arguments || {})}`;
    entries.set(key, { messageId: message.id, tool, index });
  }));
  if (includeOrphanOutputs) {
    for (const message of messages) {
      const output = String(message.content || '').trim();
      if (message.role !== 'tool' || !output || [...entries.values()].some(({ tool }) =>
        String(tool.output || '').trim() === output || String(tool.error || '').trim() === output
      )) continue;
      entries.set(`output:${message.id}`, {
        messageId: message.id, index: 0,
        tool: { tool_name: '工具输出', call_id: `output:${message.id}`, arguments: {}, state: 'completed', output },
      });
    }
  }
  return [...entries.values()];
}


/** Display recovery without rewriting or deleting the persisted failure history. */
export function markRecoveredWorkflowMessages(messages: Message[]): Message[] {
  const statusOf = (message: Message) => message.role === 'assistant'
    ? /^workflow-status:([^:]+):([^:]+):[^:]+:\d+$/.exec(message.id || '')
    : null;
  const latest = new Map<string, { status: string; index: number }>();
  messages.forEach((message, index) => {
    const match = statusOf(message);
    if (match) latest.set(match[1], { status: match[2], index });
  });
  return messages.map((message, index) => {
    const match = statusOf(message);
    const current = match && latest.get(match[1]);
    if (!match || !['blocked', 'failed'].includes(match[2])
      || current?.status !== 'completed' || current.index <= index) return message;
    return {
      ...message,
      content: '**历史阻塞（已恢复）**\n\n该运行随后已完成全部阶段，最终结果见后续报告；这不是当前故障。\n\n'
        + '<details><summary>查看当时的阻塞详情</summary>\n\n'
        + message.content + '\n\n</details>',
    };
  });
}

/**
 * Group a flat persisted transcript into Codex-style turns.
 * The last assistant answer is the result; everything before it is process.
 */
export function presentChatTurns(
  messages: Message[],
  currentTurnRunning: boolean,
): PresentedChatTurn[] {
  const turns: Array<{ key: string; user?: Message; events: Message[]; standalone?: boolean }> = [];
  let current: { key: string; user?: Message; events: Message[]; standalone?: boolean } | null = null;

  const flush = () => {
    if (!current) return;
    turns.push(current);
    current = null;
  };

  for (const [index, message] of markRecoveredWorkflowMessages(messages).entries()) {
    if (isStandaloneAsyncMessage(message)) {
      // Completion notifications/results are not answers to whichever user
      // message happened to be latest when the background job finished.
      flush();
      turns.push({
        key: `async-${String(message.id || index)}`,
        events: [message],
        standalone: true,
      });
      continue;
    }
    if (String(message.role || '').toLowerCase() === 'user') {
      flush();
      current = {
        key: String(message.id || `turn-${index}`),
        user: message,
        events: [],
      };
      continue;
    }

    if (!current) {
      current = {
        key: `process-preamble-${String(message.id || index)}`,
        events: [],
      };
    }
    current.events.push(message);
  }
  flush();

  return turns.map((turn, turnIndex) => {
    // Background workflow narration is a real timeline message, not an
    // unfinished model answer that should be folded into the process panel.
    const running = currentTurnRunning && turnIndex === turns.length - 1 && !turn.standalone;
    let resultIndex = -1;
    if (!running) {
      // 异步完成通知和自动续答可能乱序到达。后端标记的详细续答始终优先，
      // 避免后到的通知卡片把真正结果挤进折叠过程。
      for (let index = turn.events.length - 1; index >= 0; index -= 1) {
        const candidate = turn.events[index];
        if (
          String(candidate.role || '').toLowerCase() === 'assistant' &&
          (String(candidate.content || '').trim() || Boolean(candidate.content_blocks?.length)) &&
          !isPublicCommentaryMessage(candidate) &&
          isExplicitAsyncFinal(candidate)
        ) {
          resultIndex = index;
          break;
        }
      }
    }
    if (!running && resultIndex < 0) {
      for (let index = turn.events.length - 1; index >= 0; index -= 1) {
        const candidate = turn.events[index];
        if (
          String(candidate.role || '').toLowerCase() === 'assistant' &&
          (String(candidate.content || '').trim() || Boolean(candidate.content_blocks?.length)) &&
          !isPublicCommentaryMessage(candidate)
        ) {
          resultIndex = index;
          break;
        }
      }
    }

    const processMessages = turn.events.filter((message, index) =>
      index !== resultIndex && hasVisibleProcessContent(message)
    );
    const rawResult = resultIndex >= 0 ? turn.events[resultIndex] : undefined;

    if (
      rawResult?.tool_calls && rawResult.tool_calls.length > 0
    ) {
      processMessages.push({
        ...rawResult,
        id: `${rawResult.id || turn.key}-process-tools`,
        role: 'tool',
        content: '',
        usage: undefined,
      });
    }

    const result = rawResult
      ? {
          ...rawResult,
          tool_calls: undefined,
          process_notes: undefined,
        }
      : undefined;

    return {
      key: turn.key,
      user: turn.user,
      processMessages,
      result,
      running,
    };
  });
}
