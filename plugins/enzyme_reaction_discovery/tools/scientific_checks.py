"""通用的声明式计量核算与排序依据检查；不含反应模板、酶名或目标答案。"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
import math
import re

from jsonschema import Draft202012Validator


VECTOR_SCHEMA = {"type": "object", "minProperties": 1, "maxProperties": 40,
                 "additionalProperties": {"type": "number", "minimum": -1000000, "maximum": 1000000}}
STOICHIOMETRY_SCHEMA = {
    "type": "object", "required": ["species", "steps"],
    "properties": {
        "species": {"type": "object", "minProperties": 1, "maxProperties": 40,
                    "propertyNames": {"minLength": 1, "maxLength": 80},
                    "additionalProperties": {"type": "string", "maxLength": 256}},
        "charges": {"type": "object", "maxProperties": 40,
                    "additionalProperties": {"type": "integer", "minimum": -100, "maximum": 100}},
        "steps": {"type": "array", "minItems": 1, "maxItems": 8, "items": VECTOR_SCHEMA},
        "net": VECTOR_SCHEMA,
        "basis": {"type": "string", "maxLength": 600},
    }, "additionalProperties": False,
    "description": "可选计量账本。species 是稳定物种名到分子式的映射，未知分子式用空串；steps 按路线顺序给各物种有符号系数（消耗负、生成正），net 可给总反应。charges 仅填写确知的电荷。不填写不代表配平，程序不推测或自动修改系数。",
}
BALANCE_PROPOSALS_SCHEMA = {"type": "object", "maxProperties": 3,
                            "additionalProperties": STOICHIOMETRY_SCHEMA}
STEP_EQUATION_SCHEMA = {
    "type": "object", "required": ["species", "coefficients"],
    "properties": {"species": STOICHIOMETRY_SCHEMA["properties"]["species"],
                   "coefficients": VECTOR_SCHEMA,
                   "charges": STOICHIOMETRY_SCHEMA["properties"]["charges"]},
    "additionalProperties": False,
    "description": "本步骤计量：species 为稳定物种名到分子式（未知为空串）；coefficients 为消耗负、生成正的系数，charges 仅填写已知电荷。跨步骤同物种同名。程序仅相加，不自动配平。",
}
_ELEMENTS = set("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split())
CHECK_SCOPE = "仅核对提交的物种、分子式、有符号系数及已知电荷；不证明物种身份、反应方向、电子机理或目标活性。未知不自动补零或宣称配平。"


def formula_atoms(formula):
    """只解析完整的平面分子式；聚合物、括号、水合物、同位素等保留未知。"""
    if formula == "e-":
        return {}
    if not formula or not re.fullmatch(r"(?:[A-Z][a-z]?(?:[1-9][0-9]*)?)+", formula):
        return None
    result = defaultdict(int)
    for symbol, count in re.findall(r"([A-Z][a-z]?)([0-9]*)", formula):
        if symbol not in _ELEMENTS or (count and int(count) == 0):
            return None
        result[symbol] += int(count or 1)
    return dict(result)


def _display(vector):
    return {k: str(v) for k, v in vector.items() if v}


def check_stoichiometry(ledger, step_count=None):
    """精确有理数核对每步、总式和分步加和；不配平、不增补水或电子。"""
    if ledger is None:
        return {"status": "unknown", "reason": "no_structured_ledger", "scope": CHECK_SCOPE}
    errors = sorted(Draft202012Validator(STOICHIOMETRY_SCHEMA).iter_errors(ledger), key=lambda e: str(e.path))
    if errors:
        return {"status": "unknown", "reason": "invalid_ledger", "paths": [list(e.path) for e in errors[:5]], "scope": CHECK_SCOPE}
    species = ledger["species"]
    vectors = [*ledger["steps"], *([ledger["net"]] if "net" in ledger else [])]
    if any(not math.isfinite(value) for row in vectors for value in row.values()):
        return {"status": "unknown", "reason": "non_finite_coefficient", "scope": CHECK_SCOPE}
    atoms = {key: formula_atoms(value) for key, value in species.items()}
    charges = ledger.get("charges", {})
    def equation(vector):
        active = {k: Fraction(str(v)) for k, v in vector.items() if v}
        unknown = [k for k in active if k not in atoms or atoms[k] is None]
        totals = defaultdict(Fraction)
        if not unknown:
            for key, count in active.items():
                for atom, amount in atoms[key].items():
                    totals[atom] += count * amount
        charge = sum((count * charges[k] for k, count in active.items()), Fraction()) if all(k in charges for k in active) else None
        delta = _display(totals)
        status = "conflict" if delta or (charge is not None and charge) else "unknown" if unknown else "balanced"
        return {"status": status, "element_delta": delta, "charge_delta": str(charge) if charge is not None else None,
                "unknown_species": unknown, "charge_status": "unknown" if charge is None else "balanced" if not charge else "conflict"}
    steps = [equation(row) for row in ledger["steps"]]
    net = equation(ledger["net"]) if "net" in ledger else None
    summed = defaultdict(Fraction)
    for row in ledger["steps"]:
        for key, amount in row.items():
            summed[key] += Fraction(str(amount))
    difference = {k: summed[k] - Fraction(str(ledger["net"].get(k, 0)))
                  for k in set(summed) | set(ledger.get("net", {}))} if net is not None else {}
    mismatch = _display(difference)
    wrong_count = step_count is not None and len(steps) != step_count
    states = [row["status"] for row in steps] + ([net["status"]] if net else [])
    status = "conflict" if "conflict" in states or mismatch or wrong_count else "unknown" if "unknown" in states else "balanced"
    return {"status": status, "steps": steps, "net": net, "step_sum_delta": mismatch,
            "step_count_matches": not wrong_count, "net_status": "not_supplied" if net is None else "checked",
            "scope": CHECK_SCOPE}


def ranking_basis_findings(reason):
    """标记明确把来源标签作为名次因果的短句；只是审核线索，不自动改分或淘汰。"""
    findings = []
    for sentence in re.split(r"[。；;\n]", str(reason or "")):
        if re.search(r"不(?:应|得|能|可)?(?:仅)?(?:因|凭|按)|不(?:计入|进入|作为)|无关|而非|not (?:because|based)", sentence, re.I):
            continue
        provenance = re.search(r"wiki|已读(?:蛋白)?身份|仅模型知识|无蛋白级.*依据|未经.*核实|已核实.{0,20}(?:机理|机制|身份|蛋白)", sentence, re.I)
        causal_rank = re.search(r"(?:因|故|所以|因此|优于|低于|高于|分数|排名|排序|列前|列后|并列|取代)", sentence)
        if provenance and causal_rank:
            findings.append(sentence.strip())
    return findings


def ranking_findings(row):
    """检查实际排序语句，不把程序添加的来源标签本身当作来源加分。"""
    texts = [factor["statement"] for factor in row["ranking_factors"]] if row.get("ranking_factors") else [row.get("ranking_reason")]
    return [finding for text in texts for finding in ranking_basis_findings(text)]


def ranking_consistency(candidates, routes=()):
    """只核对模型明确绑定的比较方向与实际分数；不解释自然语言、不调整名次。"""
    from .reasoning_policy import chemistry_score, chemistry_rank_key
    results = []
    for kind, rows in (("candidate", candidates), ("route", routes)):
        aliases = {}
        for row in rows:
            identifier = row.get("candidate_id") or row.get("route_id") or row.get("proposal_id") or row.get("card_id")
            for alias in {identifier, row.get("proposal_id"), row.get("card_id"), *row.get("card_ids", [])} - {None, ""}:
                aliases.setdefault(alias, {})[identifier] = row
        def group(row):
            role = row.get("candidate_role", "primary")
            return (role, row.get("component_function", "other") if role == "route_component" else "") if kind == "candidate" else ("route", "")
        for row in rows:
            identifier = row.get("candidate_id") or row.get("route_id") or row.get("proposal_id") or row.get("card_id")
            for index, factor in enumerate(row.get("ranking_factors", [])):
                comparison = factor.get("comparison")
                if not comparison:
                    continue
                for peer_id in comparison["peer_ids"]:
                    matches = aliases.get(peer_id, {})
                    record = {"id": identifier, "peer_id": peer_id, "factor_index": index,
                              "declared_relation": comparison["relation"]}
                    if len(matches) != 1:
                        record.update(status="incomplete", reason="unknown_or_ambiguous_peer")
                    else:
                        peer = next(iter(matches.values()))
                        peer_key = next(iter(matches))
                        record.update(peer_id=peer_key, score=chemistry_score(row), peer_score=chemistry_score(peer))
                        if (kind == 'candidate' and comparison.get('scope', 'overall') == 'overall'
                                and (row.get('candidate_kind') == 'unmatched_capability') !=
                                    (peer.get('candidate_kind') == 'unmatched_capability')):
                            record.update(status='incomplete', reason='abstract_capability_is_not_concrete_competitor')
                        elif peer_key == identifier or group(row) != group(peer):
                            record.update(status="incomplete", reason="self_or_cross_group_comparison")
                        elif chemistry_rank_key(row)[0] or chemistry_rank_key(peer)[0]:
                            record.update(status="incomplete", reason="explicit_conflict_not_comparable")
                        elif comparison.get("scope") == "factor":
                            record.update(status="factor_specific", reason="local_factor_does_not_determine_overall_rank")
                        else:
                            actual = "higher" if record["score"] > record["peer_score"] else "lower" if record["score"] < record["peer_score"] else "equal"
                            record.update(actual_relation=actual,
                                status="unresolved" if comparison["relation"] == "unresolved" else
                                       "consistent" if comparison["relation"] == actual else "conflict")
                    results.append(record)
    return results


def route_accounting(route):
    """汇总显式分步式，检查与独立路线账本的一致性；不从反应文字猜物种。"""
    declared = route.get("stoichiometry")
    steps = route.get("steps", [])
    equations = [step.get("equation") for step in steps]
    missing = [index for index, value in enumerate(equations, 1) if value is None]
    errors, species, charges, vectors = [], {}, {}, []
    for index, equation in enumerate(equations, 1):
        if equation is None:
            continue
        if list(Draft202012Validator(STEP_EQUATION_SCHEMA).iter_errors(equation)):
            errors.append({"step": index, "reason": "invalid_step_equation"})
            continue
        vectors.append(equation["coefficients"])
        for field, target in (("species", species), ("charges", charges)):
            for name, value in equation.get(field, {}).items():
                if name in target and target[name] != value and target[name] != "" and value != "":
                    errors.append({"step": index, "species": name, "reason": field + "_identity_conflict"})
                elif name not in target or target[name] == "":
                    target[name] = value
        if declared:
            declared_steps = declared.get("steps", [])
            if index > len(declared_steps) or {k: v for k, v in equation["coefficients"].items() if v} != {k: v for k, v in declared_steps[index - 1].items() if v}:
                errors.append({"step": index, "reason": "declared_step_vector_mismatch"})
            for field in ("species", "charges"):
                for name, value in equation.get(field, {}).items():
                    other = declared.get(field, {}).get(name)
                    if other is not None and other != "" and value != "" and other != value:
                        errors.append({"step": index, "species": name, "reason": "declared_" + field + "_mismatch"})
    ledger = deepcopy(declared)
    if ledger and not errors:
        # 回读步骤中的显式补充，不能丢掉新增的已知电荷或分子式；不覆盖矛盾声明。
        for field, values in (("species", species), ("charges", charges)):
            for name, value in values.items():
                if name not in ledger.get(field, {}) or ledger[field][name] == "":
                    ledger.setdefault(field, {})[name] = value
    if not declared and equations and not missing and not errors:
        total = defaultdict(Fraction)
        for vector in vectors:
            for name, number in vector.items():
                if not math.isfinite(number):
                    errors.append({"reason": "non_finite_coefficient"})
                    continue
                total[name] += Fraction(str(number))
        # 不做反应配平；总式只是模型已提交各步的精确加和。
        net = {name: int(value) if value.denominator == 1 else float(value)
               for name, value in total.items() if value}
        if any(Fraction(str(net[name])) != value for name, value in total.items() if value):
            errors.append({"reason": "coefficient_precision_loss"})
        ledger = {"species": species, "steps": deepcopy(vectors),
                  "net": net or {next(iter(species)): 0},
                  "basis": "由显式分步系数相加；非自动配平，分子式与步骤文字仍须核实。"}
        if charges:
            ledger["charges"] = charges
    check = check_stoichiometry(ledger, len(steps))
    if errors:
        check = {**check, "status": "conflict", "delivery_errors": errors}
    return {"ledger": ledger, "check": check, "missing_equation_steps": missing,
            "source": "declared_route_ledger" if declared else "step_equations" if ledger else "not_delivered"}


def constrain_ranking_revision_tasks(tasks):
    """修订可以保留原名次，但审核不能预先锁定受影响对象的分数/排序。"""
    result, checks = deepcopy(tasks), []
    for index, task in enumerate(result):
        instruction = task["instruction"]
        clauses = re.split(r"[。；;\n]", instruction)
        locked = [clause for clause in clauses if
            re.search(r"(?:分数|评分|排序|排名|顺序|名次).{0,15}不变|(?:不得|禁止)(?:调整|修改|改变).{0,10}(?:分数|评分|排序|排名|顺序|名次)|保持.{0,10}(?:分数|评分|排序|排名|顺序|名次)", clause)
            and not re.search(r"无关|未受影响|其他对象|其它对象|可不变|允许不变", clause)]
        if not locked:
            continue
        checks.append({"task_index": index, "affected_ids": task["affected_ids"],
                       "original_instruction": instruction, "rule": "affected_ranking_must_be_reassessed",
                       "execution_status": "constrained"})
        task["instruction"] = (
            "按当前已绑定的审核问题与受影响 ID 定向修订；审核给出的替代理由不是新证据，不得照抄为事实。"
            "对受影响条目按同一角色/功能独立复核化学理由及分数，再决定名次；原分数和顺序可以有依据地保留，也可以改变，不能预先锁定。"
            "Wiki 核实状态单列，不按来源标签加减分；未知不当作反证，不强行升降候选。未受影响对象保持不变。"
            + ("计量提案仍只按已核算或隔离的 balance_proposals 处理，不执行未经核对的数值替代。" if task.get("balance_proposals") else ""))
    return result, checks


def catalyst_description_review(documents):
    """将同一催化者的辅因子原句与步骤依据并列，不推断哪个化学名称正确。"""
    candidates = documents.get('04_candidates.json', {}).get('enzyme_candidates', [])
    routes = documents.get('05_routes.json', {}).get('routes', [])
    mechanisms = {row['hypothesis_id']: row for row in documents.get('02_mechanisms.json', {}).get('mechanism_hypotheses', [])}
    return [{'candidate_id': row['candidate_id'], 'cofactor_classes': deepcopy(row.get('cofactor_classes', [])),
             'mechanism_summaries': [{'mechanism_id': key, 'summary': mechanisms[key].get('summary', '')}
                                     for key in row.get('mechanism_ids', []) if key in mechanisms],
             'bound_steps': [{'route_id': route['route_id'], 'step': step.get('step', index),
                              'cofactor_classes': deepcopy(step.get('cofactor_classes', [])),
                              'basis': (step.get('chemistry_check') or {}).get('basis', ''),
                              'input': step.get('input', ''), 'output': step.get('output', '')}
                             for route in routes for index, step in enumerate(route.get('steps', []), 1)
                             if step.get('candidate_id') == row['candidate_id']],
             'status': 'requires_semantic_review'} for row in candidates]


def cofactor_system_review(documents):
    """对照声明的共底物与上游机制，不从酶名推理机理；差异仅触发定向核对。"""
    # 通用化学物种别名仅用于对齐声明，不包含酶、目标反应或催化能力映射。
    aliases = {'2-oxoglutarate': r'2[-－ ]?(?:OG|oxoglutarate)|alpha-ketoglutarate|α-酮戊二酸|2-酮戊二酸',
               'ATP': r'(?<![A-Za-z])ATP(?![A-Za-z])|三磷酸腺苷',
               'SAM': r'(?<![A-Za-z])SAM(?![A-Za-z])|S-腺苷甲硫氨酸',
               'NADPH': r'(?<![A-Za-z])NAD(?:P|\(P\))H(?![A-Za-z])',
               'NADH': r'(?<![A-Za-z])NAD(?:\(P\))?H(?![A-Za-z])',
               'H2O2': r'H[2₂]O[2₂]|过氧化氢'}
    mechanisms = {m['hypothesis_id']: m for m in documents.get('02_mechanisms.json', {}).get('mechanism_hypotheses', [])}
    cofactors = documents.get('03_capabilities.json', {}).get('cofactor_candidates', [])
    rows = []
    for route in documents.get('05_routes.json', {}).get('routes', []):
        for index, step in enumerate(route.get('steps', []), 1):
            ids = [key for key in step.get('mechanism_ids', route.get('mechanism_ids', [])) if key in mechanisms]
            expected = [{'mechanism_id': key, 'summary': mechanisms[key].get('summary', ''),
                         'cofactor_declarations': [c['cofactor_class'] for c in cofactors if key in c.get('mechanism_ids', [])]}
                        for key in ids]
            equation = step.get('equation') or {}
            if not equation and (route.get('stoichiometry') or {}).get('steps'):
                ledger = route['stoichiometry']
                if index <= len(ledger['steps']):
                    equation = {'coefficients': ledger['steps'][index-1]}
            consumed = [name for name, amount in equation.get('coefficients', {}).items() if amount < 0]
            # 仅比较明确列为净消耗的物种；缓冲液、维持剂或底物正文不当作消耗。
            declared = '\n'.join(str(c) for e in expected for c in e['cofactor_declarations'])
            mismatches = [name for name, pattern in aliases.items()
                          if expected and declared and any(re.search(pattern, s, re.I) for s in consumed)
                          and not re.search(pattern, declared, re.I)]
            rows.append({'route_id': route['route_id'], 'step': step.get('step', index),
                         'candidate_id': step.get('candidate_id'), 'mechanism_ids': ids,
                         'upstream_systems': expected, 'consumed_species': consumed,
                         'step_cofactors': step.get('cofactor_classes', []),
                         'basis': (step.get('chemistry_check') or {}).get('basis', ''),
                         'unexplained_consumed_cosubstrates': mismatches,
                         'status': 'requires_review' if mismatches else 'no_declared_mismatch',
                         'rule': '缺少声明不是化学反证。核对是否误抄其他机制的共底物/电子链；确有不同系统则改步骤或机制关联，不能仅靠元素配平通过。'})
    return rows


def scientific_review(documents):
    """按稳定对象 ID 汇集可复算问题；不能把统计/引用身份当化学适配分。"""
    routes = documents.get("05_routes.json", {}).get("routes", [])
    candidates = documents.get("04_candidates.json", {}).get("enzyme_candidates", [])
    from .screening_tools import route_transfer_tasks
    return {
        "stoichiometry": [{"route_id": row["route_id"], **account["check"],
                           "delivery_source": account["source"], "missing_equation_steps": account["missing_equation_steps"]}
                          for row in routes for account in [route_accounting(row)]],
        "ranking_basis": [{"id": row.get("candidate_id") or row.get("route_id"), "findings": findings}
            for row in [*candidates, *routes] if (findings := ranking_findings(row))],
        "ranking_consistency": ranking_consistency(candidates, routes),
        "catalyst_descriptions": catalyst_description_review(documents),
        "cofactor_systems": cofactor_system_review(documents),
        'transfer_comparisons': route_transfer_tasks({}, candidates, routes,
            documents.get('04_candidates.json', {}).get('read_card_reviews', [])),
        "rule": "来源标签因果只触发定向重评，不自动加减分。明确计量冲突须修正或排除；没有账本不代表配平。分子式身份、文字反应式和机理仍须审核。",
        "delivery_rule": "缺账本的受影响路线逐步交 equation；非酶辅助步骤用 step_kind=nonenzymatic 并写 condition_note，不绑定酶或能力 ID。未知分子式为空串，不为得到 balanced 臆造。",
    }


def validate_balance_proposals(tasks, documents):
    """审核提议先核算再交付执行；不自洽提议隔离，不增加模型重试。"""
    routes = {row["route_id"]: row for row in documents.get("05_routes.json", {}).get("routes", [])}
    result, checks = deepcopy(tasks), []
    for task in result:
        proposals = task.get("balance_proposals", {})
        if not proposals:
            continue
        valid = True
        for route_id, ledger in proposals.items():
            route = routes.get(route_id)
            check = check_stoichiometry(ledger, len(route["steps"]) if route else None)
            if route is None or route_id not in task["affected_ids"]:
                check = {"status": "unknown", "reason": "unbound_route_proposal"}
            old_species = (route or {}).get("stoichiometry", {}).get("species", {})
            if any(ledger.get("species", {}).get(k) != value for k, value in old_species.items()):
                check = {"status": "unknown", "reason": "species_identity_changed_requires_review"}
            valid = valid and check["status"] == "balanced"
            checks.append({"route_id": route_id, "check": check, "original_instruction": task["instruction"],
                           "execution_status": "checked_declared_balance" if check["status"] == "balanced" else "quarantined"})
        task["instruction"] = (
            "仅按 balance_proposals 中已核算的账本修订对应路线；逐步输入输出与总式必须来自同一份计量，不能执行自然语言中的其他系数替代。分子式和物种身份仍需核实，不改无关候选或机制。"
            if valid else
            "此计量修订提议未通过程序核算或身份绑定，已隔离；不得照抄其系数或原自然语言建议。请从当前实际步骤重新核对物种、分子式、分步加和与电荷，未知保持未知，不改变无关候选。")
    return result, checks
