"""Conservative checks of declared structured statements, never prose entailment."""
import json


BASES = ('experimental', 'computational', 'literature_report', 'hypothesis')
from .conditions import METADATA_QUALIFIERS, typed_equal, compare_conditions


def validate_claims(claims):
    if not isinstance(claims, list) or not 1 <= len(claims) <= 100:
        raise ValueError('provide 1..100 claims')
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) - {'text', 'assertion_ids', 'statement', 'required_evidence_basis'}:
            raise ValueError('invalid claim fields')
        if not isinstance(claim.get('text'), str) or not 1 <= len(claim['text'].strip()) <= 4000:
            raise ValueError('each claim needs bounded text')
        references = claim.get('assertion_ids')
        if not isinstance(references, list) or not 1 <= len(references) <= 20 or not all(isinstance(ref, str) and 1 <= len(ref) <= 500 for ref in references):
            raise ValueError('each claim needs 1..20 explicit assertion references')
        if claim.get('required_evidence_basis', 'any') not in ('any', *BASES):
            raise ValueError('invalid required evidence basis')
        statement = claim.get('statement')
        if statement is None:
            continue
        if not isinstance(statement, dict) or set(statement) - {'subject_id', 'predicate', 'object_id', 'polarity', 'qualifiers', 'kind'}:
            raise ValueError('invalid structured statement')
        for key in ('subject_id', 'predicate', 'object_id'):
            if not isinstance(statement.get(key), str) or not 1 <= len(statement[key].strip()) <= 500:
                raise ValueError('statement requires subject_id, predicate and object_id')
        if statement.get('polarity', 'supports') not in ('supports', 'opposes') or statement.get('kind', 'reported') not in ('reported', 'hypothesis'):
            raise ValueError('invalid statement polarity/kind')
        qualifiers = statement.get('qualifiers', {})
        if not isinstance(qualifiers, dict) or len(qualifiers) > 20 or any(not isinstance(key, str) or not 1 <= len(key) <= 100 for key in qualifiers):
            raise ValueError('statement requires bounded qualifier conditions')
        if METADATA_QUALIFIERS & qualifiers.keys():
            raise ValueError('evidence requirements belong in required_evidence_basis, not statement conditions')
        try:
            encoded = json.dumps(qualifiers, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError('statement conditions must be finite JSON values') from exc
        if len(encoded) > 8000:
            raise ValueError('statement conditions too long')


def check_evidence(statement, fact, requirement):
    reasons = []
    relation_match = all(statement[key] == fact.get(key) for key in ('subject_id', 'predicate', 'object_id'))
    conditions = statement.get('qualifiers', {})
    recorded = {key: value for key, value in fact.get('qualifiers', {}).items() if key not in METADATA_QUALIFIERS}
    mismatch = [key for key, value in conditions.items() if key in recorded and not typed_equal(value, recorded[key])]
    missing = sorted(conditions.keys() - recorded.keys())
    omitted = sorted(recorded.keys() - conditions.keys())
    basis = fact.get('qualifiers', {}).get('evidence_basis', fact.get('evidence_basis', 'unspecified'))
    basis = basis if basis in BASES else 'unspecified'
    kind = fact.get('kind', fact.get('epistemic_kind', 'unspecified'))
    if not relation_match:
        reasons.append('entity_or_relation_mismatch')
    if mismatch:
        reasons.append('different_conditions')
    if missing:
        reasons.append('required_conditions_missing')
    if omitted:
        reasons.append('claim_omits_recorded_conditions')
    if basis == 'unspecified':
        reasons.append('evidence_basis_unspecified')
    if requirement != 'any' and basis != requirement:
        reasons.append('evidence_requirement_not_met')
    if kind in ('hypothesis', 'analogy', 'annotation', 'curator_inference', 'unspecified') and statement.get('kind', 'reported') != 'hypothesis':
        reasons.append('inference_cannot_be_promoted_to_reported_fact')
    if basis == 'hypothesis' and statement.get('kind', 'reported') != 'hypothesis':
        reasons.append('hypothesis_cannot_establish_reported_fact')
    if not isinstance(fact.get('quote'), str) or not fact['quote'].strip() or not fact.get('locator'):
        reasons.append('source_quote_or_locator_missing')
    if fact.get('conflict_state') == 'open':
        reasons.append('unresolved_source_conflict')
    applicable = relation_match and not mismatch and not missing
    condition_relation = compare_conditions(conditions, recorded)
    opposing = relation_match and {fact.get('polarity','supports'),statement.get('polarity','supports')} == {'supports','opposes'}
    opposite = opposing and condition_relation == 'same_conditions'
    if opposing and condition_relation == 'unknown_conditions':
        reasons.append('opposite_conditions_unknown')
    if opposite:
        reasons.append('opposite_polarity')
    status = 'conflict' if opposite else 'undetermined' if not applicable else 'partial_support' if reasons else 'supported'
    return {'assertion_id': fact['id'], 'scope': fact['scope'], 'status': status, 'reasons': reasons,
            'evidence_basis': basis, 'kind': kind, 'missing_conditions': missing,
            'condition_relation':condition_relation, 'opposing':opposing,
            'conflict_categories':([condition_relation] if opposing else []) + (['source_dispute'] if fact.get('conflict_state') == 'open' else []),
            'different_conditions': mismatch, 'omitted_conditions': omitted,
            'recorded_conditions': recorded, 'requested_conditions': conditions,
            'quote': fact.get('quote', ''), 'locator': fact.get('locator', {}), 'source_id': fact.get('source_id', ''),
            'source_revision': fact.get('source_revision'), 'evidence_ids': fact.get('evidence_ids', []),
            'evidence_refs': fact.get('evidence_refs', [])}


def assess_claim(claim, evidence, related, truncated=False):
    statement = claim.get('statement')
    if not statement:
        return {'status': 'undetermined', 'assessment_scope': 'structured_statement_only',
                'text_consistency_checked': False, 'reasons': ['structured_statement_required'],
                'evidence_checks': [], 'counterevidence': [], 'conflict_scan_truncated': False}
    requirement = claim.get('required_evidence_basis', 'any')
    checks = [check_evidence(statement, fact, requirement) for fact in evidence]
    counterevidence = [check_evidence(statement, fact, requirement) for fact in related]
    counterevidence = [check for check in counterevidence if check['opposing']]
    statuses = {check['status'] for check in checks}
    reasons = []
    if any(check['status']=='conflict' for check in counterevidence) or 'conflict' in statuses:
        status = 'conflict'
        reasons.append('opposing_evidence_requires_review')
    elif 'supported' in statuses:
        # Any unrelated/weak citation must be removed or justified before this can be clean.
        status = 'supported' if statuses == {'supported'} else 'partial_support'
    elif 'partial_support' in statuses:
        status = 'partial_support'
    else:
        status = 'undetermined'
    if any(check['condition_relation']=='unknown_conditions' for check in counterevidence):
        reasons.append('opposite_conditions_unknown')
        if status == 'supported':
            status = 'partial_support'
    if truncated:
        reasons.append('conflict_scan_incomplete')
        if status == 'supported':
            status = 'partial_support'
    reasons = list(dict.fromkeys([*reasons, *(reason for check in checks for reason in check['reasons'])]))
    return {'status': status, 'assessment_scope': 'structured_statement_only', 'text_consistency_checked': False,
            'reasons': reasons, 'evidence_checks': checks, 'counterevidence': counterevidence,
            'conflict_scan_truncated': truncated}
