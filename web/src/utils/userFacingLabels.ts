import {readableResearchMarkdown} from './researchMarkdown';
/** Presentation only: never use these labels as API identifiers. */
export function userFacingTitle(value: unknown, fallback: string): string {
  const text = String(value ?? '').trim();
  return !text || internalReference.test(text) || /^(?:pack|run|session|asset|import|inspection|research|experiment|draft|job)[-_][a-f0-9-]{16,}$/i.test(text)
    || /^[a-f0-9]{32}$/i.test(text)
    || /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(text)
    ? fallback : text;
}

export function skillDisplayName(args: unknown): string {
  let data = args;
  if (typeof data === 'string') {
    try { data = JSON.parse(data); } catch { return '专项技能'; }
  }
  const name = data && typeof data === 'object' ? String((data as Record<string, unknown>).name || '').trim() : '';
  const names: Record<string, string> = {
    'gnina-runner': '分子对接', 'plip-runner': '蛋白与配体相互作用分析',
    'boltz-runner': '蛋白结构预测', 'ligandmpnn-runner': '配体环境序列设计',
    'placer-runner': '局部结构采样', 'rfdiffusion-runner': '蛋白骨架生成',
    'evolvepro-runner': '蛋白定向进化', 'simulation-runner': '分子模拟',
    'multi-model-sequence-mutation-scoring': '序列突变评分',
    'protein-job-protocol': '异步计算任务管理',
    'foldx-runner': 'FoldX 稳定性分析',
    'paper-literature-search': '文献检索',
  };
  return names[name] || (/^[\u3400-\u9fff\s·（）]{1,40}$/.test(name) ? name : '专项技能');
}

export function toolDisplayLabel(value: unknown, args?: unknown): string {
  const name = String(value ?? '').toLowerCase();
  if (name === 'skill') return args === undefined ? '加载技能' : `加载技能 · ${skillDisplayName(args)}`;
  if (/goal_checkpoint/.test(name)) return '记录目标进展';
  if (/artifact_query/.test(name)) return '查看任务产物';
  if (/skill_source/.test(name)) return '查找技能来源';
  if (/skill_install/.test(name)) return '安装技能';
  if (/computer_press_key/.test(name)) return '键盘操作';
  if (/computer_click/.test(name)) return '点击界面';
  if (/describe_target_tool/.test(name)) return '确认工具调用要求';
  if (/submit_job/.test(name)) return '提交计算任务';
  if (/gnina/.test(name)) return '分子对接';
  if (/plip/.test(name)) return '蛋白与配体相互作用分析';
  if (/boltz|alphafold|structure_predict/.test(name)) return '蛋白结构预测';
  if (/foldx.*repair/.test(name)) return '蛋白结构修复';
  if (/foldx.*scan/.test(name)) return '蛋白位点稳定性扫描';
  if (/homology.*search/.test(name)) return '蛋白同源序列搜索';
  if (/homology.*align/.test(name)) return '蛋白多序列比对';
  if (/homology.*conservation/.test(name)) return '蛋白位点保守性分析';
  if (/paper|literature|pubmed/.test(name)) return '文献检索与整理';
  if (/research_pack/.test(name)) return '研究资料处理';
  if (/workflow/.test(name)) return '工作流执行';
  if (/job_status|collect_job|job_result/.test(name)) return '计算进度与结果查询';
  if (/search/.test(name)) return '检索资料';
  if (/read_file|list_files|file_read/.test(name)) return '读取文件';
  if (/write_file|edit_file|apply_patch/.test(name)) return '更新文件';
  if (/canvas|diagram/.test(name)) return '图表处理';
  if (/python|shell|exec|sandbox/.test(name)) return '执行计算';
  if (/capability_catalog/.test(name)) return '查找可用能力';
  return '执行任务';
}

/** Narration only: keep execution records and scientific filenames unchanged. */
export function publicNarrationText(value: unknown, toolNames: string[] = []): string {
  const known = new Set(toolNames);
  return String(value ?? '').replace(
    /[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)*/g,
    (name) => {
      const label = toolDisplayLabel(name);
      const internal = known.has(name) || /^mcp[_-]/i.test(name)
        || (name.includes('_') && !name.includes('.') && label !== '执行任务');
      return internal ? label : name;
    },
  );
}

const researchStates:Record<string,string>={
  run_limit_reached:'已达到运行次数上限',time_limit_reached:'已达到持续时间上限',
  token_limit_reached:'已达到模型用量上限',tool_call_limit_reached:'已达到工具调用上限',
  failure_limit_reached:'已达到累计失败上限',cost_limit_reached:'已达到已记录费用上限',
  active:'进行中',archived:'已归档',planned:'待执行',pending:'待处理',queued:'排队中',submitted:'已提交',starting:'启动中',running:'进行中',completed:'已完成',succeeded:'已完成',success:'成功',failed:'失败',error:'发生错误',cancelled:'已取消',canceled:'已取消',paused:'已暂停',stopped:'已停止',idle:'空闲',not_started:'尚未开始',incomplete:'未完成',partial:'部分完成',ready:'已就绪',proposed:'待批准',approved:'已批准',testing:'验证中',supported:'已有支持',refuted:'已证伪',withdrawn:'已撤回',inconclusive:'尚无定论',recommended:'推荐继续',retained:'保留备选',rejected:'不再推进',merged:'成果已纳入研究',disputed:'存在争议',superseded:'已被取代',retracted:'已撤回',context_limited:'限定条件有效',open:'待处理',under_review:'核对中',resolved:'已处理',accepted:'已接受',dismissed:'已忽略',importing:'导入中',inspected:'已检查',valid:'有效',invalid:'无效',observed:'实际记录',inferred:'推断整理',model_inference:'模型推断',source_fact:'来源事实',user_confirmed:'用户确认',computation:'计算结果',confirmed:'已确认',derived:'推导结果',automatic:'自动整理',manual:'手动记录',standard:'标准',strict:'严格',not_applicable:'无需限制',within_budget:'预算内',ok:'正常',minimum_overflow:'必要内容超过预算',conflict_overflow:'冲突信息超过预算',normal:'正常',clear:'通过',required:'需要核对',unavailable:'暂不可用',shared_workspace:'共享工作区',isolated_directory:'独立运行目录',git_worktree:'独立代码工作区',local:'本地',remote:'远程',draft:'草稿',unassigned:'待归属',included:'已纳入',excluded:'已排除',candidate:'待确认',confirmed_project:'已确认课题',current:'当前版本',stale:'需要更新',missing:'未找到',warning:'需要注意',blocked:'暂无法继续',user:'用户',agent:'研究助手',system:'系统',tool:'工具',primary:'主要',supporting:'辅助',direct:'直接证据',analogy:'类比证据',context_only:'背景来源',supports:'支持',contradicts:'反驳',derived_from:'派生来源',cites:'引用',relates_to:'相关',paper:'论文',evidence:'证据',finding:'研究结论',experiment:'实验',hypothesis:'假说',session:'研究会话',workflow:'计算流程',artifact:'产物',source:'来源',conflict:'结论冲突',passage:'原文片段',wiki_page:'知识页',wiki_claim:'知识结论',workspace:'研究空间',project:'课题',full:'完整',copy:'本地副本',parent:'来源研究包',overlay:'本地接力记录',resume_context_disabled:'研究历史接续未启用',owner_context_missing:'账号或研究空间信息不完整',single_active_project_not_found:'没有唯一的当前课题',project_not_active:'当前课题未处于研究中',empty_question:'尚未输入问题',user_opted_out:'用户选择不使用研究历史',checkpoint_missing:'尚未生成研究概况',checkpoint_quality_blocked:'研究概况尚未通过质量核对',final_context_overflow:'研究上下文超过预算',mandatory_conflict_context_overflow:'必要冲突信息超过预算',mandatory_conflict_set_incomplete:'必要冲突信息不完整',deterministic_term_overlap:'问题与当前研究主题相关',explicit_continuation:'用户明确要求继续研究',no_research_relevance_signal:'未找到与当前研究相关的内容',workflow_template_changed:'计算方案已变更，需要重新核对',execution_environment_changed:'执行环境已变更，需要重新核对',max_runs:'已达到研究轮次上限',max_minutes:'已达到时间上限',max_tokens:'已达到模型用量上限',max_tool_calls:'已达到操作次数上限',max_failures:'已达到失败次数上限',max_cost:'已达到费用上限',budget_exhausted:'研究预算已用尽',untrusted_data:'导入资料，尚需核对',sanitized_trace:'已隐藏隐私的来源记录',event:'研究活动',source_trace:'来源追踪',session_message:'会话记录','message.created':'会话消息',evidence_disagreement:'证据不一致',finding_challenged:'结论受到质疑',conclusion_conflict:'结论不一致',source_conflict:'来源不一致',scope_conflict:'适用条件不同'
};
export const researchStatusLabel=(value:unknown,fallback='状态未说明')=>researchStates[String(value||'')]||(String(value||'').trim()?fallback:'未记录');
const researchFields:Record<string,string>={title:'名称',name:'名称',statement:'判断内容',summary:'摘要',summary_text:'摘要',description:'说明',data:'资料内容',read_only:'只读资料',content_trust:'可信性说明',automatic_execution_allowed:'允许自动执行',distribution:'资料保存方式',included:'已纳入',complete:'资料完整',actor_type:'记录者类型',layer:'资料来源',record:'研究记录',object:'资料内容',trust:'可信性说明',trust_level:'可信性说明',redaction_mode:'隐私处理方式',preview:'内容摘要',payload:'记录内容',event_type:'记录类型',source_kind:'来源类型',included_reason:'纳入原因',included_as_dependency:'作为关联资料',content:'内容',text:'正文',reason:'原因或目的',status:'状态',state:'状态',decision:'判断',provenance:'来源类型',scope:'适用范围',source_scope:'来源范围',activity_status:'记录状态',attribution:'归因',rationale:'提出依据',result_summary:'结果摘要',revision:'版本',current_revision:'版本',hypothesis_revision:'假说版本',test_plan:'验证计划',falsification_condition:'证伪条件',metrics:'指标',score:'评分',confidence:'置信度',accuracy:'准确率',energy:'能量',temperature:'温度',rmsd:'结构偏差',plddt:'局部可信度',ptm:'整体可信度',iptm:'界面可信度',input:'输入参数',inputs:'输入参数',output:'输出结果',outputs:'输出结果',input_artifacts:'输入文件',output_artifacts:'输出文件',artifacts:'研究产物',sources:'来源',source:'来源',source_type:'来源类型',metadata:'补充信息',environment:'执行环境',model:'模型',backend:'执行位置',stages:'研究步骤',stage:'研究步骤',validation:'检查结果',counts:'数量',files:'文件',filename:'文件名',path:'文件',ref:'来源文件',source_path:'来源文件',doi:'论文永久标识',pmid:'文献数据库编号',url:'来源链接',page:'页码',locator:'原文位置',created_at:'创建时间',updated_at:'更新时间',occurred_at:'记录时间',timestamp:'记录时间',experiences:'研究经历',unassigned:'待归属记录',researches:'课题',events:'研究活动',findings:'研究结论',conflicts:'结论冲突',evidence:'证据',sessions:'研究会话',activities:'研究进展',episodes:'研究阶段',canvases:'交互画布',pages:'交互页面',papers:'论文',wiki_pages:'知识页',candidates:'研究候选',schema_version:'资料格式版本',execution_mode:'执行位置',value:'数值',unit:'单位',units:'单位',display_name:'名称',template_key:'计算方案',binding_affinity:'结合亲和力',docking_score:'对接评分',confidence_score:'置信度',delta_g:'自由能变化',rmse:'均方根误差',mae:'平均绝对误差',mse:'均方误差',r2:'决定系数',auc:'曲线下面积',run_snapshot:'计算与来源信息',scope_type:'范围类型',object_type:'资料类型',relation:'关联关系'};
export function researchFieldLabel(key:unknown,fallback='补充信息') {
  const text=String(key||'');
  return researchFields[text]||(/^[\u3400-\u9fff]/.test(text)?text:fallback);
}
const internalReference=/^(?:(?:hyp|exp|rs|ev|evt|pack|run|session|asset|import|inspection|research|event|workflow|activity|finding|conflict|claim|chunk|anchor|paper|fact|proposal|revision|rev|page|canvas|experiment|episode|link|job)[-_:][\w.…:-]+|LOCAL-[\w-]+|[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i;
export function researchValueText(value:unknown,key=''):string {
  if(value===undefined||value===null||value==='')return '未记录';
  if(/(?:^|_)revision$/.test(key)&&/^v?\d+$/.test(String(value)))return `第 ${String(value).replace(/^v/,'')} 版`;
  if(typeof value==='boolean')return value?'是':'否';
  if(typeof value==='number')return String(value);
  if(Array.isArray(value))return value.length?`已记录 ${value.length} 项`:'未记录';
  if(typeof value==='object')return Object.keys(value).length?`已记录 ${Object.keys(value).length} 项信息`:'未记录';
  const text=String(value);
  
  if(/^https?:\/\//i.test(text))return text;
  if(/(?:^|_)(?:path|ref)$/.test(key))return text.replace(/\\/g,'/').split('/').pop()||'来源文件';
  if(/(?:status|state|decision|provenance|attribution|scope_type|object_type|execution_mode|source_type|activity_status|kind|type|role|actor_type|included_reason|source_kind|状态|判断|来源类型)$/.test(key))return researchStatusLabel(value,'含义尚未说明');
  if(internalReference.test(text))return '已关联记录（未保存名称）';
  if (/^(?:layer|trust|trust_level|redaction_mode|distribution|content_trust)$/.test(key))return researchStatusLabel(value,'含义尚未说明');
  if (/^[a-z]+(?:_[a-z0-9]+)+$/.test(text)&&! /^(?:title|name|model|filename|doi|pmid|text|content|summary|preview|description)$/.test(key))return researchStatusLabel(value,'已记录（含义尚未说明）');
  return key==='preview'?readableResearchMarkdown(text):text;
}
export function researchDetailRows(value:unknown,limit=100) {
  const rows:Array<{label:string;value:string}>=[];let omitted=0;
  const visit=(item:unknown,path:string,depth:number)=>{
    if(rows.length>=limit){omitted++;return;}
    if(item&&typeof item==='object'&&depth<4){
      const entries=Array.isArray(item)?item.map((v,i)=>[String(i+1),v] as const):Object.entries(item);
      if(!entries.length){rows.push({label:path||'内容',value:'未记录'});return;}
      for(const [key,v] of entries){
        if(typeof v==='string'&&/^sha(?:256|512):/i.test(v))continue;
        if(/(^id$|_ids?$|_hash$|_digest$|^schema$|^git_commit$|^request_id$|^causality$|^actor_id$|^hash$|^digest$)/.test(key))continue;
        const label=Array.isArray(item)?`第 ${key} 项`:researchFieldLabel(key,`补充信息 ${entries.findIndex(e=>e[0]===key)+1}`);
        if(v&&typeof v==='object')visit(v,path?`${path} · ${label}`:label,depth+1);
        else if(rows.length<limit)rows.push({label:path?`${path} · ${label}`:label,value:researchValueText(v,key)});
        else omitted++;
      }
    }else rows.push({label:path||'内容',value:researchValueText(item)});
  };
  visit(value,'',0);return {rows,omitted};
}

export function researchAgentLabel(value:unknown) {
  const name=String(value||'').trim();
  const known:Record<string,string>={paper_auto:'文献自动协作',build:'开发助手',plan:'规划助手',research:'研究助手',supervisor:'研究协调助手',root:'主研究助手'};
  return known[name]||(/^[a-z]+(?:_[a-z0-9]+)+$/.test(name)?'研究助手（名称未提供）':userFacingTitle(name,'未命名研究助手'));
}
