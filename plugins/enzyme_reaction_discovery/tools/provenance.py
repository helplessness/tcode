"""来源标签不是活性评分；模型假说永远不能冒充 Wiki 事实。"""

WIKI_ENZYME = "wiki_enzyme"
MODEL_ENZYME = "model_enzyme"
CAPABILITY = "unmatched_capability"
KINDS = (WIKI_ENZYME, MODEL_ENZYME, CAPABILITY)
NOTES = {
    WIKI_ENZYME: "Wiki 有既往反应依据；目标活性未验证",
    MODEL_ENZYME: "模型提出的具体酶，未经过 Wiki 核实",
    CAPABILITY: "所需催化能力，尚未匹配具体酶，未经过 Wiki 核实",
}


def candidate_provenance(item):
    kind = item.get("candidate_kind", WIKI_ENZYME)
    if kind not in KINDS:
        raise ValueError("unknown candidate_kind")
    level = item.get("identity_level", "named_enzyme" if str(item.get("enzyme_id", "")).startswith("EC:") else "protein") if kind == WIKI_ENZYME else item.get("identity_level", "named_enzyme" if kind == MODEL_ENZYME else "capability")
    if kind == WIKI_ENZYME and str(item.get("enzyme_id", "")).startswith("EC:"):
        level = "named_enzyme"  # EC是活性类别，任何旧标签都不能将其升级为具体蛋白。
    if level not in {"protein", "named_enzyme", "family", "capability"}:
        raise ValueError("unknown candidate identity_level")
    if kind == MODEL_ENZYME and level == "capability":
        raise ValueError("model_enzyme must name an enzyme or family")
    if kind == CAPABILITY:
        level = "capability"
    scope = item.get("evidence_scope", "wiki_prior" if kind == WIKI_ENZYME else "unverified")
    if scope not in {"wiki_prior", "related_only", "mixed_or_uncertain", "unverified"}:
        raise ValueError("unknown evidence_scope")
    note = NOTES[kind]
    if kind == WIKI_ENZYME and scope == "related_only":
        note = "仅有同类蛋白类比依据，不是本蛋白实测活性；目标活性未验证"
    elif kind == WIKI_ENZYME and scope == "mixed_or_uncertain":
        note = "Wiki 证据含类比或归属待核对；目标活性未验证"
    elif kind == MODEL_ENZYME and level != "protein":
        note = "模型提出的家族线索，未经过 Wiki 核实" if level == "family" else "模型提出的酶名线索，未经过 Wiki 核实"
    extra = {}
    if kind == WIKI_ENZYME and level != "protein":
        note = "Wiki已核实酶活性/EC来源；具体蛋白身份未确定，目标活性另核实"
    if kind == MODEL_ENZYME and item.get("user_provided_name"):
        extra = {"user_provided_name": item["user_provided_name"],
                 "user_input_quote": item.get("user_input_quote", "")}
        note = "用户提供的候选，未经过 Wiki 核实；输入声明未独立验证"
    binding = item.get("identity_verification")
    if kind == MODEL_ENZYME and isinstance(binding, dict) and binding.get("status") == "wiki_identity_linked":
        extra["identity_verification"] = binding
        note = "Wiki 已关联蛋白身份；目标活性仍为未验证假说，旧反应注释不作目标活性证明"
    return {"candidate_kind": kind, "identity_level": level, "evidence_scope": scope, "verification_note": note, **extra}


def candidate_evidence_level(item):
    """区分身份证据与目标适配；类比不会升级为目标活性证明。"""
    kind = item.get("candidate_kind", WIKI_ENZYME)
    if kind == MODEL_ENZYME and (item.get("identity_verification") or {}).get("status") == "wiki_identity_linked":
        return "Wiki 已关联蛋白身份；目标活性未验证"
    if kind == MODEL_ENZYME and item.get("user_provided_name"):
        return "用户提供具体候选；未经过 Wiki 核实"
    if kind == CAPABILITY:
        return "未匹配催化能力"
    if kind == WIKI_ENZYME:
        if item.get("identity_level") == "named_enzyme" or str(item.get("enzyme_id", "")).startswith("EC:"):
            return "Wiki已核实酶活性类别；具体蛋白身份未确定"
        if item.get("substrate_relation") == "mechanistic_analogy" or item.get("evidence_scope") in {"related_only", "mixed_or_uncertain"}:
            return "Wiki 仅支持机制类比"
        return "Wiki 已核实具体酶身份；目标活性另核实"
    if item.get("identity_level") == "family":
        return "Wiki 支持家族机制；目标家族适配未核实" if item.get("sources") and any(str(ref).startswith("ERD-CARD-") for ref in item["sources"]) else "模型提出的家族；未经过 Wiki 核实"
    return "模型提出具体酶；未经过 Wiki 核实"


def reasoning_provenance(item):
    basis = item.get("evidence_basis", "wiki_analogy")
    if basis not in {"wiki_analogy", "model_hypothesis"}:
        raise ValueError("unknown evidence_basis")
    return {"evidence_basis": basis, "verification_note": (
        "Wiki 类比支持；目标机制未验证" if basis == "wiki_analogy"
        else "模型机制/辅因子假说，未经过 Wiki 核实"
    )}
