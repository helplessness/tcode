import { createMemo, Show } from 'solid-js';
import type { QuestionRequest } from '../stores/session';
import { respondQuestions } from '../api/client';
import { questionGroup } from '../utils/questionGroup';
import QuestionCard from './QuestionCard';

/** One submission for all live questions, including independent tool calls. */
export default function QuestionPager(props: {
  requests: QuestionRequest[];
  compact?: boolean;
  notice?: (request: QuestionRequest) => string;
  onAnswers?: (answers: Record<string, string>) => Promise<void>;
}) {
  const group = createMemo(() => questionGroup(props.requests));
  const submit = async (answers: Record<string, string>) => {
    if (props.onAnswers) await props.onAnswers(answers);
    else await respondQuestions(answers, props.requests[0].session_id);
  };
  return <Show when={props.requests[0]?.session_id} keyed>{sessionId =>
    <div class="question-pager w-full max-w-3xl min-w-0">
      <QuestionCard compact={props.compact}
        request={{
          request_id: 'question-group', session_id: sessionId,
          question: {question:'请完成当前问题组后统一提交', type:'batch', questions:group().questions},
        }}
        notice={props.requests.map(request => props.notice?.(request)).filter(Boolean).join('\n')}
        onAnswer={answer => submit(group().answers(answer))}/>
    </div>
  }</Show>;
}
