import { createSignal, createEffect, createMemo, on, Show, For } from 'solid-js';
import { respondPermission } from '../../api/client';
import { sessionStore } from '../../stores/session';
import { composerIdentity, composerKey } from '../../stores/composer';
import QuestionPager from '../QuestionPager';

/** Permissions remain in the composer; all Questions share the timeline card. */
export default function InteractionPanel(props: { mode: 'permissions' | 'questions' }) {
  const draftKey = () => composerKey(sessionStore.currentSessionId());
  const [interactionErrors, setInteractionErrors] = createSignal<Record<string, string>>({});
  const pendingInteractions = new Set<string>();
  const interactionKey = (requestId: string) => `${draftKey()}:${requestId}`;
  const [showQuestionHistory, setShowQuestionHistory] = createSignal(false);
  const pendingQuestions = createMemo(() => {
    const sessionId = sessionStore.currentSessionId();
    const requests = sessionStore.questionHistory().filter(item => item.session_id === sessionId && item.status === 'pending').slice().reverse().map(item => ({ request_id:item.request_id, session_id:item.session_id, question:item.question }));
    const current = sessionStore.questionRequest();
    if (current && !requests.some(item => item.request_id === current.request_id)) requests.push(current);
    return requests;
  });
  const questionHistoryStatusLabel = (status: string) => ({ pending: '待回答', answered: '已回答', timeout: '已超时', error: '已结束' }[status] || status);
  const currentQuestionHistory = createMemo(() => {
    const sessionId = sessionStore.currentSessionId();
    return sessionId ? sessionStore.questionHistory().filter(item => item.session_id === sessionId && item.status !== 'pending').slice(0, 8) : [];
  });
  const handlePermission = async (response: 'once' | 'always' | 'reject' | 'allow') => {
    const request = sessionStore.permissionRequest();
    const sessionId = sessionStore.currentSessionId();
    if (!request || !sessionId) return;
    const key = interactionKey(request.request_id);
    if (pendingInteractions.has(key)) return;
    pendingInteractions.add(key);
    const identity = composerIdentity();

    try {
      await respondPermission(request.request_id, response, sessionId);
      if (composerIdentity() !== identity) return;
      if (sessionStore.getSessionRuntimeState(sessionId).permissionRequest?.request_id === request.request_id) {
        sessionStore.setSessionPermissionRequest(sessionId, null);
        sessionStore.setSessionStatus(sessionId, response === 'reject' ? '权限已拒绝' : '权限已允许');
      }
      setInteractionErrors(previous => ({ ...previous, [key]: '' }));
    } catch (e) {
      console.error('Failed to respond permission:', e);
      setInteractionErrors(previous => ({ ...previous, [key]: e instanceof Error ? e.message : '提交失败，请重试' }));
    } finally {
      pendingInteractions.delete(key);
    }
  };


  createEffect(on(sessionStore.currentSessionId, () => setShowQuestionHistory(false)));
  return (
    <>
        <Show when={props.mode === 'permissions' && sessionStore.permissionRequest()}>
          {(request) => (
            <div class="permission-request mb-4 rounded-2xl border border-[#f1a02b]/28 bg-[#211a0f] px-4 py-4">
              <div class="permission-request-body" role="region" aria-label="本次工具授权内容" tabIndex={0}>
              <Show when={interactionErrors()[interactionKey(request().request_id)]}>{error => <p role="alert" class="mb-2 text-sm text-red-300">{error()}</p>}</Show>
              <div class="mb-3 text-sm text-[#ffe1b3]">
                允许使用工具：<span class="font-semibold text-[#fff0d5]">{request().tool_name}</span>
              </div>
              <details>
                <summary class="cursor-pointer text-sm">查看本次工具参数</summary>
                <pre>{JSON.stringify(request().arguments, null, 2)}</pre>
              </details>
              </div>
              <div class="permission-request-actions flex flex-wrap gap-2">
                <button
                  type="button"
                  class="rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-4 py-2 text-xs font-semibold text-[#9ee6bb]"
                  onClick={() => handlePermission('once')}
                >
                  仅一次
                </button>
                <button
                  type="button"
                  class="rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/12 px-4 py-2 text-xs font-semibold text-[#8ed5ff]"
                  onClick={() => handlePermission('always')}
                >
                  始终允许
                </button>
                <button
                  type="button"
                  class="rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 px-4 py-2 text-xs font-semibold text-[#ffcbc7]"
                  onClick={() => handlePermission('reject')}
                >
                  拒绝
                </button>
              </div>
            </div>
          )}
        </Show>


      <Show when={props.mode === 'questions' && pendingQuestions().length > 0}>
        <QuestionPager requests={pendingQuestions()}/>
      </Show>
        <Show when={props.mode === 'questions' && currentQuestionHistory().length > 0}>
          <div class="mb-4 rounded-2xl border border-[#2a3136] bg-[#13191d] px-4 py-4">
            <button
              type="button"
              class="mb-3 rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
              onClick={() => setShowQuestionHistory(!showQuestionHistory())}
            >
              {showQuestionHistory() ? '收起已处理的问题' : `已处理的问题 · ${currentQuestionHistory().length}`}
            </button>
            <Show when={showQuestionHistory()}>
              <div class="max-h-40 space-y-2 overflow-y-auto">
                <For each={currentQuestionHistory()}>
                  {(item) => (
                    <div class="rounded-2xl border border-[#2a3136] bg-[#0f1418] px-3 py-3 text-xs">
                      <div class="text-[#9faeb7]">{questionHistoryStatusLabel(item.status)}</div>
                      <div class="mt-1 truncate text-[#e6edf3]">{item.question.question}</div>
                      <Show when={item.answer !== undefined && item.answer !== ''}>
                        <div class="mt-1 truncate text-[#8ed5ff]">答：{item.answer}</div>
                      </Show>
                    </div>
                  )}
                </For>
              </div>
            </Show>
          </div>
        </Show>


    </>
  );
}
