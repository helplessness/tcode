import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { reviewPhase, reviewReferences, reviewSummary } from './HumanReviewCard';
import type { HumanReviewItem } from '../api/humanReview';
import { collectProcessTools, presentChatTurns } from '../utils/chatPresentation';
import type { Message } from '../stores/session';

const review = (extra: Partial<HumanReviewItem> = {}): HumanReviewItem => ({
  id:'r', identifier:'REVIEW-1', status:'pending_review', tool_server:'boltz-mcp', tool_name:'boltz_mcp_server', tool_args:{}, ...extra,
});

describe('human review presentation', () => {
  it('uses structured tool records even when the model only says a Chinese approval is needed', () => {
    const call = {tool_name:'mcp_job-protocol-mcp_submit_job', arguments:{}, metadata:{human_review_required:true,id:'r',identifier:'REVIEW-1'}};
    expect(reviewReferences([call, call, {...call,metadata:{}}])).toEqual([{reviewId:'r',reviewIdentifier:'REVIEW-1'}]);
  });
  it('finds the review on a process tool message when the final answer has no tool calls', () => {
    const call = {tool_name:'mcp_job-protocol-mcp_submit_job',call_id:'call-review',arguments:{},state:'completed',metadata:{human_review_required:true,id:'r',identifier:'REVIEW-1'}};
    const messages = [
      {id:'u',role:'user',content:'核对输入并申请审核'},
      {id:'a',role:'assistant',content:'',tool_calls:[call]},
      {id:'t',role:'tool',content:'Human review required',tool_calls:[call]},
      {id:'f',role:'assistant',content:'这一步需要审核，审核完成后才能继续。'},
    ] as Message[];
    const turn = presentChatTurns(messages, false)[0];
    expect(turn.result?.tool_calls?.length || 0).toBe(0);
    expect(reviewReferences(collectProcessTools(turn.processMessages).map(entry => entry.tool))).toEqual([{reviewId:'r',reviewIdentifier:'REVIEW-1'}]);
    const chat = readFileSync(new URL('./ChatView.tsx', import.meta.url),'utf8');
    expect(chat).toContain('HumanReviewCard reference={reference}');
  });
  it('never labels approval or an enqueue failure as computation success', () => {
    expect(reviewPhase(review())).toBe('等待审核');
    expect(reviewPhase(review({status:'approved'}))).toContain('执行状态待确认');
    expect(reviewPhase(review({status:'approved',resume_status:'failed'}))).toContain('失败');
    expect(reviewPhase(review({status:'approved',resume_status:'completed',resume_result:{started:true,approved_action:{status:'queued'}}}))).toContain('计算结果待返回');
    expect(reviewPhase(review({status:'rejected'}))).toBe('已拒绝');
  });
  it('shows required scientific parameters and input names, not addresses or credentials', () => {
    const rows = reviewSummary(review({tool_args:{input_config:{
      tools_input:'/host/private/U8-0123456789abcdef0123456789abcdef.yaml', model:'boltz2', seed:42,
      affinity_ligand_index:null, devices:1, use_potentials:false, endpoint:'http://private', token:'secret',
    }}}));
    expect(rows).toContainEqual({label:'输入文件',value:'U8.yaml'});
    expect(rows).toContainEqual({label:'亲和力预测',value:'不启用'});
    expect(JSON.stringify(rows)).not.toMatch(/private|secret|0123456789abcdef/);
  });
  it('shows native operation outcomes separately from computation results', () => {
    expect(reviewPhase(review({status:'approved',resume_result:{native_tool_status:'completed'}}))).toBe('已批准，操作已完成');
    expect(reviewPhase(review({status:'approved',resume_status:'failed',resume_result:{native_tool_status:'failed'}}))).toBe('已批准，但操作执行失败');
    expect(reviewPhase(review({status:'approved',resume_result:{native_tool_status:'cancelled'}}))).toBe('已批准，操作已取消');
    expect(reviewPhase(review({status:'pending_review',resume_result:{native_tool_status:'completed'}}))).toBe('等待审核');
  });
  it('shows homology search scope from the stored review without exposing runtime fields', () => {
    const args = {fasta:'research/derived/U8_chainA_A.fasta',max_sequences:500,evalue:1e-10,threads:2,
      endpoint:'http://private-server:10110/mcp',token:'secret',database:'/private/database'};
    const rows = reviewSummary(review({tool_name:'homology_search_mcp_server',tool_args:{input_config:args}}));
    expect(rows).toEqual([
      {label:'输入文件',value:'U8_chainA_A.fasta'},
      {label:'最多返回序列数',value:'500'},
      {label:'E-value 阈值',value:'1e-10'},
      {label:'线程数',value:'2'},
    ]);
    expect(reviewSummary(review({tool_args:args}))).toEqual(rows);
    expect(JSON.stringify(rows)).not.toMatch(/research\/|private|secret|10110/);
  });
  it('shows alignment and conservation inputs while retaining safe scalar filtering', () => {
    expect(reviewSummary(review({tool_args:{fasta:'/private/homologs.fasta',threads:2}}))).toEqual([
      {label:'输入文件',value:'homologs.fasta'}, {label:'线程数',value:'2'},
    ]);
    expect(reviewSummary(review({tool_args:{input_config:{alignment:'/private/aligned.fasta',query_id:'U8_chainA_A'}}}))).toEqual([
      {label:'输入文件',value:'aligned.fasta'}, {label:'目标序列名称',value:'U8_chainA_A'},
    ]);
    expect(JSON.stringify(reviewSummary(review({tool_args:{fasta:{artifact_id:'art_private'},threads:'http://secret',query_id:{token:'secret'}}})))).not.toMatch(/private|secret/);
  });
  it('refreshes on panel activation and keeps errors separate from an empty queue', () => {
    const panel = readFileSync(new URL('./HumanReviewPanel.tsx', import.meta.url),'utf8');
    expect(panel).toContain('[isActive(), sessionStore.currentSessionId()]');
    expect(panel).toContain('审核状态未知');
    expect(panel).toContain('sessionId !== sessionStore.currentSessionId()');
    expect(panel).toContain("selectedReview()?.status !== 'pending_review'");
  });
  it('shows the FoldX input and exact scan scope without private paths or unsafe values', () => {
    const args = {pdb:'/private/server/input.pdb',positions:['AA12a'],token:'secret'};
    expect(reviewSummary(review({tool_name:'foldx_scan_mcp_server',tool_args:args}))).toEqual([
      {label:'输入文件',value:'input.pdb'}, {label:'扫描位点（PDB编号）',value:'AA12a'},
    ]);
    expect(reviewSummary(review({tool_args:{input_config:args}}))).toEqual(reviewSummary(review({tool_args:args})));
    expect(JSON.stringify(reviewSummary(review({tool_args:{...args,positions:['http://private/secret']}})))).not.toMatch(/private|secret/);
    expect(reviewSummary(review({tool_args:{...args,positions:Array(501).fill('AA12a')}}))).toHaveLength(1);
  });
});
