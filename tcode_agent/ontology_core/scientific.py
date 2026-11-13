"""Source-bound scientific definitions on the existing entity/qualifier model."""
import re
import math
from decimal import Decimal


ENTITY_FIELDS = ('key', 'type', 'label', 'quote', 'external_ids', 'identity_scope', 'aliases', 'mutations')
MUTATION = re.compile(r'[ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY]')
TRANSITION_STATE = re.compile(r'\bTS\d+\b|transition[ -]state|过渡态', re.I)


def evidence_text(text):
    text = re.sub(r'(?<=[a-z])-\n(?=[a-z])', '', text)
    return ' '.join(text.replace('-\n', '-').split())



SENTENCE_BOUNDARY = re.compile(r'(?<=[.!?。])\s+(?=[A-Z\u4e00-\u9fff])')


def observation_source_quote(quote, source, *, preceding=False):
    """Complete a selected line range to source sentences, never paraphrase it."""
    start = source.find(quote)
    if start < 0:
        return quote  # The shared normalizer rejects non-source quotes.
    end = start + len(quote)
    starts, right = [0], len(source)
    for boundary in SENTENCE_BOUNDARY.finditer(source):
        if boundary.end() <= start:
            starts.append(boundary.end())
        if boundary.start() >= end:
            right = boundary.start()
            break
    return source[starts[max(0, len(starts) - 1 - int(preceding))]:right]



def entity_issue(entity):
    label, kind, quote = entity['label'], entity['type'], entity['quote']
    if kind == 'Pathway' and not named_in(entity, quote):
        return 'pathway_source_name_required'
    if kind == 'MechanisticIntermediate' and TRANSITION_STATE.search(label):
        return 'transition_state_type_required'
    if kind == 'Protein' and (entity.get('mutations') or re.search(r'\bmutant\b|\bvariant\b|-M\d+\b', label, re.I) or MUTATION.search(label)):
        return 'enzyme_variant_type_required'
    for alias in entity.get('aliases', []):
        if re.fullmatch(r'(?:it|this|that|our|the) (?:approach|strategy|step|reaction|route|pathway|enzyme|mutant|selectivity)|it', alias.strip(), re.I):
            return 'referential_alias_requires_reference_protocol'
        normalized_quote = evidence_text(quote)
        if evidence_text(alias) not in normalized_quote:
            return 'alias_source_evidence_required'
    if kind in {'Protein', 'ProteinGroup', 'EnzymeVariant'}:
        for identifier in entity.get('external_ids', {}).values():
            if identifier.casefold() not in quote.casefold():
                return 'protein_identifier_source_evidence_required'
    for mutation in entity.get('mutations', []):
        if not MUTATION.fullmatch(mutation) or not re.search(r'(?<![A-Za-z0-9])' + re.escape(mutation) + r'(?![A-Za-z0-9])', quote):
            return 'mutation_source_evidence_required'
    return None



NUMBER_WORDS = dict(zip(('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty').split(), range(21)))


def source_numbers(text):
    # A unit exponent (M−1, s^-1) is not a positive measured baseline of 1.
    for number in re.findall(r'(?<![A-Za-z0-9+.^-])[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', text.replace('−', '-')):
        yield Decimal(number)
    for word in re.findall(r'\b[a-z]+\b', text.lower()):
        if word in NUMBER_WORDS:
            yield Decimal(NUMBER_WORDS[word])


def named_in(entity, text):
    def normalized(value):
        value = evidence_text(value)
        if entity['type'] == 'CatalyticCapability':
            value = value.replace('’', "'")
        if entity['type'] in {'Pathway', 'Reaction'}:
            # Only an explicitly parenthesized source acronym and typographic dashes.
            value = re.sub(r'\(([A-Z][A-Z0-9-]+)\)', r'\1', value)
            value = re.sub(r'[\s]*[-−–][\s]*', '', value)
        return value
    return any(re.search(r'(?<!\w)' + re.escape(normalized(name)) + r'(?!\w)', normalized(text), re.I)
               for name in [entity['label'], *entity.get('aliases', [])])


def construct_named_in(entity, text):
    """A hyphenated construct name does not also name its parent."""
    return any(re.search(r'(?<![\w-])' + re.escape(evidence_text(name)) + r'(?![\w-])', text, re.I)
               for name in [entity['label'], *entity.get('aliases', [])])


def capability_issue(assertion, subject, obj, peers):
    if assertion['predicate'] != 'supports_capability':
        return None
    if any(field in assertion['qualifiers'] for field in ('metric', 'value', 'unit', 'comparison_value', 'fold_change')):
        return 'capability_measurement_requires_observation'
    others = [p for p in peers if p['type'] in {'Protein', 'EnzymeVariant', 'ProteinGroup'}
              and p.get('entity_id', p['key']) != subject.get('entity_id', subject['key'])]
    for sentence in SENTENCE_BOUNDARY.split(evidence_text(assertion['quote'])):
        if not construct_named_in(subject, sentence) or not named_in(obj, sentence):
            continue
        evolution = re.search(r'\b(?:directed evolution|engineering)\b.*?\binto\b', sentence, re.I)
        owner_text = sentence[evolution.end():] if evolution else sentence
        if construct_named_in(subject, owner_text) and not any(construct_named_in(p, owner_text) for p in others):
            return None
    return 'capability_subject_source_binding_required'


def membership_issue(assertion, subject, obj, peers):
    """A class annotation must describe this construct, not its neighbour/descendant."""
    if assertion['predicate'] != 'member_of':
        return None
    quote = evidence_text(assertion['quote'])
    competitors = [p for p in peers if p['type'] in {'Protein', 'EnzymeVariant', 'ProteinGroup'}
                   and p.get('entity_id', p['key']) not in
                   {subject.get('entity_id', subject['key']), obj.get('entity_id', obj['key'])}]
    named = construct_named_in

    def competing(text):
        return any(named(p, text) for p in competitors)

    sentences = SENTENCE_BOUNDARY.split(quote)
    if any(named(subject, sentence) and named(obj, sentence)
           and not competing(sentence) for sentence in sentences):
        return None
    # A single engineering-to-naming narrative may state the class before the
    # resulting name. No other construct in that narrative may inherit it.
    if (subject['type'] == 'EnzymeVariant' and named(subject, quote) and named(obj, quote)
            and not competing(quote)):
        for index, sentence in enumerate(sentences):
            naming = re.search(r'\b(?:designated|named|termed|called)\b', sentence, re.I)
            if (naming and named(subject, sentence[naming.end():])
                    and any(named(obj, earlier) and re.search(r'\brepurpose|\bengineer', earlier, re.I)
                            for earlier in sentences[:index])):
                return None
    return 'membership_subject_class_binding_required'


def reference_phrases(subject, scope):
    allowed = {
        'pathway': {'our approach', 'this approach', 'this strategy', 'our strategy', 'this route', 'the route', 'this pathway', 'the entire pathway', 'this synthetic route'},
        'reaction': {'this reaction', 'the reaction', 'this step', 'the step', 'this process'},
        'entity': {'it', 'this enzyme', 'the enzyme', 'this mutant', 'the final mutant', 'this selectivity'},
    }
    if subject.get('identity_scope') == 'unresolved' and re.match(r'the one with ', subject['label'], re.I) and re.search(r'\bselected\b', subject.get('quote', ''), re.I):
        allowed['entity'].add('this approach')
    if subject['type'] == 'TransitionState':
        allowed['entity'] |= {'this step', 'the transition state', 'this transition state', 'the corresponding transition state'}
    return allowed[scope]


def reference_issue(reference, index, sentences, subject, peers, scope, *, antecedent_bound=False):
    if reference not in reference_phrases(subject, scope) or not index:
        return 'observation_reference_unresolved'
    if not re.search(r'(?<!\w)' + re.escape(reference) + r'(?!\w)', sentences[index][:120], re.I):
        return 'observation_reference_not_in_result'
    if reference in {'this step', 'the step'} and re.search(r'\b(overall|entire|whole|complete)\b', subject['label'], re.I):
        return 'observation_step_not_overall_reaction'
    antecedent = sentences[index - 1]
    if not antecedent_bound and not named_in(subject, antecedent):
        return 'observation_reference_antecedent_required'
    combination = re.search(r'\bcombin(?:ing|e)\b.*?\bwith\b', antecedent, re.I)
    if scope == 'pathway' and combination and (named_in(subject, antecedent[combination.end():])
            or named_in(subject, antecedent[combination.start():combination.end()])):
        return 'observation_reference_combination_not_component'
    kinds = {'Protein', 'EnzymeVariant', 'ProteinGroup'} if subject['type'] in {'Protein', 'EnzymeVariant', 'ProteinGroup'} else {subject['type']}
    for other in peers:
        if other.get('entity_id', other['key']) != subject.get('entity_id', subject.get('key')) and other['type'] in kinds and (named_in(other, antecedent) or named_in(other, sentences[index])):
            return 'observation_reference_ambiguous'
    return None



def reference_chain_issue(chain, index, sentences, subject, peers, scope):
    if not isinstance(chain, list) or not 1 <= len(chain) <= 4 or index < len(chain):
        return 'observation_reference_chain_invalid'
    for offset, phrase in enumerate(chain):
        if not isinstance(phrase, str):
            return 'observation_reference_chain_invalid'
        position = index - len(chain) + offset + 1
        issue = reference_issue(phrase.casefold(), position, sentences, subject, peers, scope,
                                antecedent_bound=offset > 0)
        if issue:
            return issue
    return None


def route_reference_bindings(entities, source, assertions=()):
    """Resolve a local referential route name to a uniquely named source antecedent."""
    bindings, questions = {}, []
    for entity in entities:
        if entity['type'] != 'Pathway' or entity['label'].casefold() not in reference_phrases(entity, 'pathway'):
            continue
        phrase = entity['label'].casefold()
        strategies = {a['qualifiers']['procedure_strategy'] for a in assertions
                      if a['subject_key'] == entity['key'] and isinstance(a['qualifiers'].get('procedure_strategy'), str)}
        quote = observation_source_quote(entity['quote'], source, preceding=True)
        sentences = SENTENCE_BOUNDARY.split(evidence_text(quote))
        peers = [e for e in entities if e['key'] != entity['key']]
        matches = []
        for candidate in peers:
            if candidate['type'] != 'Pathway' or candidate['label'].casefold() in reference_phrases(candidate, 'pathway'):
                continue
            if strategies and not all(named_in(candidate, name) for name in strategies):
                continue
            if any(reference_issue(phrase, i, sentences, candidate, peers, 'pathway') is None for i in range(len(sentences))):
                matches.append(candidate)
        identities = {e['entity_id'] for e in matches}
        if len(identities) == 1:
            target = matches[0]
            bindings[entity['key']] = target
            questions.append({'code': 'resolved_route_reference', 'original_key': entity['key'],
                              'canonical_key': target['key'], 'reference': phrase, 'quote': quote})
    return bindings, questions



DETAIL_FIELDS = ('method', 'structure_id', 'role', 'reference_state', 'operation',
                 'after_phase', 'timing', 'background', 'structure_template', 'procedure_strategy')


def observation_detail_issue(assertion, subject):
    data, quote = assertion['qualifiers'], evidence_text(assertion['quote'])
    for field in DETAIL_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, str) or not value.strip():
            return 'observation_detail_invalid:' + field
        if field == 'procedure_strategy':
            found = named_in({'label': value, 'type': 'Pathway'}, quote)
        else:
            found = evidence_text(value) in quote
        if not found:
            return 'observation_detail_not_in_source:' + field
    for field in ('participants', 'sites'):
        values = data.get(field, [])
        if not isinstance(values, list) or len(values) > 30 or any(not isinstance(v, str) or not v.strip() or evidence_text(v) not in quote for v in values):
            return 'observation_detail_not_in_source:' + field
    # Template similarity/application does not assign the template's model method.
    for field in ('method', 'structure_id'):
        if field not in data:
            continue
        owners = [re.split(r'\b(?:nearly identical|similar to|also applicable to|compared with)\b', sentence, maxsplit=1, flags=re.I)[0]
                  for sentence in SENTENCE_BOUNDARY.split(quote)]
        if not any(named_in(subject, owner) and evidence_text(data[field]) in owner for owner in owners):
            return 'observation_model_owner_required:' + field
    return None


def observation_issue(assertion, subject, obj, *, peers=()):
    if assertion['predicate'] != 'has_observation':
        return None
    if subject['type'] == 'Pathway' and subject['label'].casefold() in reference_phrases(subject, 'pathway'):
        return 'observation_reference_unresolved'
    data = assertion['qualifiers']
    detail_issue = observation_detail_issue(assertion, subject)
    if detail_issue:
        return detail_issue
    if not isinstance(data.get('metric'), str) or not data['metric'].strip():
        return 'observation_metric_required'
    for field in ('value', 'comparison_value', 'fold_change'):
        if field != 'value' and data.get(field) is None:
            continue
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return 'observation_value_required'
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return 'observation_value_must_be_finite'
            if Decimal(str(value)) not in source_numbers(assertion['quote']):
                return 'observation_value_not_in_source'
        elif re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', value.strip()):
            return 'observation_numeric_value_must_be_number'
        elif not value.strip() or evidence_text(value) not in evidence_text(assertion['quote']):
            return 'observation_value_not_in_source'
    if (data.get('comparison_value') is not None or data.get('fold_change') is not None) and not data.get('comparison_target'):
        return 'observation_comparison_target_required'
    if data.get('comparison_target') and evidence_text(str(data['comparison_target'])) not in evidence_text(assertion['quote']):
        return 'observation_comparison_target_not_in_source'
    if data.get('operator') in {'up_to', 'up to'} and not re.search(r'up to|at most|最多|不超过', assertion['quote'], re.I):
        return 'observation_scope_not_in_source'
    scope = 'pathway' if subject['type'] == 'Pathway' else 'reaction' if subject['type'] == 'Reaction' else 'entity'
    if data.get('observation_scope', scope) != scope:
        return 'observation_subject_scope_mismatch'
    # ponytail: references need a unique adjacent antecedent at every bounded hop.
    # Implicit or ambiguous links stay quarantined for review.
    sentences = SENTENCE_BOUNDARY.split(evidence_text(assertion['quote']))
    reference = data.get('subject_reference')
    chain = data.get('subject_reference_chain')
    for index, sentence in enumerate(sentences):
        if chain is not None:
            if reference_chain_issue(chain, index, sentences, subject, peers, scope):
                continue
        elif reference:
            if not isinstance(reference, str):
                return 'observation_reference_unresolved'
            error = reference_issue(reference.casefold(), index, sentences, subject, peers, scope)
            if error:
                continue
        elif not named_in(subject, sentence):
            continue
        # Explicit whole-route aggregates cannot become one enzyme/reaction result.
        if scope != 'pathway' and re.search(r'(?:(?:entire|overall|whole|complete)\s+(?:synthetic\s+)?(?:route|synthesis)|longest linear sequence)', sentence, re.I) and re.search(r'steps?|sequence|products?', str(data['metric']), re.I):
            return 'observation_route_scope_required'
        value = data['value']
        if isinstance(value, (int, float)):
            bound = Decimal(str(value)) in source_numbers(sentence)
        else:
            bound = evidence_text(value) in sentence
        if bound:
            if data.get('operation') and (evidence_text(data['operation']) not in sentence or any(evidence_text(p) not in sentence for p in data.get('participants', []))):
                return 'observation_operation_participant_binding_required'
            upper = re.search(r'\bup to\s+([0-9]+(?:\.[0-9]+)?)', sentence, re.I)
            if upper and isinstance(value, (int, float)) and Decimal(upper[1]) == Decimal(str(value)) and data.get('operator') not in {'up_to', 'up to'}:
                return 'observation_upper_bound_operator_required'
            combination = re.search(r'\bcombin(?:ing|e)\b.*?\bwith\b', sentence, re.I)
            if scope == 'pathway' and combination and named_in(subject, sentence[combination.end():]) and re.search(r'steps?|sequence|products?|yield', data['metric'], re.I):
                return 'observation_combination_not_component'
            # A historical comparator in the same sentence is not this subject's result.
            comparison = re.search(r'\b(?:fewer than|more than|compared (?:with|to)|versus)\b', sentence, re.I)
            if comparison and named_in(subject, sentence[:comparison.start()]):
                before, after = sentence[:comparison.start()], sentence[comparison.end():]
                if isinstance(value, (int, float)):
                    misplaced = Decimal(str(value)) in source_numbers(after) and Decimal(str(value)) not in source_numbers(before)
                else:
                    misplaced = evidence_text(value) in after and evidence_text(value) not in before
                if misplaced:
                    return 'observation_comparator_cannot_be_primary'
            return None
    return 'observation_reference_unresolved' if reference or chain is not None else 'observation_subject_value_binding_required'




def resolve_route_comparison(assertion, subject):
    """Fill an explicit route step comparison, keeping the old range as a comparator."""
    if subject['type'] != 'Pathway' or not re.search(r'steps?|sequence', str(assertion['qualifiers'].get('metric', '')), re.I):
        return assertion
    data = assertion['qualifiers']
    if any(data.get(k) is not None for k in ('comparison_target', 'comparison_value', 'comparison_unit')):
        return assertion
    text = evidence_text(assertion['quote'])
    matches = list(re.finditer(r'\b(fewer than|more than)\s+(?:the\s+)?(?P<value>\d+(?:\s*(?:to|–|-)\s*\d+)?)\s+steps\s+reported in\s+(?P<target>previous work|prior work|previous studies)', text, re.I))
    if len(matches) != 1:
        return assertion
    match = matches[0]
    primary = data.get('value')
    prefix = SENTENCE_BOUNDARY.split(text[:match.start()])[-1]
    if not isinstance(primary, (int, float)) or isinstance(primary, bool) or Decimal(str(primary)) not in source_numbers(prefix) or not named_in(subject, prefix):
        return assertion
    value = match['value']
    qualifiers = {**data, 'comparison_value': int(value) if value.isdigit() else value,
                  'comparison_target': match['target'], 'comparison_unit': 'steps',
                  'comparison_operator': match[1].lower().replace(' ', '_')}
    return {**assertion, 'qualifiers': qualifiers}


def resolve_fold_comparison(assertion, subject, peers=()):
    """Normalize a misplaced ratio only when the source spells out that ratio."""
    data = assertion['qualifiers']
    ratio, primary = data.get('comparison_value'), data.get('value')
    if (data.get('comparison_unit', '').casefold() != 'fold'
            or not isinstance(ratio, (int, float)) or isinstance(ratio, bool)
            or not isinstance(primary, (int, float)) or isinstance(primary, bool)
            or data.get('fold_change') is not None or not data.get('comparison_target')):
        return assertion
    pattern = r'(?P<ratio>\d+(?:\.\d+)?)\s*[-−–]?\s*fold\s+(?:higher than|increase over|above|over)\s+(?P<target>[^.;]+)'
    matches = []
    for sentence in SENTENCE_BOUNDARY.split(evidence_text(assertion['quote'])):
        for match in re.finditer(pattern, sentence, re.I):
            before = sentence[:match.start()]
            if (Decimal(match['ratio']) != Decimal(str(ratio)) or not named_in(subject, before)
                    or Decimal(str(primary)) not in source_numbers(before)
                    or not named_in({'label': data['comparison_target'], 'type': 'Protein'}, match['target'])):
                continue
            competing = [p for p in peers if p['type'] in {'Protein', 'EnzymeVariant', 'ProteinGroup'}
                         and p.get('entity_id', p['key']) != subject.get('entity_id', subject['key'])
                         and not any(name.startswith(p['label'] + '-') for name in [subject['label'], *subject.get('aliases', [])])
                         and named_in(p, before)]
            if not competing:
                matches.append(match)
    if len(matches) != 1:
        return assertion
    return {**assertion, 'qualifiers': {**{k: v for k, v in data.items() if k not in ('comparison_value', 'comparison_unit')},
                                       'fold_change': ratio}}


def resolve_source_strategy_name(entity, assertions, source, peers):
    """Use an already supplied, source-proven strategy name, never invent one."""
    if entity['type'] != 'Pathway':
        return entity
    named = (named_in(entity, entity['quote'])
             and entity['label'].casefold() not in reference_phrases(entity, 'pathway'))
    choices = {}
    for claim in assertions:
        data = claim['qualifiers']
        name = data.get('procedure_strategy')
        if (claim['subject_key'] != entity['key'] or claim['predicate'] != 'has_observation'
                or not isinstance(name, str) or not name.strip() or claim['quote'] not in source):
            continue
        # A corrected full source name and its literal short form must not
        # become two competing routes. Never replace a different named subject.
        if named and not named_in(entity, name):
            continue
        quote = observation_source_quote(claim['quote'], source)
        candidate = {**entity, 'label': name, 'quote': quote}
        # The name is proved against a directly named result, independent of a
        # possibly wrong pronoun in that draft. The full claim is validated later.
        direct = {**claim, 'quote': quote, 'qualifiers': {k: v for k, v in data.items()
                  if k not in ('subject_reference', 'subject_reference_chain')}}
        if observation_issue(direct, candidate, {}, peers=peers) is not None:
            continue
        sentences = SENTENCE_BOUNDARY.split(evidence_text(quote))
        if any(p['key'] != entity['key'] and p['type'] == 'Pathway'
               and p['label'].casefold() not in reference_phrases(p, 'pathway')
               and any(named_in(candidate, s) and named_in(p, s) for s in sentences) for p in peers):
            continue
        choices.setdefault(name, candidate)
    return next(iter(choices.values())) if len(choices) == 1 else entity


def strategy_correction_candidates(assertion, subject, obj, peers):
    """Suggest a source-proven name; a wrong named endpoint remains rejected."""
    name = assertion['qualifiers'].get('procedure_strategy')
    if subject['type'] != 'Pathway' or not isinstance(name, str) or name == subject['label']:
        return []
    proposed = {**subject, 'label': name, 'aliases': []}
    if not named_in(proposed, assertion['quote']) or observation_issue(assertion, proposed, obj, peers=peers):
        return []
    return [{'label': name, 'type': 'Pathway', 'quote': assertion['quote'],
             'instruction': 'Declare this source-named strategy under a NEW key for its own outcomes. Do not redefine the existing different named endpoint. Recheck the complete claim.'}]


def reference_correction_candidates(assertion, peers):
    """Suggest a unique proven antecedent; never silently rebind a named subject."""
    if assertion['predicate'] != 'has_observation' or not assertion['qualifiers'].get('subject_reference'):
        return []
    matches = {p['entity_id']: p for p in peers if p['type'] == 'Pathway'
               and observation_issue(assertion, p, {}, peers=peers) is None}
    if len(matches) != 1:
        return []
    peer = next(iter(matches.values()))
    return [{'subject_key': peer['key'], 'label': peer['label'], 'quote': assertion['quote'],
             'subject_reference': assertion['qualifiers']['subject_reference']}]


def resolve_observation_reference(assertion, subject, obj, source, peers):
    """Record only references that pass the same explicit-source validator."""
    issue = observation_issue(assertion, subject, obj, peers=peers)
    if issue != 'observation_subject_value_binding_required' or assertion['qualifiers'].get('subject_reference'):
        return assertion
    scope = 'pathway' if subject['type'] == 'Pathway' else 'reaction' if subject['type'] == 'Reaction' else 'entity'
    quote = observation_source_quote(assertion['quote'], source, preceding=True)
    for reference in sorted(reference_phrases(subject, scope)):
        candidate = {**assertion, 'quote': quote, 'qualifiers': {**assertion['qualifiers'], 'subject_reference': reference}}
        if observation_issue(candidate, subject, obj, peers=peers) is None:
            return candidate
    return assertion


def extend_schema(root):
    """Additive, immutable platform release; never edits old bundles or facts."""
    from .system_admin import active, schema, apply_change_set
    root, manifest = active(root)
    ontology = schema(root)
    classes = {row['id']: row for row in ontology['classes']}
    predicates = {row['id']: row for row in ontology['predicates']}
    additions = [
        {'id': 'TransitionState', 'label': '过渡态', 'parent': 'Entity'},
        {'id': 'ResearchObservation', 'label': '研究观察', 'parent': 'Entity'},
    ]
    changed = False
    for row in additions:
        if row['id'] not in classes:
            classes[row['id']] = row
            changed = True
    expected = [{'id': 'has_observation', 'label': '研究观察',
                 'domains': [key for key in ('Entity', 'Document', 'Protein', 'ProteinGroup', 'EnzymeVariant',
                                            'Reaction', 'Pathway', 'ChemicalEntity', 'TransitionState') if key in classes],
                 'ranges': ['ResearchObservation']}]
    if 'Reaction' in classes:
        expected.append({'id': 'has_transition_state', 'label': '经过渡态',
                         'domains': ['Reaction'], 'ranges': ['TransitionState']})
    for row in expected:
        if row['id'] not in predicates:
            predicates[row['id']] = row
            changed = True
    if not changed:
        return
    apply_change_set(root, {'expected_release_id': manifest['release_id'],
                           'operations': [{'op': 'update_schema'}],
                           'ontology': {**ontology, 'classes': list(classes.values()),
                                        'predicates': list(predicates.values())}}, publish=True)
