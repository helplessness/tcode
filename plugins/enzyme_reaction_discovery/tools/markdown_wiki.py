"""中文 Markdown 是唯一事实源；仅在内存中生成现有工具的兼容视图。

不从正文猜测底物结构、催化事件或适用性，不添加化学规则和评分。
天然反应与同类蛋白研究分开引用，读取时始终附带身份、局限和来源。
"""
from __future__ import annotations

import hashlib
import re


PROTEIN_PATTERN = r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-[1-9][0-9]*)?"
REQUIRED_SECTIONS = ("简介", "催化的反应", "辅因子与机理", "研究与结构资料", "适用范围与限制", "参考资料")


def parse_wiki(text, entry):
    title = re.match(r"# ([^\n]+)\n", text)
    if not title:
        raise ValueError("Wiki requires one Chinese Markdown title")
    # 六行普通 Wiki 身份信息；版本、校验和由 manifest 维护，不重复编写化学 JSON。
    metadata = {}
    for key, value in re.findall(r"^> ([^：\n]+)：([^\n]+)$", text, re.M):
        if key in metadata:
            raise ValueError(f"duplicate Wiki metadata: {key}")
        metadata[key] = value.strip()
    for key in ("UniProt", "来源", "英文名", "EC", "家族", "辅因子"):
        if not metadata.get(key):
            raise ValueError(f"Wiki missing metadata: {key}")
    accession = metadata["UniProt"]
    activity_only = metadata.get("身份类型") == "酶活性"
    if activity_only:
        if accession != "未确定" or not re.fullmatch(r"[1-7]\.[0-9]+\.[0-9]+\.[0-9]+", metadata["EC"]):
            raise ValueError("酶活性卡必须有明确完整EC，并将UniProt标为未确定")
    elif not re.fullmatch(PROTEIN_PATTERN, accession):
        raise ValueError("Wiki requires a concrete protein or explicitly sourced EC enzyme activity")
    # 官方条目可能尚无完整 EC；末位 '-' 表示未细分，不能替蛋白编造编号。
    if metadata["EC"] != "未注明" and not re.fullmatch(r"[1-7]\.[0-9]+\.[0-9]+\.(?:[0-9]+|-)", metadata["EC"]):
        raise ValueError("Wiki EC must be a reaction classification with a numeric or unassigned final field")
    parts = re.split(r"^## ([^\n]+)\n", text, flags=re.M)
    sections = {}
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        if heading in sections:
            raise ValueError(f"duplicate Wiki section: {heading}")
        sections[heading] = body.strip()
    unknown = set(sections) - set(REQUIRED_SECTIONS) - {"同类酶研究先例"}
    if unknown:
        raise ValueError(f"unindexed Wiki section; use a subsection under a known heading: {sorted(unknown)}")
    for key in REQUIRED_SECTIONS:
        if not sections.get(key):
            raise ValueError(f"Wiki missing section: {key}")
    summary = re.sub(r"^> .*\n?", "", sections["简介"], flags=re.M).strip()
    if not summary:
        raise ValueError("Wiki introduction must describe the enzyme function")
    sources = []
    for label, url in re.findall(r"\[([^\]\n]+)\]\((https://[^\s)]+)\)", sections["参考资料"]):
        if any(e["url"] == url for e in sources):
            continue
        sources.append({"evidence_id": "SRC-" + hashlib.sha256(url.encode()).hexdigest()[:12],
                        "title": label, "url": url, "verification_status": "curated_source_link"})
    protein_url = f"https://www.uniprot.org/uniprotkb/{accession}/entry"
    rhea_id = metadata.get("Rhea", "")
    rhea_url = f"https://www.rhea-db.org/rhea/{rhea_id.removeprefix('RHEA:')}"
    has_identity_source = any(e["url"] == protein_url for e in sources) or (
        bool(re.fullmatch(r"RHEA:[0-9]+", rhea_id))
        and any(e["url"] == rhea_url for e in sources)
    )
    if activity_only:
        ec_url = "https://iubmb.qmul.ac.uk/enzyme/EC" + metadata["EC"].replace(".", "/") + ".html"
        has_identity_source = any(e["url"] == ec_url for e in sources) and any(e["url"] == rhea_url for e in sources)
    if not has_identity_source:
        raise ValueError(
            "Wiki references must include its exact UniProt entry or declared Rhea record"
        )
    scope = sections["适用范围与限制"]
    if activity_only:
        scope += "\n本条为EC酶活性记录，具体UniProt蛋白、序列及物种身份未确定；不能用作已核实具体蛋白证据。"
    reactions = []
    groups = [("NATIVE", "催化的反应", "native", "本蛋白的已知反应与机理", ["催化的反应", "辅因子与机理", "研究与结构资料"])]
    if activity_only:
        groups[0] = ("NATIVE", "催化的反应", "native", "该EC酶活性的标准反应；具体蛋白身份未确定", ["催化的反应", "辅因子与机理", "研究与结构资料"])
    if "同类酶研究先例" in sections:
        groups.append(("RELATED", "同类酶研究先例", "representative", "同类蛋白的研究先例，不是本蛋白的实测活性", ["同类酶研究先例"]))
    for rid, heading, kind, attribution, included in groups:
        markdown = "\n\n".join(f"## {key}\n\n{sections[key]}" for key in included)
        # 证据链接只来自该片段及其随附局限，不能把全篇参考资料误当每项结论的支持证据。
        urls = set(re.findall(r"\]\((https://[^\s)]+)\)", markdown + "\n" + scope))
        evidence = [e["evidence_id"] for e in sources if e["url"] in urls]
        if not evidence:
            raise ValueError(f"Wiki section needs an inline source also listed in references: {heading}")
        reactions.append({"reaction_id": rid, "name": heading, "activity_type": kind,
                          "markdown": markdown, "attribution": attribution,
                          "mechanistic_role": sections["辅因子与机理"] if rid == "NATIVE" else sections[heading],
                          "scope": scope, "evidence_refs": evidence,
                          # 有来源的说明不等于每步直接观测；精确证据强度仍以正文为准。
                          "activity_status": ("curated_enzyme_activity" if activity_only else "curated_protein_annotation") if rid == "NATIVE" else "related_protein_only",
                          "mechanism_status": "see_source_scope"})
    # "未注明"表示所引数据库条目没有给出辅因子注释，不能当成一种辅因子索引。
    cofactors = [] if metadata["辅因子"] in {"无", "未注明"} else [c.strip() for c in metadata["辅因子"].split("；")]
    return {
        "schema_version": "2.0", "card_id": entry["card_id"], "card_version": entry["card_version"],
        "source_format": "markdown", "source_path": entry["path"],
        "identity": {"identity_level": "named_enzyme" if activity_only else "protein", "enzyme_id": f"EC:{metadata['EC']}" if activity_only else f"UniProt:{accession}",
                     "display_name": title[1], "english_name": metadata["英文名"],
                     "organism": metadata["来源"], "ec_class": metadata["EC"],
                     "enzyme_family": metadata["家族"], "aliases": [metadata["英文名"]]},
        "wiki": {"summary": summary, "functional_roles": [], "known_reactions": reactions},
        "classification": {"cofactor_classes": cofactors, "cofactor_notes": sections["辅因子与机理"].split("\n\n")[0], "transformation_tags": []},
        "mechanism": {"summary": sections["辅因子与机理"]},
        "evidence": sources, "known_limitations": [scope],
    }
