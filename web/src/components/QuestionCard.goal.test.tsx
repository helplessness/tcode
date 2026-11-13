import { renderToString } from 'solid-js/web';
import { expect, it } from 'vitest';
import QuestionCard from './QuestionCard';
import QuestionPager from './QuestionPager';
import { goalQuestionNotice, type GoalQuestion } from '../api/goalMode';

const question: GoalQuestion = {id:'internal-reference',status:'pending',required:true,
  deadline:null,default_answer:'',answer:'',payload:{question:'请提供起始序列',type:'custom'}};

it('retains the answer field for a critical goal question without exposing its internal id', () => {
  const html = renderToString(() => <QuestionCard compact
    request={{request_id:question.id,session_id:'session',question:question.payload}}
    notice={goalQuestionNotice(question)} onAnswer={async () => {}} />);
  expect(html).toContain('关键输入必须由你确认');
  expect(html).toContain('不会超时默认');
  expect(html).toContain('aria-label="你的回答"');
  expect(html).not.toContain('h-[360px]');
  expect(html).not.toContain('>internal-reference<');
});

it('keeps the input after using a default and labels its provenance', () => {
  const item = {...question,status:'defaulted' as const,required:false,answer:'简洁'};
  const html = renderToString(() => <QuestionCard compact
    request={{request_id:item.id,session_id:'session',question:item.payload}}
    notice={goalQuestionNotice(item)} onAnswer={async () => {}} />);
  expect(html).toContain('超时暂用默认值：简洁（非用户确认）');
  expect(html).toContain('仍可补答');
  expect(html).toContain('提交答案');
});

it('leaves the ordinary question layout and default response path unchanged', () => {
  const html = renderToString(() => <QuestionCard request={{request_id:'q',session_id:'s',question:question.payload}}/>);
  expect(html).toContain('h-[360px]');
  expect(html).not.toContain('超时暂用默认值');
});

it('renders every independent question without truncating the batch', () => {
  const html = renderToString(() => <QuestionCard request={{request_id:'batch',session_id:'s',question:{
    question:'发布前确认',type:'batch',questions:[
      {id:'platform',header:'平台',question:'目标平台？',type:'single',options:['macOS','Windows'],allow_custom:false,required:true},
      {id:'features',header:'功能',question:'需要哪些功能？',type:'multiple',options:['文件预览','远程 MCP'],allow_custom:false,required:true},
      {id:'note',header:'补充',question:'还有什么要求？',type:'custom',required:false},
      {id:'audience',header:'用户',question:'谁会使用？',type:'custom',required:true},
      {id:'delivery',header:'交付',question:'如何分发？',type:'custom',required:true},
    ],
  }}}/>);
  expect(html).toContain('data-question-batch');
  expect(html).toContain('需要你的回答 ·');
  expect(html).toContain('5<!--/--> 项');
  expect(html).toContain('目标平台？');
  expect(html).toContain('需要哪些功能？');
  expect(html).toContain('谁会使用？');
  expect(html).toContain('如何分发？');
  expect(html).toContain('提交全部回答');
  expect(html).toContain('问题组翻页');
  expect(html).toContain('翻页不提交答案');
  expect(html).not.toContain('max-h-[600px]');
  expect(html.match(/<fieldset[^>]*\shidden(?:\s|=|>)/g)?.length).toBe(4);
});

it('combines independent requests into one paged form and one submit action', () => {
  const html = renderToString(() => <QuestionPager requests={['first','second'].map(id => ({request_id:id,session_id:'owner',question:{question:id,type:'custom'}}))}/>);
  expect(html).toContain('问题组翻页');
  expect(html).toContain('上一题');
  expect(html).toContain('下一题');
  expect(html.match(/data-question-card/g)?.length).toBe(1);
  expect(html.match(/<fieldset[^>]*\shidden(?:\s|=|>)/g)?.length).toBe(1);
  expect(html.match(/提交全部回答/g)?.length).toBe(1);
});
