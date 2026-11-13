export const SUPPORT_LABELS: Record<string,string> = {
  supported:'支持（仅限结构化事实）', partial_support:'部分支持', conflict:'存在冲突', undetermined:'无法判断',
};

const REASONS: Record<string,string> = {
  structured_statement_required:'缺少明确的结论对象、关系和条件，无法核对自由文本。',
  entity_or_relation_mismatch:'引用对应的实体、变体或关系与结论不同，不能直接迁移。',
  different_conditions:'证据记录了不同条件，不能据此支持当前条件下的结论。',
  required_conditions_missing:'证据未记录结论要求的条件，不能假定满足。',
  claim_omits_recorded_conditions:'结论遗漏了证据中的条件，可能过度推广。',
  evidence_basis_unspecified:'证据类型未标明，不能提升为实验事实。',
  evidence_requirement_not_met:'证据类型不满足结论要求；计算或转述不能代替实验。',
  inference_cannot_be_promoted_to_reported_fact:'类比、标注或推断不能直接升级为已报道事实。',
  hypothesis_cannot_establish_reported_fact:'未验证假设不能作为已报道事实的证明。',
  source_quote_or_locator_missing:'缺少可追溯原文或位置，需要补充来源。',
  unresolved_source_conflict:'该来源仍有未解决的争议，需要人工复核。',
  opposite_polarity:'相同对象与适用条件下存在相反主张。',
  opposite_conditions_unknown:'存在相反主张，但条件不足，无法确认是否适用；不能作为完整支持。',
  opposing_evidence_requires_review:'发现相反证据，结论需要人工复核。',
  conflict_scan_incomplete:'相反证据检查达到上限，不能宣称已排除冲突。',
};
export const supportReason = (code:string) => REASONS[code] || `需要人工核对：${code}`;

export function statementConditions(rows:Array<{key:string;value:string}>):Record<string,unknown> {
  const entries:Array<[string,unknown]> = [];
  const seen=new Set<string>();
  for (const row of rows) {
    const key=row.key.trim(), raw=row.value.trim();
    if (!key&&!raw) continue;
    if (!key||!raw) throw new Error('结论条件需要填写名称和值');
    if (seen.has(key)) throw new Error(`结论条件 ${key} 重复`);
    if (['evidence_basis','primary_reference'].includes(key)) throw new Error('请在证据要求中指定类型，不要将来源元信息作为结论条件');
    let value:unknown;
    try {value=JSON.parse(raw);} catch {value=raw;}
    if (value===null || (typeof value==='number'&&!Number.isFinite(value))) throw new Error('结论条件不能使用空值或非有限数字');
    entries.push([key,value]);seen.add(key);
  }
  return Object.fromEntries(entries);
}
