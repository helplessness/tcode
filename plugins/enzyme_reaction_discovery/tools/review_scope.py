"""逐对象审核与依赖传播；不推断酶活性、不修改科学分数。"""
import copy

from .scientific_checks import scientific_review


def object_review_schema(key):
    return {'type': 'array', 'maxItems': 30, 'items': {'type': 'object',
        'required': [key, 'disposition', 'reason', 'evidence_refs'], 'properties': {
            key: {'type': 'string', 'minLength': 1},
            'disposition': {'enum': ['retain', 'demote', 'revise', 'exclude']},
            'reason': {'type': 'string', 'minLength': 1, 'maxLength': 500},
            'evidence_refs': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string'}}},
        'additionalProperties': False}}


def aggregate_review(documents, decision, critique, validate=True):
    """局部可交付必须通过原有科学检查；不能把全局错误洗成部分通过。"""
    from .audit_tools import require_review_consistency
    pending = documents.get('04_candidates.json', {}).get('unassessed_read_cards', [])
    if pending and decision in {'PASS', 'PARTIAL_PASS'}:
        # 未执行的筛选任务不是化学否定，也不能由审核的一句覆盖限制洗成已完成。
        instruction = ('补齐已回读但未比较的知识卡：' + '、'.join(pending)
                       + '。按共享事件、目标缺口与保留候选同尺度比较，提交 read_card_reviews.comparison；'
                         '可提名、不选或诚实暂缓，不要求任何酶入选，不新增检索或重试。')[:800]
        if instruction not in critique.setdefault('required_revisions', []):
            critique['required_revisions'].append(instruction)
        task = {'scope': 'screening', 'affected_ids': pending[:8], 'instruction': instruction}
        if task not in critique.setdefault('revision_tasks', []):
            critique['revision_tasks'].append(task)
        decision = 'REVISE'
    summary = scoped_review(documents, critique)
    if decision == 'PARTIAL_PASS':
        try:
            require_review_consistency(documents, 'PARTIAL_PASS', critique)
        except ValueError as exc:
            # 部分放行不成立时收紧为修订，保留全部问题；不让状态标签纠错消耗模型轮次。
            summary['partial_delivery_blocked'] = str(exc)
            decision = 'REVISE'
    if decision == 'REVISE' and summary['can_partially_deliver'] and not summary.get('partial_delivery_blocked'):
        try:
            require_review_consistency(documents, 'PARTIAL_PASS', critique)
        except ValueError as exc:
            summary['partial_delivery_blocked'] = str(exc)
        else:
            decision = 'PARTIAL_PASS'
    if validate or decision == 'PARTIAL_PASS':
        require_review_consistency(documents, decision, critique)
    return decision, summary


def scoped_review(documents, critique):
    """化学判断与排序判断分轴；只有实际依赖者继承待修订状态。"""
    groups = {
        'mechanism': ('02_mechanisms.json', 'mechanism_hypotheses', 'hypothesis_id'),
        'candidate': ('04_candidates.json', 'enzyme_candidates', 'candidate_id'),
        'route': ('05_routes.json', 'routes', 'route_id')}
    rows, aliases, originals = {}, {}, {}
    for kind, (file, field, key) in groups.items():
        for item in documents.get(file, {}).get(field, []):
            identifier = item[key]
            originals[identifier] = item
            rows[identifier] = {'object_id': identifier, 'object_type': kind,
                'name': item.get('enzyme_name') or item.get('required_capability') or item.get('summary') or identifier,
                'assessment': 'unreviewed', 'ranking': 'clear', 'depends_on': [],
                'affected_by': [], 'reasons': []}
            for alias in {identifier, item.get('proposal_id'), item.get('card_id'), *item.get('card_ids', [])} - {None, ''}:
                aliases.setdefault(alias, set()).add(identifier)
    global_reasons = []

    def resolve(identifier):
        if identifier in aliases:
            return aliases[identifier]
        if identifier in documents:
            return {key for key in rows if groups[rows[key]['object_type']][0] == identifier}
        return set()

    def task_targets(identifiers):
        # 同一任务列出实体与文件时，文件是编辑容器，不是其全部对象的失败声明。
        # 只有旧式纯文件任务才回退到整份产物；真正的文件级科学冲突仍由 issue 传播。
        specific = set().union(*(resolve(value) for value in identifiers if value not in documents))
        return specific or set().union(*(resolve(value) for value in identifiers))

    for kind, field, key in [('candidate', 'candidate_reviews', 'candidate_id'),
                             ('mechanism', 'mechanism_reviews', 'hypothesis_id'),
                             ('route', 'route_dispositions', 'route_id')]:
        seen = set()
        for item in critique.get(field, []):
            identifier = item[key]
            if identifier not in rows or rows[identifier]['object_type'] != kind or identifier in seen:
                raise ValueError(f'unknown or duplicate {kind} review ID: {identifier}')
            seen.add(identifier)
            if item['disposition'] not in {'retain', 'demote', 'revise', 'exclude'} or (kind != 'route' and not item.get('reason', '').strip()):
                raise ValueError('object review requires valid disposition and reason')
            rows[identifier]['assessment'] = {'retain': 'retained', 'demote': 'retained', 'revise': 'revise', 'exclude': 'excluded'}[item['disposition']]
            if item.get('reason'):
                rows[identifier]['reasons'].append(item['reason'])
    for identifier in [*critique.get('excluded_candidate_ids', []), *critique.get('excluded_mechanism_ids', [])]:
        if identifier not in rows:
            raise ValueError('review exclusion references an unknown object')
        rows[identifier]['assessment'] = 'excluded'

    # 旧审核的retain路线仅可承接实际催化者和机制的审核，不推广到其他未审候选。
    for identifier, review in rows.items():
        item = originals[identifier]
        dependencies = set()
        if review['object_type'] == 'candidate':
            dependencies.update(item.get('mechanism_ids', []))
        if review['object_type'] == 'route':
            for step in item.get('steps', []):
                dependencies.update(step.get('mechanism_ids', []))
                if step.get('candidate_id'):
                    dependencies.update(resolve(step['candidate_id']))
            dependencies.update(item.get('mechanism_ids', []))
        review['depends_on'] = sorted(dependencies & rows.keys())
    for review in rows.values():
        if review['object_type'] == 'route' and review['assessment'] == 'retained':
            pending = list(review['depends_on'])
            visited = set()
            while pending:
                dependency = pending.pop()
                if dependency in visited:
                    continue
                visited.add(dependency)
                if rows[dependency]['assessment'] == 'unreviewed':
                    rows[dependency]['assessment'] = 'retained'
                pending.extend(rows[dependency]['depends_on'])

    def flag(ids, axis, reason):
        for identifier in ids:
            rows[identifier][axis] = 'pending' if axis == 'ranking' else (
                'excluded' if rows[identifier]['assessment'] == 'excluded' else 'revise')
            if reason not in rows[identifier]['reasons']:
                rows[identifier]['reasons'].append(reason)

    for issue in [*critique.get('hard_conflicts', []), *critique.get('evidence_gaps', [])]:
        from .audit_tools import review_issue_type
        kind = issue.get('issue_type') or (review_issue_type(issue) if issue in critique.get('hard_conflicts', []) else 'evidence_gap')
        if kind == 'evidence_gap':
            continue  # 可保留的科学未知不是失败，也不是实测活性证明。
        concrete = set().union(*(resolve(value) for value in issue.get('affected_ids', [])))
        unknown = [value for value in issue.get('affected_ids', []) if not resolve(value)]
        if kind == 'audit_delivery' or not concrete or unknown or '01_reaction_analysis.json' in issue.get('affected_ids', []):
            global_reasons.append(issue.get('statement') or '审核问题未定位，不能部分放行')
        if kind == 'audit_delivery':
            continue  # 交付故障不能当作某条化学路线失败。
        flag(concrete, 'ranking' if kind == 'ranking_inconsistency' else 'assessment', issue.get('statement', kind))
    for task in critique.get('revision_tasks', []):
        concrete = task_targets(task.get('affected_ids', []))
        unknown = any(not resolve(value) for value in task.get('affected_ids', []))
        if task.get('scope') in {'reaction', 'delivery', 'audit'} or not concrete or unknown:
            global_reasons.append(task['instruction'])
        if task.get('scope') in {'delivery', 'audit'}:
            continue
        for identifier in concrete:
            # 已明确归为排序修订的对象，不把它再变成化学不通过。
            if rows[identifier]['ranking'] != 'pending':
                flag({identifier}, 'assessment', task['instruction'])
    computed = scientific_review(documents)
    for finding in computed.get('ranking_consistency', []):
        if finding['status'] in {'conflict', 'incomplete'}:
            flag(resolve(finding['id']), 'ranking', '程序检测到排序比较方向或分组不一致')
    for finding in computed.get('stoichiometry', []):
        if finding['status'] == 'conflict':
            flag(resolve(finding['route_id']), 'assessment', '程序检测到计量冲突')
    for identifier, item in originals.items():
        if item.get('hard_conflict'):
            flag({identifier}, 'assessment', '候选存在已声明的明确冲突')

    changed = True
    while changed:
        changed = False
        for identifier, review in rows.items():
            for dependency in review['depends_on']:
                source = rows[dependency]
                for axis in ('assessment', 'ranking'):
                    if source[axis] not in {'revise', 'excluded', 'pending'}:
                        continue
                    before = review[axis]
                    flag({identifier}, axis, f'依赖对象 {dependency} 的' + ('排序' if axis == 'ranking' else '机制／证据判断') + '待处理')
                    review['affected_by'] = sorted(set(review['affected_by']) | {dependency})
                    changed |= before != review[axis]
    for review in rows.values():
        review['status'] = ('excluded' if review['assessment'] == 'excluded' else 'needs_revision'
            if review['assessment'] == 'revise' or review['ranking'] == 'pending' else
            'retained' if review['assessment'] == 'retained' else 'unreviewed')
    retained_routes = [row['object_id'] for row in rows.values() if row['object_type'] == 'route' and row['status'] == 'retained']
    pending = [row['object_id'] for row in rows.values() if row['status'] in {'needs_revision', 'unreviewed'}]
    if critique.get('required_revisions') and not any(row['status'] == 'needs_revision' for row in rows.values()):
        global_reasons.append('修订意见尚未定位到具体问题对象，不能自动部分放行')
    return {'objects': list(rows.values()), 'retained_route_ids': retained_routes,
        'pending_ids': pending, 'global_reasons': list(dict.fromkeys(global_reasons)),
        'can_partially_deliver': bool(retained_routes) and not global_reasons}


def retained_projection(documents, critique, summary):
    """仅为校验构建无问题对象视图；从不修改原产物或偷偷改分。"""
    clean = copy.deepcopy(documents)
    keep = {row['object_id'] for row in summary['objects'] if row['status'] == 'retained'}
    review = copy.deepcopy(critique)
    review.update(required_revisions=[], revision_tasks=[], excluded_candidate_ids=[], excluded_mechanism_ids=[])
    review['excluded_candidate_ids'] = [row['object_id'] for row in summary['objects'] if row['object_type'] == 'candidate' and row['object_id'] not in keep]
    review['excluded_mechanism_ids'] = [row['object_id'] for row in summary['objects'] if row['object_type'] == 'mechanism' and row['object_id'] not in keep]
    review['route_dispositions'] = [row if row['route_id'] in keep else {**row, 'disposition': 'exclude'}
                                    for row in review.get('route_dispositions', [])]
    covered = {row['route_id'] for row in review['route_dispositions']}
    review['route_dispositions'].extend({'route_id': row['route_id'], 'disposition': 'exclude',
        'reason': '仅在未受影响对象的校验视图中隔离待办，不修改原审核处置', 'evidence_refs': []}
        for row in clean.get('05_routes.json', {}).get('routes', []) if row['route_id'] not in covered)
    for field, key in [('candidate_reviews', 'candidate_id'), ('mechanism_reviews', 'hypothesis_id')]:
        review[field] = [row for row in review.get(field, []) if row[key] in keep]
    for field in ('hard_conflicts', 'evidence_gaps'):
        review[field] = [row for row in review.get(field, []) if set(row.get('affected_ids', [])) <= keep]
    return clean, review


def preserve_unaffected_screening(previous, review, candidates, routes, *, review_policy_current=True):
    """部分通过后的筛选修订只替换问题对象；无关产物逐字结构保留。"""
    if review.get('review_decision') != 'PARTIAL_PASS':
        return candidates, routes, {'applied': False}
    if not review_policy_current:
        return candidates, routes, {'applied': False, 'reason': 'review_policy_changed_requires_fresh_review'}
    from .audit_tools import audit_digest
    if review.get('audited_upstream_digest') != audit_digest({'documents': previous}):
        if any(task.get('scope') in {'reaction', 'mechanism'} for task in review.get('critique', {}).get('revision_tasks', [])):
            # 上游已按明确任务重建时，旧筛选快照不能充当新机制的审核凭据。
            return candidates, routes, {'applied': False, 'reason': 'upstream_changed_requires_fresh_review'}
        raise ValueError('local revision requires a current upstream audit; do not freeze objects using a stale review')
    summary = scoped_review(previous, review.get('critique', {}))
    allowed = {row['object_id'] for row in summary['objects'] if row['status'] == 'needs_revision'}
    receipt = {'applied': True, 'editable_ids': sorted(allowed), 'preserved_ids': [], 'ignored_unrelated_edits': []}
    all_ledgers = {row['statement_id']: row for file in ('04_candidates.json', '05_routes.json')
                   for row in previous.get(file, {}).get('evidence_ledger', [])}
    def references(value):
        if isinstance(value, str):
            # 账本引用兼容裸 statement_id 与带产物名的稳定引用。
            identifier = value.split(':', 1)[1] if value.startswith(('04_candidates.json:', '05_routes.json:')) else value
            return {identifier} & all_ledgers.keys()
        if isinstance(value, dict):
            return set().union(*(references(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(references(child) for child in value))
        return set()
    preserved_rows = [row for file, field, key in (('04_candidates.json', 'enzyme_candidates', 'candidate_id'),
                      ('05_routes.json', 'routes', 'route_id')) for row in previous.get(file, {}).get(field, []) if row[key] not in allowed]
    protected = references(preserved_rows)
    pending = list(protected)
    while pending:
        linked = references(all_ledgers[pending.pop()]) - protected
        protected.update(linked)
        pending.extend(linked)
    editable_refs = references([row for output, field, key in ((candidates, 'enzyme_candidates', 'candidate_id'),
                               (routes, 'routes', 'route_id')) for row in output[field] if row[key] in allowed])
    outputs = []
    for file, field, key, output in [('04_candidates.json', 'enzyme_candidates', 'candidate_id', candidates),
                                    ('05_routes.json', 'routes', 'route_id', routes)]:
        result = copy.deepcopy(output)
        existing = {row[key]: row for row in result[field]}
        old_rows = previous.get(file, {}).get(field, [])
        for row in old_rows:
            identifier = row[key]
            if identifier not in allowed:
                if identifier in existing and existing[identifier] != row:
                    receipt['ignored_unrelated_edits'].append(identifier)
                existing[identifier] = copy.deepcopy(row)
                receipt['preserved_ids'].append(identifier)
        result[field] = list(existing.values())
        old_ledger = {row['statement_id']: row for row in previous.get(file, {}).get('evidence_ledger', [])}
        new_ledger = {row['statement_id']: row for row in result.get('evidence_ledger', [])}
        local_protected = protected & old_ledger.keys()
        if any(identifier in editable_refs and identifier in new_ledger and new_ledger[identifier] != old_ledger[identifier] for identifier in local_protected):
            raise ValueError('local revision collides with preserved evidence IDs; keep stable statement references')
        new_ledger.update({identifier: copy.deepcopy(old_ledger[identifier]) for identifier in local_protected})
        if 'evidence_ledger' in result or new_ledger:
            result['evidence_ledger'] = list(new_ledger.values())
        outputs.append(result)
    return *outputs, receipt
