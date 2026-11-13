import { createMemo, createSignal, For, Show } from 'solid-js';
import { respondQuestion } from '../api/client';
import type { QuestionRequest } from '../stores/session';
import { normalizeQuestionOptions, questionAnswerValue, workflowQuestionSummary } from '../utils/questionPresentation';

type QuestionCardProps = { request: QuestionRequest; onAnswer?: (answer: string) => Promise<void>; notice?: string; compact?: boolean };
type BatchQuestionItem = {
  id: string;
  question: string;
  header?: string | null;
  type?: string;
  options?: unknown;
  allow_custom?: boolean;
  required?: boolean;
  recommendation?: string | null;
};

const batchItems = (request: QuestionRequest): BatchQuestionItem[] => {
  const raw = request.question.questions;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is BatchQuestionItem => Boolean(
    item && typeof item === 'object' && typeof item.id === 'string'
    && typeof item.question === 'string'
  ));
};

function BatchQuestionCard(props: QuestionCardProps & { items: BatchQuestionItem[] }) {
  const [page, setPage] = createSignal(0);
  let body: HTMLDivElement | undefined;
  const goTo = (index: number) => { setPage(Math.max(0, Math.min(index, props.items.length - 1))); if (body) body.scrollTop = 0; };
  const [selected, setSelected] = createSignal<Record<string, string[]>>({});
  const [custom, setCustom] = createSignal<Record<string, string>>({});
  const [customActive, setCustomActive] = createSignal<Record<string, boolean>>({});
  const [pending, setPending] = createSignal(false);
  const [error, setError] = createSignal('');
  const options = (item: BatchQuestionItem) => normalizeQuestionOptions(item.options);
  const value = (item: BatchQuestionItem) => questionAnswerValue(
    options(item), customActive()[item.id] ? [] : selected()[item.id] || [],
    customActive()[item.id] ? custom()[item.id] || '' : '', item.type === 'multiple'
  );
  const complete = () => props.items.every(item => item.required === false || value(item).trim());
  const answeredCount = () => props.items.filter(item => value(item).trim()).length;
  const firstMissing = () => props.items.findIndex(item => item.required !== false && !value(item).trim());
  const choose = (item: BatchQuestionItem, option: string) => {
    const wasCustom = customActive()[item.id];
    setCustomActive(previous => ({ ...previous, [item.id]: false }));
    setSelected(previous => {
      const current = wasCustom ? [] : previous[item.id] || [];
      return { ...previous, [item.id]: item.type === 'multiple'
        ? current.includes(option) ? current.filter(entry => entry !== option) : [...current, option]
        : [option] };
    });
  };
  const useCustom = (item: BatchQuestionItem) => {
    setCustomActive(previous => ({ ...previous, [item.id]: true }));
  };
  const submit = async () => {
    if (pending() || !complete()) return;
    setPending(true);
    setError('');
    const answer = JSON.stringify(Object.fromEntries(props.items.map(item => [item.id, value(item)])));
    try {
      if (props.onAnswer) await props.onAnswer(answer);
      else await respondQuestion(props.request.request_id, answer, props.request.session_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '提交失败，请重试');
    } finally {
      setPending(false);
    }
  };

  return <section role="region" aria-label="待回答问题组" data-question-card data-question-batch
    class={`flex ${props.compact ? 'h-[300px]' : 'h-[360px]'} w-full max-w-3xl shrink-0 flex-col overflow-hidden rounded-2xl border border-[#f1a02b]/28 bg-[#1d1b16] shadow-sm`}>
    <header class="shrink-0 border-b border-[#f1a02b]/10 px-4 py-3 sm:px-5">
      <div class="flex items-center gap-2 text-sm font-semibold text-[#ffe1b3]"><span class="h-2 w-2 rounded-full bg-[#f1a02b]" />需要你的回答 · {props.items.length} 项</div>
      <p class="mt-1 text-xs text-[#9faeb7]">{props.request.question.question || '请一次补充以下信息，完成后统一提交。'}</p>
      <div class="mt-2 flex items-center justify-between text-xs text-[#9faeb7]"><span aria-live="polite">第 {page() + 1} / {props.items.length} 题</span><span>已填写 {answeredCount()} 项</span></div>
    </header>
    <div ref={body} class="min-h-0 flex-1 overflow-y-auto px-4 py-3 sm:px-5" data-question-body>
      <Show when={props.notice}><p role="status" class="mb-2 text-xs leading-5 text-[#ffe1b3]">{props.notice}</p></Show>
      <For each={props.items}>{(item, index) => {
        const itemOptions = () => options(item);
        const allowCustom = () => item.allow_custom !== false || !itemOptions().length;
        const isCustom = () => !itemOptions().length || Boolean(customActive()[item.id]);
        const isSelected = (option: string) => !isCustom() && (selected()[item.id] || []).includes(option);
        const summary = () => isCustom() ? custom()[item.id]?.trim() || '请填写自定义答案'
          : (selected()[item.id] || []).join('、') || '尚未选择';
        return <fieldset hidden={index() !== page()} style={{display:index() === page() ? undefined : 'none'}} class="rounded-xl border border-[#3e484f] bg-[#171916]/60 p-3">
          <legend class="px-1 text-xs font-semibold text-[#8ed5ff]">{index() + 1}. {item.header || '补充信息'}{item.required === false ? ' · 可选' : ''}</legend>
          <p class="mb-3 whitespace-pre-wrap break-words text-sm leading-5 text-[#e6edf3]">{item.question}</p>
          <Show when={item.recommendation}><p class="mb-3 text-xs text-[#9faeb7]">建议：{item.recommendation}</p></Show>
          <Show when={itemOptions().length}><div class="mb-3 grid gap-2 sm:grid-cols-2">
            <For each={itemOptions()}>{option => <button type="button" disabled={pending()} aria-pressed={isSelected(option)}
              class={`flex min-h-11 items-center gap-2 rounded-xl border px-3 py-2 text-left text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#8ed5ff] ${isSelected(option) ? 'border-[#8ed5ff] bg-[#38bdf8]/20 font-semibold text-[#c4ecff] ring-1 ring-[#38bdf8]/40' : 'border-[#3e484f] bg-[#0f1418] text-[#bdc8d1]'}`}
              onClick={() => choose(item, option)}><span aria-hidden="true" class="w-4 shrink-0">{isSelected(option) ? '✓' : '○'}</span><span class="min-w-0 flex-1 break-words">{option}</span><Show when={isSelected(option)}><span class="shrink-0 text-[11px]">已选</span></Show></button>}</For>
          </div></Show>
          <Show when={allowCustom()}>
            <div class={`rounded-xl border p-2 transition-colors ${isCustom() ? 'border-[#8ed5ff] bg-[#38bdf8]/10 ring-1 ring-[#38bdf8]/40' : 'border-[#3e484f]'}`}>
              <button type="button" disabled={pending()} aria-pressed={isCustom()} class="mb-2 flex min-h-11 w-full items-center gap-2 rounded-lg px-1 text-left text-xs text-[#c4ecff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#8ed5ff]"
                onClick={event => { useCustom(item); event.currentTarget.parentElement?.querySelector('input')?.focus(); }}>
                <span aria-hidden="true">{isCustom() ? '✓' : '○'}</span><span>自定义输入</span><Show when={isCustom()}><span class="ml-auto">已选中</span></Show>
              </button>
            <input aria-label={`${item.header || `问题 ${index() + 1}`}的回答`} value={custom()[item.id] || ''} disabled={pending()}
              onFocus={() => useCustom(item)}
              onInput={event => { useCustom(item); setCustom(previous => ({ ...previous, [item.id]: event.currentTarget.value })); }}
              placeholder={itemOptions().length ? '也可输入其他答案' : '输入你的回答'}
              class="h-10 w-full rounded-xl border border-[#3e484f] bg-[#0f1418] px-3 text-sm text-[#e6edf3]" />
            </div>
          </Show>
          <p role="status" aria-live="polite" class="mt-3 break-words text-xs leading-5 text-[#c4ecff]">{isCustom() ? '当前答案 · 自定义：' : '当前答案：'}{summary()}</p>
        </fieldset>;
      }}</For>
    </div>
    <footer class="shrink-0 border-t border-[#f1a02b]/10 bg-[#171916]/60 px-4 py-3 sm:px-5">
      <Show when={error()}><p role="alert" class="mb-2 text-xs text-red-300">{error()}</p></Show>
      <div class="question-pagination" aria-label="问题组翻页">
        <span class="text-xs text-[#9faeb7]">翻页不提交答案</span>
        <div class="question-page-buttons"><button type="button" disabled={page() === 0 || pending()} onClick={() => goTo(page() - 1)}>上一题</button><button type="button" disabled={page() === props.items.length - 1 || pending()} onClick={() => goTo(page() + 1)}>下一题</button></div>
      </div>
      <div class="flex items-center justify-between gap-3"><Show when={page() === props.items.length - 1 && !complete()} fallback={<span class="text-xs text-[#9faeb7]">全部回答会一次提交</span>}><button type="button" disabled={pending()} class="text-xs text-[#8ed5ff]" onClick={() => goTo(firstMissing())}>返回未答题</button></Show>
        <button type="button" disabled={pending() || !complete()} onClick={() => void submit()}
          class="h-10 rounded-xl border border-[#38b46a]/28 bg-[#38b46a]/12 px-4 text-xs font-semibold text-[#9ee6bb] disabled:opacity-50">{pending() ? '正在提交…' : '提交全部回答'}</button>
      </div>
    </footer>
  </section>;
}

/** One bounded surface for every Question, independent of which agent emitted it. */
export default function QuestionCard(props: QuestionCardProps) {
  const batch = createMemo(() => batchItems(props.request));
  return <Show when={batch().length > 0} fallback={<SingleQuestionCard {...props} />}>
    <BatchQuestionCard {...props} items={batch()} />
  </Show>;
}

function SingleQuestionCard(props: QuestionCardProps) {
  const text = (value: unknown) => typeof value === 'string' ? value : '';
  const q = () => props.request.question;
  const workflow = createMemo(() => workflowQuestionSummary(q()));
  const options = createMemo(() => normalizeQuestionOptions(q().options));
  const multiple = () => q().type === 'multiple';
  const allowCustom = () => q().allow_custom !== false || !options().length;
  const directChoice = () => Boolean(workflow() && q().type === 'single' && !allowCustom());
  const [expanded, setExpanded] = createSignal(false);
  const [selected, setSelected] = createSignal<string[]>([]);
  const [custom, setCustom] = createSignal('');
  const [customActive, setCustomActive] = createSignal(false);
  const [pending, setPending] = createSignal(false);
  const [error, setError] = createSignal('');
  let body: HTMLDivElement | undefined;
  const description = () => workflow()?.reason || q().question || q().display || '请回答后继续';
  const title = () => workflow()?.title || text(q().goal) || '请补充信息，帮助我继续';
  const visibleOptions = () => expanded() ? options() : options().slice(0, 2);
  const answer = () => questionAnswerValue(options(), customActive() ? [] : selected(), customActive() ? custom() : '', multiple());

  const submit = async (override?: string) => {
    if (pending()) return;
    const value = override ?? answer();
    if (!value.trim()) return;
    setPending(true);
    setError('');
    try {
      // Bind to the request's owner, even if the user switches chats during POST.
      if (props.onAnswer) await props.onAnswer(value);
      else await respondQuestion(props.request.request_id, value, props.request.session_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : '提交失败，请重试');
    } finally {
      setPending(false);
    }
  };
  const choose = (option: string) => {
    if (directChoice()) { void submit(option); return; }
    const wasCustom = customActive();
    setCustomActive(false);
    if (multiple()) setSelected(items => wasCustom ? [option] : items.includes(option) ? items.filter(x => x !== option) : [...items, option]);
    else setSelected([option]);
  };
  const toggleDetails = () => {
    setExpanded(value => !value);
    if (body) body.scrollTop = 0;
  };

  return (
    <section role="region" aria-label="待回答问题" data-question-card
      class={`flex ${props.compact ? 'max-h-[300px]' : 'h-[360px]'} w-full max-w-3xl shrink-0 flex-col overflow-hidden rounded-2xl border border-[#f1a02b]/28 bg-[#1d1b16] shadow-sm`}>
      <header class="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-[#f1a02b]/10 px-4 sm:px-5">
        <div class="flex min-w-0 items-center gap-2 text-sm font-semibold text-[#ffe1b3]">
          <span class="h-2 w-2 shrink-0 rounded-full bg-[#f1a02b]" />
          <span class="truncate">{workflow() ? '启动前确认' : multiple() ? '请选择 · 可多选' : '需要你的回答'}</span>
        </div>
        <button type="button" aria-expanded={expanded()} aria-controls={`question-details-${props.request.request_id}`}
          class="shrink-0 rounded-full px-3 py-1.5 text-xs font-semibold text-[#8ed5ff] hover:bg-[#38bdf8]/10 focus-visible:outline focus-visible:outline-[#8ed5ff]"
          onClick={toggleDetails}>{expanded() ? '收起详情' : '展开详情'}</button>
      </header>

      <div ref={body} id={`question-details-${props.request.request_id}`} tabIndex={expanded() ? 0 : undefined}
        data-question-body class={`min-h-0 flex-1 px-4 py-3 sm:px-5 ${expanded() ? 'overflow-y-auto overscroll-contain' : 'overflow-hidden'}`}>
        <h3 class={`mb-2 break-words text-sm font-semibold leading-5 text-[#e6edf3] ${expanded() ? '' : 'line-clamp-1'}`}>{title()}</h3>
        <p class={`whitespace-pre-wrap break-words text-sm leading-5 text-[#bdc8d1] ${expanded() ? '' : 'line-clamp-3'}`}>{description()}</p>
        <Show when={props.notice}><p role="status" class="mt-2 whitespace-pre-wrap break-words text-xs leading-5 text-[#ffe1b3]">{props.notice}</p></Show>
        <Show when={expanded()}>
          <Show when={workflow()}>
            {summary => <div class="mt-3 space-y-2 whitespace-pre-wrap break-words text-xs leading-5 text-[#9faeb7]">
              <Show when={summary().purpose}><p>流程用途：{summary().purpose}</p></Show>
              <Show when={summary().gaps.length}><p>已知限制：{summary().gaps.join('；')}</p></Show>
              <p>继续会按所选流程执行，不代表限制已解决。不运行则返回普通对话处理。</p>
            </div>}
          </Show>
          <Show when={q().question && q().question !== description()}><p class="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-[#bdc8d1]">{q().question}</p></Show>
          <Show when={text(q().known_context)}><p class="mt-3 whitespace-pre-wrap break-words text-xs leading-5 text-[#9faeb7]">背景：{text(q().known_context)}</p></Show>
          <Show when={text(q().recommendation)}><p class="mt-3 whitespace-pre-wrap break-words text-xs leading-5 text-[#9faeb7]">建议：{text(q().recommendation)}</p></Show>
          <Show when={error()}><p role="alert" class="mt-3 whitespace-pre-wrap break-words text-xs text-red-300">{error()}</p></Show>
        </Show>
        <Show when={options().length}>
          <div class="mt-3 grid gap-2">
            <For each={visibleOptions()}>{option => (
              <button type="button" disabled={pending()} aria-pressed={!customActive() && selected().includes(option)} title={option}
                class={`flex min-h-11 min-w-0 items-center gap-2 rounded-xl border px-3 py-2 text-left text-xs leading-4 transition-colors disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#8ed5ff] ${!customActive() && selected().includes(option) ? 'border-[#8ed5ff] bg-[#38bdf8]/20 font-semibold text-[#c4ecff] ring-1 ring-[#38bdf8]/40' : 'border-[#3e484f] bg-[#171c20] text-[#bdc8d1] hover:border-[#7bd0ff]/35'}`}
                onClick={() => choose(option)}>
                <span aria-hidden="true">{!customActive() && selected().includes(option) ? '✓' : '○'}</span>
                <span class={`min-w-0 flex-1 ${expanded() ? 'whitespace-pre-wrap break-words' : 'truncate'}`}>{option}</span>
                <Show when={!customActive() && selected().includes(option)}><span class="shrink-0">已选</span></Show>
              </button>
            )}</For>
          </div>
        </Show>
      </div>

      <footer class="h-[100px] shrink-0 border-t border-[#f1a02b]/10 bg-[#171916]/60 px-4 py-3 sm:px-5">
        <div class="mb-2 flex h-5 min-w-0 items-center justify-between gap-2 text-xs text-[#9faeb7]">
          <span role="status" class="min-w-0 truncate" title={error() || selected().join('、')}>
            {pending() ? '正在提交回答…' : error() ? `提交失败：${error()}` : customActive() ? `已选自定义输入：${custom() || '请填写答案'}` : selected().length ? `当前答案：${selected().join('、')}` : directChoice() ? '点击选项直接提交' : '回答后继续'}
          </span>
          <Show when={!expanded() && options().length > 2}>
            <button type="button" class="shrink-0 text-[#8ed5ff]" onClick={toggleDetails}>查看全部 {options().length} 个选项</button>
          </Show>
        </div>
        <Show when={!directChoice()} fallback={<p class="text-xs leading-5 text-[#9faeb7]">如有疑问，请先展开详情了解原因与限制。</p>}>
          <form class="flex min-w-0 items-center gap-2" onSubmit={event => { event.preventDefault(); void submit(); }}>
            <Show when={allowCustom()} fallback={<span class="min-w-0 flex-1 truncate text-xs text-[#9faeb7]">{multiple() ? '可选择多个选项' : '请选择一个选项'}</span>}>
              <input aria-label="你的回答" value={custom()} disabled={pending()}
                onFocus={() => setCustomActive(true)}
                onInput={event => { setCustomActive(true); setCustom(event.currentTarget.value); }}
                placeholder={options().length ? '也可输入其他答案' : '输入你的答案'}
                class={`h-10 min-w-0 flex-1 rounded-xl border bg-[#0f1418] px-3 text-sm text-[#e6edf3] ${customActive() ? 'border-[#8ed5ff] ring-1 ring-[#38bdf8]/40' : 'border-[#3e484f]'}`} />
            </Show>
            <button type="submit" disabled={pending() || !answer().trim()}
              class="h-10 shrink-0 rounded-xl border border-[#38b46a]/28 bg-[#38b46a]/12 px-4 text-xs font-semibold text-[#9ee6bb] disabled:opacity-50">提交答案</button>
          </form>
        </Show>
      </footer>
    </section>
  );
}
