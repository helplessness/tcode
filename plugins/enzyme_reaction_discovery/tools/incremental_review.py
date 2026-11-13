"""本运行内的审核继承：内容、依赖、原文和规则均不变才可复用；不是长期记忆。"""
import copy
import hashlib
from pathlib import Path

from .retrieval_tools import canonical

COLLECTIONS = {
    'mechanism': ('02_mechanisms.json', 'mechanism_hypotheses', 'hypothesis_id', 'mechanism_reviews'),
    'candidate': ('04_candidates.json', 'enzyme_candidates', 'candidate_id', 'candidate_reviews'),
    'route': ('05_routes.json', 'routes', 'route_id', 'route_dispositions')}


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def policy_digest():
    """审核提示、契约或执行校验改变时，旧结论失效，而非只看版本号。"""
    root = Path(__file__).resolve().parents[1]
    prompt = root/'prompts/critical_report.md'
    if not prompt.exists():
        prompt = root/'workflows/mechanism-guided-enzyme-search/prompts/critical_report.md'
    paths = [prompt, *sorted((root/'schemas').glob('*.schema.json'))]
    paths += [path for path in (root/'graph.py',root/'working_memory.py') if path.exists()]
    paths += [Path(__file__).with_name(name) for name in
              ('incremental_review.py', 'audit_tools.py', 'review_scope.py', 'scientific_checks.py',
               'reasoning_policy.py', 'definitions.py')]
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def evidence_state(store, snapshot):
    from .audit_tools import actual_references, reviewed_card_read_refs
    evidence = {}
    for stage in ('mechanism', 'screen'):
        record = store._read_json_if_exists(store._run_path(snapshot['run']['run_id'], f'retrieval_{stage}.json')) or {}
        if record.get('knowledge_fingerprint') == snapshot['run'].get('knowledge_fingerprint'):
            allowed = set(record.get('selected_refs', [])) | set(record.get('inherited_refs', []))
            evidence.update({key: value for key, value in record.get('evidence_snapshots', {}).items() if key in allowed})
    owners = actual_references(snapshot['documents'], reviewed_card_read_refs(store, snapshot))
    return owners, evidence


def global_binding(snapshot):
    docs, run = snapshot['documents'], snapshot['run']
    # 反应、辅因子能力或机制共享定义变化，保守地重新完整审核。
    return digest({'run_id': run['run_id'], 'knowledge': run.get('knowledge_fingerprint'),
        'input': run.get('reaction_input'), 'reaction': docs.get('01_reaction_analysis.json'),
        'capabilities': docs.get('03_capabilities.json'),
        'mechanism_shared': {k: v for k, v in docs.get('02_mechanisms.json', {}).items()
                             if k not in ('mechanism_hypotheses', 'evidence_ledger')}})


def object_index(documents, owners, evidence):
    """实际依赖＋同组比较参与失效传播；共享 Wiki 引用本身不建立依赖。"""
    rows, aliases = {}, {}
    for kind, (file, field, key, _) in COLLECTIONS.items():
        for item in documents.get(file, {}).get(field, []):
            identifier = item[key]
            if identifier in rows:
                raise ValueError('ambiguous review object ID')
            rows[identifier] = {'kind': kind, 'file': file, 'item': item, 'deps': set()}
            for alias in {identifier, item.get('proposal_id'), item.get('card_id'), *item.get('card_ids', [])} - {None, ''}:
                aliases.setdefault(alias, set()).add(identifier)
    def resolve(value):
        return aliases.get(value, set())
    for identifier, row in rows.items():
        item = row['item']
        if any(ref not in rows for ref in item.get('mechanism_ids', [])):
            raise ValueError('unresolved mechanism dependency')
        row['deps'].update(ref for ref in item.get('mechanism_ids', []) if ref in rows)
        for step in item.get('steps', []):
            if ((step.get('candidate_id') and len(resolve(step['candidate_id'])) != 1)
                    or any(ref not in rows for ref in step.get('mechanism_ids', []))):
                raise ValueError('unresolved route dependency')
            row['deps'].update(resolve(step.get('candidate_id')))
            row['deps'].update(ref for ref in step.get('mechanism_ids', []) if ref in rows)
        for factor in item.get('ranking_factors', []):
            for peer in factor.get('comparison', {}).get('peer_ids', []):
                row['deps'].update(resolve(peer))
        row['direct_deps'] = set(row['deps'])
        if row['kind'] in ('candidate', 'route'):
            def group(other):
                role = other['item'].get('candidate_role', 'primary')
                return (other['kind'], role, other['item'].get('component_function', 'other') if role == 'route_component' else '')
            row['deps'].update(key for key, other in rows.items() if group(other) == group(row))
    ledgers = {}
    for file, document in documents.items():
        for statement in document.get('evidence_ledger', []):
            for key in (statement['statement_id'], file+':'+statement['statement_id']):
                ledgers.setdefault(key, []).append(statement)
    def strings(value):
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict):
            return set().union(*(strings(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(strings(child) for child in value))
        return set()
    def closure(ids, field='deps'):
        found, pending = set(), list(ids)
        while pending:
            key = pending.pop()
            if key in found or key not in rows:
                continue
            found.add(key)
            pending.extend(rows[key][field] - found)
        return found
    for identifier, row in rows.items():
        dependencies = closure([identifier])
        refs, statements = set(), {}
        for key in dependencies:
            source = rows[key]
            owner = ('mechanism:' if source['kind'] == 'mechanism' else 'route:' if source['kind'] == 'route' else '') + key
            refs.update(owners.get(owner, []))
            pending = list(strings(source['item']))
            visited = set()
            while pending:
                value = pending.pop()
                if value in visited:
                    continue
                visited.add(value)
                if value in evidence:
                    refs.add(value)
                for statement in ledgers.get(value, []):
                    statements[digest(statement)] = statement
                    pending.extend(strings(statement) - visited)
        row.update(closure=dependencies, context_closure=closure([identifier],'direct_deps'), refs=refs,
            fingerprint=digest({'objects': {key: rows[key]['item'] for key in sorted(dependencies)},
                                'order':[key for key in rows if key in dependencies],
                                'ledger': statements, 'evidence': {ref: evidence.get(ref) for ref in sorted(refs)}}))
    return rows, aliases


def load_basis(store, snapshot, basis_id=None, bind_active=True):
    """损坏、规则变化、知识变化或跨运行缓存均回退全审，不继承通过状态。"""
    from .audit_tools import audit_digest, audit_context_complete, require_review_consistency
    run_id = snapshot['run']['run_id']
    try:
        if basis_id is None:
            basis_id = (store._read_json_if_exists(store._run_path(run_id, 'review_checkpoint.json')) or {}).get('basis_id')
        if not isinstance(basis_id, str) or len(basis_id) != 64 or any(c not in '0123456789abcdef' for c in basis_id):
            return None
        record = store._read_json_if_exists(store._run_path(run_id, f'.review-bases/{basis_id}.json'))
        if not record or digest(record) != basis_id or record.get('policy') != policy_digest():
            return None
        if record.get('reviewer') != store._read_json_if_exists(store._run_path(run_id,'review_runtime.json')):
            return None
        old = {'run': record['run'], 'documents': record['documents']}
        if global_binding(old) != global_binding(snapshot):
            return None
        review, proof = record['review'], record['audit_proof']
        if review.get('review_decision') not in ('PASS','PARTIAL_PASS'):
            return None
        expected = set().union(*(set(refs) for refs in record['owners'].values()))
        if (review.get('audited_upstream_digest') != audit_digest(old) or proof.get('digest') != audit_digest(old)
                or not audit_context_complete(proof) or not expected <= set(proof.get('delivered', []))
                or not expected <= record['evidence'].keys()):
            return None
        if bind_active:
            active = store._read_json_if_exists(store._run_path(run_id, '06_critique.json'))
            if active != review:
                return None
        require_review_consistency(old['documents'], review['review_decision'], review['critique'])
        return record
    except (KeyError, TypeError, ValueError, OSError):
        return None


def save_basis(store, snapshot, review):
    from .audit_tools import require_complete_audit
    require_complete_audit(store, snapshot)
    owners, evidence = evidence_state(store, snapshot)
    reviewer=store._read_json_if_exists(store._run_path(snapshot['run']['run_id'],'review_runtime.json'))
    if reviewer and reviewer.get('policy_digest') != policy_digest():
        raise ValueError('review policy changed during model execution; do not cache old-policy review')
    proof = store._read_json_if_exists(store._run_path(snapshot['run']['run_id'], 'report_evidence_audit.json'))
    if review.get('review_decision') not in ('PASS','PARTIAL_PASS'):
        return None
    record = {'policy': policy_digest(), 'run': snapshot['run'], 'documents': {k:v for k,v in snapshot['documents'].items() if k.startswith(('01_','02_','03_','04_','05_'))},
        'review': review, 'owners': owners, 'evidence': evidence,
        'reviewer':reviewer,
        'audit_proof': {key: proof[key] for key in ('digest', 'context_protocol', 'context_delivered', 'context_total', 'delivered')}}
    if proof.get('incremental_review'):
        record['audit_proof']['incremental_review']=copy.deepcopy(proof['incremental_review'])
    basis_id = digest(record)
    store._atomic_write(store._run_path(snapshot['run']['run_id'], f'.review-bases/{basis_id}.json'), canonical(record)+'\n')
    store._atomic_write(store._run_path(snapshot['run']['run_id'], 'review_checkpoint.json'), canonical({'basis_id': basis_id})+'\n')
    return basis_id


def review_plan(store, snapshot):
    from .review_scope import scoped_review
    old = load_basis(store, snapshot)
    if not old:
        return None
    owners, evidence = evidence_state(store, snapshot)
    try:
        current, aliases = object_index(snapshot['documents'], owners, evidence)
        prior, _ = object_index(old['documents'], old['owners'], old['evidence'])
    except ValueError:
        return None
    states = {row['object_id']: row for row in scoped_review(old['documents'], old['review']['critique'])['objects']}
    reused = {key for key, row in current.items() if key in prior and
              row['fingerprint'] == prior[key]['fingerprint'] and states[key]['status'] in ('retained', 'excluded')}
    # 旧式缺少逐项处置的记录不推定已逐项审核。
    reviews = {}
    for kind, (_, _, key, field) in COLLECTIONS.items():
        reviews.update({row[key]: row for row in old['review']['critique'].get(field, [])})
    reused &= reviews.keys()
    dirty = current.keys() - reused
    removed = prior.keys() - current.keys()
    context = set().union(*(current[key]['context_closure'] for key in dirty))
    required_refs = set().union(*(current[key]['refs'] for key in context))
    all_refs = set().union(*(set(refs) for refs in owners.values()))
    inherited = {ref for ref in all_refs - required_refs if ref in evidence and
                 old['evidence'].get(ref) == evidence[ref] and ref in old['audit_proof']['delivered']}
    required_refs |= all_refs - inherited
    def shared(docs):
        return {file: {key: value for key, value in doc.items()
                      if key not in {'run_id','schema_version','evidence_ledger',*(r[1] for r in COLLECTIONS.values())}}
                for file, doc in docs.items()}
    before_shared, after_shared = shared(old['documents']), shared(snapshot['documents'])
    metadata = {file: {'before': before_shared.get(file), 'after': value} for file, value in after_shared.items()
                if before_shared.get(file) != value}
    if not reused:
        return None
    return {'old': old, 'objects': current, 'aliases': aliases, 'reused': set(reused), 'dirty': set(dirty),
        'context': context, 'required_refs': required_refs, 'inherited_refs': inherited,
        'removed':{key:prior[key]['item'] for key in sorted(removed)},
        'removed_evidence':{ref:old['evidence'][ref] for key in removed for ref in prior[key]['refs'] if ref in old['evidence']},
        'metadata_changes': metadata,
        'proof': {'basis_id': digest(old), 'reused_ids': sorted(reused), 'dirty_ids': sorted(dirty),
                  'context_ids': sorted(context), 'inherited_refs': sorted(inherited),
                  'removed_ids':sorted(removed),
                  'current_evidence_digest':digest({ref:evidence.get(ref) for ref in sorted(all_refs)})}}


def stable_issue(issue, plan):
    """原审核未知／反证说明只有绑定对象、来源和处置背景不变才继承。"""
    affected = issue.get('affected_ids', [])
    resolved = set()
    for value in affected:
        targets = plan['aliases'].get(value, set())
        if not targets:
            # 无实体的卡片处置按其实际证据所有者定位，而不是全文字符串猜测。
            targets = {key for key, row in plan['objects'].items() if
                       any(ref.startswith(value+':') for ref in row['refs'])}
        if not targets:
            return False
        resolved.update(targets)
    if not resolved or not resolved <= plan['reused']:
        return False
    # 共享取舍元数据改变时，旧的来源中立性／排除说明需重新审核。
    guarded = ('candidate_generation', 'read_card_reviews', 'mechanism_feedback')
    return not any((change.get('before') or {}).get(key) != (change.get('after') or {}).get(key)
                   for change in plan['metadata_changes'].values() for key in guarded)


def merge_review(plan, actions):
    """模型只交变化部分；服务端合并已验证未变处置，完整科学门控仍运行。"""
    result = copy.deepcopy(actions)
    handled_removals={ref for issue in actions.get('evidence_gaps',[]) if issue.get('statement','').strip()
                      for ref in issue.get('affected_ids',[])}
    if not {'removed-object:'+key for key in plan['removed']} <= handled_removals:
        raise ValueError('incremental review must explain removed objects in evidence_gaps using removed-object:<ID>')
    for kind, (_, _, key, field) in COLLECTIONS.items():
        fresh = {row[key]: row for row in actions.get(field, [])}
        if len(fresh) != len(actions.get(field, [])):
            raise ValueError('duplicate incremental review ID')
        required = {identifier for identifier in plan['dirty'] if plan['objects'][identifier]['kind'] == kind}
        if not required <= fresh.keys():
            raise ValueError('incremental review missing changed/dependent objects: '+', '.join(sorted(required-fresh.keys())))
        inherited = {row[key]: row for row in plan['old']['review']['critique'].get(field, []) if row[key] in plan['reused']}
        inherited.update(fresh)
        result[field] = list(inherited.values())
    for field in ('hard_conflicts','evidence_gaps'):
        old_issues = [copy.deepcopy(row) for row in plan['old']['review']['critique'].get(field, []) if stable_issue(row, plan)]
        result[field] = list({digest({k:v for k,v in row.items() if k!='issue_id'}):row
                              for row in [*old_issues,*actions.get(field, [])]}.values())
        for i, row in enumerate(result[field],1):
            row['issue_id'] = f'{field}-{i}'
    for field, kind in (('excluded_candidate_ids','candidate'),('excluded_mechanism_ids','mechanism')):
        _,_,key,reviews_field = COLLECTIONS[kind]
        overridden = {row[key] for row in actions.get(reviews_field,[])}
        result[field] = sorted(set(result.get(field,[])) | ((set(plan['old']['review']['critique'].get(field,[])) & plan['reused'])-overridden))
    return result


def audit_projection(snapshot, base, plan):
    """完整变化项＋只读依赖＋共享元数据差异；未变对象给哈希索引并允许按 ID 回读。"""
    documents = {}
    for kind, (file, field, _, _) in COLLECTIONS.items():
        documents[file] = {field:[row['item'] for key,row in plan['objects'].items()
                                 if key in plan['dirty'] and row['kind']==kind]}
    # 账本正文按稳定条目差异交付；不能用候选行不变掩盖证据陈述变化。
    ledgers = {}
    for file, document in snapshot['documents'].items():
        old = {row['statement_id']:row for row in plan['old']['documents'].get(file,{}).get('evidence_ledger',[])}
        new = {row['statement_id']:row for row in document.get('evidence_ledger',[])}
        changes = {key:{'before':old.get(key),'after':new.get(key)} for key in old.keys()|new.keys() if old.get(key)!=new.get(key)}
        if changes:
            ledgers[file]=changes
    projected = copy.deepcopy(base)
    # 完整提名历史已在元数据差异和冻结产物中，不能每轮另抄一遍。
    projected.pop('candidate_generation',None)
    projected.pop('revision_targets',None)
    from .scientific_checks import scientific_review
    projected['scientific_checks'] = {
        'changes':structural_changes(scientific_review(plan['old']['documents']),base.get('scientific_checks',{})),
        'rule':'全量确定性检查仍执行；这里仅交付结果变化，未变的已审结果由指纹继承。'}
    for field, kind in (('mechanisms','mechanism'),('candidates','candidate'),('routes','route')):
        key = COLLECTIONS[kind][2]
        projected[field] = [row for row in projected.get(field,[]) if row.get(key) in plan['context']]
    projected['incremental_review'] = {**plan['proof'], 'mode':'incremental',
        'reused_dispositions':[{key:row[key] for key in ('object_id','object_type','name','status')}
            for row in plan['old']['review'].get('review_summary',{}).get('objects',[]) if row['object_id'] in plan['reused']],
        'instruction':'只审核 dirty_ids 及共享变化；依赖对象作为只读上下文。其余处置由服务端按内容、证据和规则指纹继承。可通过工作记忆按 ID 回读；发现旧项新问题时可显式覆写该对象处置，缓存不豁免科学检查。'}
    previous = {row[COLLECTIONS[kind][2]]:row for kind,(file,field,_,_) in COLLECTIONS.items()
                for row in plan['old']['documents'].get(file,{}).get(field,[])}
    shared_changes = {file:structural_changes(value.get('before'),value.get('after'))
                      for file,value in plan['metadata_changes'].items()}
    return projected, {'changed_objects':documents,'shared_changes':shared_changes,
        'removed_objects':plan['removed'],
        'removed_objects_previous_evidence':plan['removed_evidence'],
        'ledger_changes':ledgers, 'object_changes':{key:structural_changes(previous.get(key),plan['objects'][key]['item'])
                                                  for key in sorted(plan['dirty'])},
        'readonly_dependency_ids':sorted(plan['context']-plan['dirty']),
        'dependency_rule':'只读依赖的旧审核已由指纹验证；导航摘要保留机制、冲突与引用索引，需核对原件时按 ID 回读当前产物。'}


def structural_changes(before, after, path=''):
    """只省略逐值相等的字段；变更、删除、空值和数组顺序均完整保留。"""
    if before == after:
        return []
    if isinstance(before,dict) and isinstance(after,dict):
        changes=[]
        for key in sorted(before.keys() | after.keys()):
            pointer=path+'/'+str(key).replace('~','~0').replace('/','~1')
            if key in before and key in after:
                changes.extend(structural_changes(before[key],after[key],pointer))
            else:
                changes.append({'path':pointer,'before_present':key in before,'after_present':key in after,
                                **({'before':before[key]} if key in before else {}),
                                **({'after':after[key]} if key in after else {})})
        return changes
    if isinstance(before,list) and isinstance(after,list) and len(before)==len(after):
        return [change for i,(old,new) in enumerate(zip(before,after))
                for change in structural_changes(old,new,path+'/'+str(i))]
    return [{'path':path,'before':before,'after':after}]


def revision_projection(store, snapshot, context):
    """局部筛选主动交付待改原件、相关机制和封存原文，避免靠恢复旧游标找材料。"""
    from .audit_tools import audit_digest
    old = load_basis(store, snapshot)
    if (not old or old['review'].get('review_decision') != 'PARTIAL_PASS'
            or old['review'].get('audited_upstream_digest') != audit_digest(snapshot)
            or any(task['scope'] != 'screening' for task in old['review']['critique'].get('revision_tasks', []))):
        return context
    owners, evidence = evidence_state(store, snapshot)
    try:
        rows, _ = object_index(snapshot['documents'], owners, evidence)
    except ValueError:
        return context
    editable = {row['object_id'] for row in old['review']['review_summary']['objects'] if row['status']=='needs_revision'}
    relevant = set().union(*(rows[key]['context_closure'] for key in editable if key in rows))
    mechanisms = {key for key in relevant if rows[key]['kind']=='mechanism'}
    refs = set().union(*(rows[key]['refs'] for key in editable if key in rows))
    if not refs <= evidence.keys():
        return context  # 不把缺少原文的假缓存当作已交接。
    keep = ('run_id','context_digest','result_limits','max_route_steps','candidate_review_rules','commit_receipt','submission_policy','revision_context',
            'completion_tasks','read_card_comparison_tasks','staged_progress')
    result = {key:copy.deepcopy(context[key]) for key in keep if key in context}
    result.update(context_delivery='local_revision', reaction=snapshot['documents']['01_reaction_analysis.json']['reaction_analysis'],
        mechanisms=[rows[key]['item'] for key in sorted(mechanisms)],
        comparison_context=[rows[key]['item'] for key in sorted(relevant-editable-mechanisms)],
        current_items=[rows[key]['item'] for key in sorted(editable) if key in rows],
        evidence_snapshots={ref:evidence[ref] for ref in sorted(refs)},
        incremental_revision={'editable_ids':sorted(editable),'preserved_count':len(rows)-len(editable),
            'upstream_digest':digest({name:snapshot['documents'][name] for name in
                                      ('01_reaction_analysis.json','02_mechanisms.json','03_capabilities.json')}),
            'basis_id':digest(old),'instruction':'本包已交付本轮待改对象、只读比较对象、相关机制和所需封存原文；直接小批暂存待改对象并 use_staged 提交。不要重读无关机制或复用旧 delivery_cursor；需要补证据时使用正常 context 工具，所有科学校验保持执行。'})
    staged = store._read_json_if_exists(store._run_path(snapshot['run']['run_id'],'screening_items.draft.json')) or {}
    aliases = {key for key in editable}
    aliases.update(value for key in editable if key in rows for value in
                   (rows[key]['item'].get('card_id'),rows[key]['item'].get('proposal_id')) if value)
    result['editable_staged_candidates'] = [row for key,row in staged.get('candidates',{}).items() if key in aliases]
    peer_ids = {ref for key in editable if key in rows for ref in rows[key]['deps']
                if rows[ref]['kind']=='candidate'} - editable
    result['comparison_peer_index'] = [{key:row['item'][key] for key in
        ('candidate_id','enzyme_name','candidate_role','component_function','chemistry_fit_score','score','mechanism_ids','ranking_reason','verification_note')
        if key in row['item']} for identifier,row in rows.items() if identifier in peer_ids]
    result['candidate_generation'] = {'mode':'reuse_frozen_draft_for_local_revision'}
    return result
