// Preserve Markdown structure; resolve references without rewriting stored history.
export function readableResearchMarkdown(value:unknown,names:ReadonlyMap<string,string>=new Map()):string {
  let text=String(value||'').trim();
  if(!text)return '';
  // Older projections flattened and cut Markdown. Recover complete rows only.
  if(!/\n\s*\|/.test(text)){
    const table=/(\|[^\n]+?\|)\s+(\|(?:\s*:?-{3,}:?\s*\|)+)/.exec(text);
    if(table&&table.index!==undefined){
      const width=table[1].split('|').length-2;
      const tail=text.slice(table.index+table[0].length).trim();
      const rows=tail.split(/\|\s*\|/).map(row=>row.replace(/^\||\|$/g,'').trim()).filter(Boolean);
      const complete=rows.filter(row=>row.split('|').length===width);
      text=text.slice(0,table.index).trim()+'\n\n'+table[1]+'\n'+table[2]+'\n'+complete.map(row=>'| '+row+' |').join('\n');
      if(complete.length<rows.length)text+='\n\n部分历史摘要已被截断，请到原始会话核对完整内容。';
    }
  }
  const titles=new Map(names);
  const lines=text.split(/\r?\n/);
  const cells=(line:string)=>line.trim().replace(/^\||\|$/g,'').split('|').map(v=>v.trim());
  for(const line of lines){
    if(!line.trim().startsWith('|'))continue;
    const row=cells(line);
    if(/^`?exp-/.test(row[0]||'')&&row[1])titles.set(row[0].replace(/`/g,''),row[1].replace(/`/g,''));
  }
  const reference=(id:string)=>{
    if(titles.has(id))return titles.get(id)!;
    if(id==='research_workspace')return '研究工作区';
    const [prefix,suffix='']=id.split(/…|\.\.\./);
    const matches=[...titles].filter(([key])=>key.startsWith(prefix)&&key.endsWith(suffix));
    if(matches.length===1)return matches[0][1];
    if (/^rev[_-]/.test(id))return '已保存版本';
    if (/^(page|canvas)[_-]/.test(id))return '交互界面';
    return id.startsWith('hyp-')?'假说（未记录名称）':'相关记录（未记录名称）';
  };
  // Drop a redundant table column containing only machine experiment references.
  for(let i=0;i<lines.length-1;i++){
    if(!lines[i].trim().startsWith('|')||!/^\|?\s*:?-{3}/.test(lines[i+1].trim()))continue;
    let end=i+2;while(end<lines.length&&lines[end].trim().startsWith('|'))end++;
    if(end>i+2&&lines.slice(i+2,end).every(line=>/^`?exp-/.test(cells(line)[0]||'')))for(let j=i;j<end;j++)lines[j]=`| ${cells(lines[j]).slice(1).join(' | ')} |`;
    i=end-1;
  }
  text=lines.join('\n').replace(/`?\b(?:hyp|exp|rs|evt|event|session|run|research|activity|finding|conflict|episode|asset|claim|rev|page|canvas)[_-][a-zA-Z0-9…._-]+`?/g,id=>reference(id.replace(/`/g,'')));
  for(const [id,title] of titles)text=text.split(id).join(title);
  const labels:Record<string,string>={canvas:'交互画布',clay:'粘土拟态',research_workspace:'研究工作区',list_projects:'查看课题',list_research_branches:'查看实验分支',parent_experiment_id:'上级实验',hypothesis_id:'关联假说',revision:'版本',status:'状态',planned:'待执行',completed:'已完成',frozen:'已冻结',testing:'验证中',active:'进行中',inconclusive:'尚无定论',null:'无'};
  text=text.replace(/`?value="([^"]*)"`?/g,'默认值：$1').replace(/select\/input\/toggle/g,'选择、输入和开关');
  return text.replace(/\b(canvas|clay|research_workspace|list_projects|list_research_branches|parent_experiment_id|hypothesis_id|revision|status|planned|completed|frozen|testing|active|inconclusive|null)\b/g,v=>labels[v]);
}

/** Index excerpts may contain serialized records instead of readable source text. */
export function readableResearchExcerpt(value:unknown,structured=false):string {
  const text=String(value||'');
  if (structured || /对象类型：|```json|^\s*\{\s*"/.test(text)) {
    const match=/"(?:preview|summary|statement|description|text|title)"\s*:\s*("(?:[^"\\]|\\.)*")/.exec(text);
    if(match){try{return readableResearchMarkdown(JSON.parse(match[1]));}catch{}}
    return '结构化研究资料；请点击“查看完整资料”阅读中文字段说明。';
  }
  return readableResearchMarkdown(text);
}
export function researchIndexTitle(value:unknown,fallback='研究资料'):string {
  const text=String(value||'').trim();
  return !text||/^(?:event|experience|experiment|hypothesis|finding|activity|paper|canvas|page|wiki)[ ·|>]/i.test(text)?fallback:readableResearchMarkdown(text);
}
