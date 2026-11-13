"""Protein research data checks and evidence joins; public SDK only, no execution."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from collections import Counter

from tcode_agent.plugin_sdk import (
    PluginHealth, PluginHealthStatus, PluginMetadata, ToolDefinition, ToolOutput,
)

AMINO = dict(zip(
    "ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL".split(),
    "ARNDCQEGHILKMFPSTWYV"))
CANONICAL = set(AMINO.values())
ANALYSIS_BYTES = 4 * 1024 * 1024
REF = {"type": "object", "description": "复用 protein_inputs_check 返回的 snapshot 对象（path 和 sha256）；不是原始 PDB/FASTA 文件引用，也不是评分文件。",
    "properties": {
    "path": {"type": "string", "minLength": 1},
    "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
    "required": ["path", "sha256"], "additionalProperties": False}
METADATA = {"type": "object", "description": "仅在评分文件缺少 metadata 时补充；规范 scores.json 已含 metadata 时省略，不改写原方法或单位。", "properties": {
    key: {"type": "string", "minLength": 1, "maxLength": 200}
    for key in ("method", "version", "metric", "units", "target_sequence_sha256")},
    "required": ["method", "version", "metric", "units", "target_sequence_sha256"],
    "additionalProperties": False}
METADATA["properties"]["direction"] = {"enum": ["lower", "higher"]}
METADATA["required"].append("direction")


def sequence_from_fasta(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith(">") or sum(line.startswith(">") for line in lines) != 1:
        raise ValueError("需要只含一条目标序列的 FASTA；多条序列请先明确目标，不能自动选择。")
    sequence = "".join(lines[1:]).upper()
    if not sequence or set(sequence) - CANONICAL:
        raise ValueError("第一批只支持20种标准氨基酸；空序列、未知或修饰残基需要先澄清。")
    return sequence


def structure_residues(text, chain):
    models = [line for line in text.splitlines() if line.startswith("MODEL ")]
    if len(models) > 1:
        raise ValueError("PDB 含多个模型，第一批不自动选择模型。")
    residues, seen_chains = {}, set()
    for line in text.splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        if len(line) < 27:
            raise ValueError("PDB 原子记录不完整。")
        name, current_chain = line[17:20].strip(), line[21:22].strip()
        # Waters, ions and ligands are not protein residues; never edit them.
        if line.startswith("HETATM") and name not in AMINO:
            if name in {"MSE", "SEC", "PYL", "UNK", "HYP", "SEP", "TPO", "PTR"} and (chain is None or chain == current_chain):
                raise ValueError("结构含非标准蛋白残基，不能忽略或自动替换。")
            continue
        seen_chains.add(current_chain)
        if chain is not None and current_chain != chain:
            continue
        if name not in AMINO:
            raise ValueError("结构含非标准蛋白残基，不能猜测其氨基酸身份。")
        try:
            number = int(line[22:26])
        except ValueError as exc:
            raise ValueError("PDB 残基编号无效。") from exc
        key = (current_chain, number, line[26:27].strip())
        if key in residues and residues[key] != AMINO[name]:
            raise ValueError("同一结构位点含冲突的氨基酸身份。")
        residues[key] = AMINO[name]
    if chain is None and len(seen_chains) != 1:
        raise ValueError("请明确要研究的结构链；不自动选择多链结构。")
    if not residues:
        raise ValueError("所选链没有可检查的标准蛋白残基。")
    # PDB order is the observed chain order; no renumbering or alignment guesses.
    return [{"chain": key[0], "residue_number": key[1], "insertion_code": key[2], "amino_acid": value}
            for key, value in residues.items()]


def mutation_check(mutation, sequence, protected):
    match = re.fullmatch(r"([A-Z])([1-9]\d*)([A-Z])", str(mutation))
    if not match or match[1] not in CANONICAL or match[3] not in CANONICAL:
        return "只支持标准氨基酸单点替换，例如 A12V；位点按目标序列从1开始。"
    position = int(match[2])
    if position < 1 or position > len(sequence):
        return "突变位点超出目标序列。"
    if sequence[position - 1] != match[1]:
        return "原始氨基酸与目标序列不一致。"
    if match[1] == match[3]:
        return "该记录不是氨基酸替换。"
    if position in protected:
        return "该位点受保护，不参与候选筛选。"
    return ""


def check_inputs(fasta, pdb=None, *, chain=None, protected=(), mutations=()):
    sequence = sequence_from_fasta(fasta)
    if any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= len(sequence) for p in protected):
        raise ValueError("受保护位点必须是目标序列范围内的1起始整数位置。")
    residues = structure_residues(pdb, chain) if pdb is not None else []
    if pdb is not None and "".join(residue["amino_acid"] for residue in residues) != sequence:
        raise ValueError("序列与所选链的完整观测序列不一致：可能有缺失残基、标签或输入不匹配，需要澄清；不会猜测映射或修改输入。")
    for index, residue in enumerate(residues, 1):
        residue["sequence_position"] = index
    checks = [{"mutation": mutation, "reason": mutation_check(mutation, sequence, protected)} for mutation in mutations]
    return {"kind": "protein_inputs", "schema_version": 1, "status": "blocked" if any(c["reason"] for c in checks) else "checked",
            "sequence": sequence, "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
            "chain": residues[0]["chain"] if residues else None, "mapping": residues,
            "protected_positions": sorted(set(protected)), "mutation_checks": checks,
            "limitations": ["只核对标准氨基酸完整观测链，不证明结构质量、活性或稳定性。"],
            "next_step": "先处理突变检查中的冲突。" if any(c["reason"] for c in checks) else "可分析已有评分；新计算仍须走现有授权入口。"}


def pocket_context(text, inputs, ligands, radius):
    """Heavy-atom distances to explicitly selected PDB hetero residues, not pocket prediction."""
    if not math.isfinite(radius) or not 0 < radius <= 30:
        raise ValueError("口袋半径必须在0至30 Å之间。")
    selected = {(item["chain"], item["residue_number"], item.get("insertion_code", ""), item["name"]) for item in ligands}
    if not selected or len(selected) != len(ligands):
        raise ValueError("需要唯一、明确的配体残基标识。")
    mapping = {(r["chain"], r["residue_number"], r["insertion_code"]): r for r in inputs["mapping"]}
    protein, ligand_atoms, found = [], [], set()
    for line in text.splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        key = (line[21:22].strip(), int(line[22:26]), line[26:27].strip())
        ligand = (*key, line[17:20].strip())
        is_ligand = line.startswith("HETATM") and ligand in selected
        is_protein = key in mapping and AMINO.get(line[17:20].strip()) == mapping[key]["amino_acid"]
        if not is_ligand and not is_protein:
            continue
        if line[16:17].strip():
            raise ValueError("距离计算遇到替代构象，需先明确采用的构象。")
        element = line[76:78].strip() or line[12:16].strip().lstrip("0123456789")[:1]
        if element.upper() in {"H", "D"}:
            continue
        xyz = tuple(float(line[start:start + 8]) for start in (30, 38, 46))
        if not all(math.isfinite(v) for v in xyz):
            raise ValueError("结构坐标必须为有限数值。")
        if is_ligand:
            found.add(ligand)
            ligand_atoms.append(xyz)
        else:
            protein.append((key, xyz))
    if found != selected or not protein or len(ligand_atoms) > 500:
        raise ValueError("配体标识缺失、无重原子，或配体超过500个重原子；不自动选配体。")
    distances = {}
    for key, xyz in protein:
        distance = min(math.dist(xyz, other) for other in ligand_atoms)
        position = mapping[key]["sequence_position"]
        distances[position] = min(distances.get(position, math.inf), distance)
    if set(distances) != set(range(1, len(inputs["sequence"]) + 1)):
        raise ValueError("部分目标残基缺少可用重原子坐标。")
    return {"radius_angstrom": radius, "ligands": ligands, "distance_definition": "minimum_heavy_atom_distance",
            "residues": [{"position": position, "distance_angstrom": distance, "within_radius": distance <= radius}
                         for position, distance in sorted(distances.items())]}


def analyze_rows(text, file_format, metadata, inputs, threshold=None):
    if threshold is not None and (isinstance(threshold, bool) or not math.isfinite(threshold)):
        raise ValueError("筛选阈值必须是有限数值。")
    if inputs.get("kind") != "protein_inputs" or inputs.get("schema_version") != 1 or inputs.get("status") != "checked":
        raise ValueError("需要本插件已通过的输入检查记录，不能用未检查或冲突输入分析。")
    if file_format == "json":
        document = json.loads(text)
        if not isinstance(document, dict) or set(document) != {"metadata", "rows"}:
            raise ValueError("JSON 需要 metadata 和 rows；不自动猜测第三方格式。")
        if metadata is not None and metadata != document["metadata"]:
            raise ValueError("传入元数据与文件元数据冲突。")
        metadata, rows = document["metadata"], document["rows"]
    else:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV 表头缺失或重复。")
        rows = list(reader)
    if not isinstance(metadata, dict) or not all(isinstance(metadata.get(key), str) and metadata[key].strip() and len(metadata[key]) <= 200 for key in METADATA["required"]):
        raise ValueError("必须声明方法、版本、指标、单位、分数方向及目标序列指纹，不能猜测评分语义。")
    if metadata["direction"] not in {"lower", "higher"} or metadata["target_sequence_sha256"] != inputs["sequence_sha256"]:
        raise ValueError("分数方向无效或结果声明的序列指纹与已检查输入不匹配。")
    if not isinstance(rows, list) or not rows or len(rows) > 10000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("评分必须是1至10000行对象记录，空结果不能作为有效证据。")
    sequence, protected = inputs["sequence"], inputs["protected_positions"]
    duplicates = Counter(str(row.get("mutation", "")) for row in rows)
    mapping = {r["sequence_position"]: r for r in inputs["mapping"]}
    normalized = []
    for index, row in enumerate(rows, 1):
        mutation = str(row.get("mutation", ""))
        reason = mutation_check(mutation, sequence, protected)
        score = None
        if not reason and duplicates[mutation] > 1:
            reason = "同一突变重复；请明确重复实验或模型聚合方法。"
        if not reason and row.get("chain", inputs["chain"]) != inputs["chain"]:
            reason = "评分链与已检查结构链不一致。"
        if not reason and "pdb_residue_number" in row:
            mapped = mapping.get(int(re.fullmatch(r"[A-Z](\d+)[A-Z]", mutation)[1]))
            number = row["pdb_residue_number"]
            if file_format == "csv" and isinstance(number, str) and re.fullmatch(r"[+-]?[0-9]+", number.strip()):
                try:
                    number = int(number)
                except ValueError:
                    number = None
            if type(number) is not int or not mapped or (number, row.get("insertion_code", "")) != (mapped["residue_number"], mapped["insertion_code"]):
                reason = "评分PDB编号或插入码与输入映射不一致。"
        if not reason and any(key in row and row[key] != metadata[key] for key in METADATA["required"]):
            reason = "行级评分元数据与文件声明不一致，不能混排不同指标或来源。"
        if not reason and str(row.get("status", "success")) not in {"success", "succeeded", "ok"}:
            reason = "原始评分失败或状态未确认。"
        try:
            if isinstance(row.get("score"), bool):
                raise ValueError()
            score = float(row["score"])
            if not math.isfinite(score):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            reason = reason or "分数缺失或不是有限数值。"
            score = None
        if not reason and threshold is not None:
            if (metadata["direction"] == "lower" and score > threshold) or (metadata["direction"] == "higher" and score < threshold):
                reason = "未达到声明的筛选阈值。"
        normalized.append({"row": index, "mutation": mutation, "score": score,
                           "accepted": not bool(reason), "reason": reason, "raw": row})
    candidates = [row for row in normalized if row["accepted"]]
    candidates.sort(key=lambda row: (row["score"] if metadata["direction"] == "lower" else -row["score"], row["mutation"]))
    for rank, row in enumerate(candidates, 1):
        row["rank"] = rank
    return {"kind": "protein_analysis", "schema_version": 1,
            "analysis_scope": "full", "analyzed_rows": len(rows),
            "status": "analyzed" if candidates else "insufficient_evidence", "metric": metadata,
            "constraints": {"threshold": threshold, "protected_positions": protected},
            "rows": normalized, "candidates": [row["mutation"] for row in candidates],
            "counts": {"total": len(rows), "accepted": len(candidates), "excluded": len(rows) - len(candidates)},
            "exclusion_reasons": dict(Counter(row["reason"] for row in normalized if not row["accepted"])),
            "limitations": ["元数据为来源声明，不独立证明评分对应的计算过程。", "不同方法的指标不可直接混排；预测分数不等同于实验结论或热力学自由能。"],
            "next_step": "解释候选证据及局限；新计算需另行授权。" if candidates else "没有满足条件的候选；说明缺失或排除原因，不擅自放宽条件或提交计算。"}


def combine_evidence(analyses, inputs, *, conservation=None, contexts=(), required=(), conservation_max=None, min_coverage=None, pocket_only=False):
    """Join facts by checked sequence position; no composite score or scientific decision."""
    labels = [label for label, _ in analyses]
    if len(set(labels)) != len(labels) or set(required) - set(labels):
        raise ValueError("评分来源标签重复或必需来源不存在。")
    conservation_rows = {}
    if conservation is not None:
        meta = conservation.get("metadata", {})
        if (meta.get("target_sequence_sha256") != inputs["sequence_sha256"]
                or meta.get("method") != "unweighted_shannon"):
            raise ValueError("保守性文件需为同源MCP的unweighted_shannon结果，且目标指纹一致。")
        for row in conservation.get("rows", []):
            position = row.get("position")
            if type(position) is not int or not 1 <= position <= len(inputs["sequence"]) or position in conservation_rows:
                raise ValueError("保守性位点重复或超出目标序列。")
            if row.get("original_aa") != inputs["sequence"][position - 1]:
                raise ValueError("保守性原氨基酸与目标不一致。")
            for key, upper in (("coverage", 1), ("information_bits", math.log2(20))):
                value = row.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= upper:
                    raise ValueError("保守性数值无效。")
            conservation_rows[position] = row
        if len(conservation_rows) != len(inputs["sequence"]):
            raise ValueError("保守性结果不完整，不以部分结果代替全量证据。")
    if (conservation_max is not None or min_coverage is not None) and conservation is None:
        raise ValueError("保守性过滤需要对应的完整证据。")
    for value in (conservation_max, min_coverage):
        if value is not None and (isinstance(value, bool) or not math.isfinite(value)):
            raise ValueError("证据阈值必须为有限数值。")
    if min_coverage is not None and not 0 <= min_coverage <= 1:
        raise ValueError("coverage阈值必须在0至1之间。")
    if pocket_only and not contexts:
        raise ValueError("口袋过滤需要明确配体的结构上下文。")
    pocket_rows = {}
    for index, context in enumerate(contexts):
        for row in context["residues"]:
            pocket_rows.setdefault(row["position"], []).append({"context": index, **row})
    joined = {}
    for label, analysis in analyses:
        for row in analysis["rows"]:
            entry = joined.setdefault(row["mutation"], {})
            if label in entry:
                entry[label].setdefault("duplicate_rows", []).append(row)
            else:
                entry[label] = dict(row)
    if len(joined) > 10000:
        raise ValueError("联合突变超过10000项；请缩小显式分析范围。")
    rows = []
    for mutation, evidence in sorted(joined.items()):
        reason = mutation_check(mutation, inputs["sequence"], inputs["protected_positions"])
        match = re.fullmatch(r"[A-Z]([1-9]\d*)[A-Z]", mutation)
        position = int(match[1]) if match else None
        missing = [label for label in labels if label not in evidence]
        failures = {label: row["reason"] for label, row in evidence.items() if not row["accepted"]}
        if not reason and (set(required) & (set(missing) | set(failures))):
            reason = "必需评分来源缺失、失败或未达到指定阈值。"
        if not reason and not any(row["accepted"] for row in evidence.values()):
            reason = "没有有效评分证据。"
        site = conservation_rows.get(position)
        pockets = pocket_rows.get(position, [])
        if not reason and conservation_max is not None and site["information_bits"] > conservation_max:
            reason = "未达到指定的保守性阈值。"
        if not reason and min_coverage is not None and site["coverage"] < min_coverage:
            reason = "未达到指定的比对覆盖率。"
        if not reason and pocket_only and not any(r["within_radius"] for r in pockets):
            reason = "不在指定配体口袋并集内。"
        rows.append({"mutation": mutation, "accepted": not bool(reason), "reason": reason,
                     "raw": {"scores": evidence, "missing_sources": missing, "source_failures": failures,
                             "conservation": site, "pockets": pockets}})
    candidates = [row["mutation"] for row in rows if row["accepted"]]
    return {"kind": "protein_analysis", "schema_version": 1, "analysis_scope": "full",
            "analyzed_rows": sum(a["analyzed_rows"] for _, a in analyses),
            "status": "analyzed" if candidates else "insufficient_evidence", "rows": rows, "candidates": candidates,
            "metrics": {label: a["metric"] for label, a in analyses},
            "source_counts": {label: a["counts"] for label, a in analyses},
            "counts": {"total": len(rows), "accepted": len(candidates), "excluded": len(rows) - len(candidates)},
            "constraints": {"required_sources": list(required), "conservation_max": conservation_max,
                            "min_coverage": min_coverage, "pocket_only": pocket_only,
                            "score_thresholds": {label: a["constraints"]["threshold"] for label, a in analyses}},
            "exclusion_reasons": dict(Counter(r["reason"] for r in rows if r["reason"])),
            "limitations": ["联合表按突变名称排列，不是综合排名；未计算跨方法总分。", "来源缺失与失败保留在每行；未指定的阈值不自动补全。", "配体距离和低保守性不证明功能保留；预测不是实验结论。"],
            "next_step": "由Agent结合完整证据解释、比较和提出下一步；无需额外计算时可直接交付报告。"}


class Plugin:
    info = PluginMetadata(name="protein-research", version="0.1.0", description="蛋白研究：输入检查与已有结果分析", author="TCode", plugin_type=("tool",))

    async def start(self, ctx, config):
        ctx.tools.register(ToolDefinition(
            name="protein_inputs_check", on_demand=True, side_effect_level="write",
            description="蛋白输入检查：读取单序列FASTA，可附PDB核对链与残基映射；可按明确配体标识计算重原子距离和口袋上下文。发布可恢复研究记录，不修改输入、不提交计算。",
            input_schema={"type": "object", "properties": {
                "fasta_path": {"type": "string", "minLength": 1}, "pdb_path": {"type": "string", "minLength": 1},
                "chain": {"type": "string", "maxLength": 1},
                "ligands": {"type": "array", "minItems": 1, "maxItems": 16, "items": {"type": "object", "properties": {
                    "chain": {"type": "string", "maxLength": 1}, "residue_number": {"type": "integer"},
                    "insertion_code": {"type": "string", "maxLength": 1}, "name": {"type": "string", "minLength": 1, "maxLength": 3}},
                    "required": ["chain", "residue_number", "name"], "additionalProperties": False}},
                "pocket_radius": {"type": "number", "exclusiveMinimum": 0, "maximum": 30, "default": 6},
                "protected_positions": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 10000, "uniqueItems": True},
                "mutations": {"type": "array", "items": {"type": "string", "maxLength": 30}, "maxItems": 10000}},
                "required": ["fasta_path"], "additionalProperties": False}, handler=self._inputs))
        ctx.tools.register(ToolDefinition(
            name="protein_results_analyze", on_demand=True, side_effect_level="write",
            description="蛋白已有结果全量整理：读取规范CSV/JSON单点评分，或按突变联结多份评分、同源MCP保守性与已检查口袋上下文。每份评分最多4 MiB/10000行。阈值与必需证据由调用者指定，不自动综合打分或选择研究路线；保留缺失、失败和排除原因，模型只接收统计与小预览，不提交计算。",
            input_schema={"type": "object", "properties": {
                "input_snapshot": REF, "results_path": {"type": "string", "minLength": 1},
                "file_format": {"enum": ["csv", "json"]}, "metadata": METADATA,
                "threshold": {"type": "number"},
                "score_sources": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "properties": {
                    "label": {"type": "string", "minLength": 1, "maxLength": 60}, "path": {"type": "string", "minLength": 1},
                    "file_format": {"enum": ["csv", "json"]}, "metadata": METADATA, "threshold": {"type": "number"}, "input_snapshot": REF},
                    "required": ["label", "path", "file_format"], "additionalProperties": False}},
                "conservation_path": {"type": "string", "minLength": 1},
                "context_snapshots": {"type": "array", "items": REF, "maxItems": 8},
                "required_sources": {"type": "array", "items": {"type": "string"}, "uniqueItems": True, "maxItems": 8},
                "conservation_max": {"type": "number"}, "min_coverage": {"type": "number", "minimum": 0, "maximum": 1},
                "pocket_only": {"type": "boolean", "default": False}},
                "required": ["input_snapshot"], "oneOf": [
                    {"required": ["results_path", "file_format"], "not": {"required": ["score_sources"]}},
                    {"required": ["score_sources"], "not": {"anyOf": [{"required": ["results_path"]}, {"required": ["file_format"]}, {"required": ["metadata"]}, {"required": ["threshold"]}]}}],
                "additionalProperties": False}, handler=self._results))

    async def health(self):
        return PluginHealth(PluginHealthStatus.HEALTHY, "已有数据分析可用；不提供计算能力")

    async def stop(self):
        pass

    async def _inputs(self, args, call):
        files = call.artifacts
        if files is None:
            raise RuntimeError("宿主缺少授权产物接口，请升级宿主；不会降级为直接读取。")
        fasta = await files.read_text(args["fasta_path"])
        if (args.get("ligands") or "pocket_radius" in args or "chain" in args) and not args.get("pdb_path"):
            raise ValueError("指定链或配体距离分析需要对应PDB结构。")
        if "pocket_radius" in args and not args.get("ligands"):
            raise ValueError("口袋半径需要明确的配体标识。")
        pdb = await files.read_text(args["pdb_path"], max_bytes=ANALYSIS_BYTES) if args.get("pdb_path") else None
        result = check_inputs(fasta["text"], pdb["text"] if pdb else None, chain=args.get("chain"),
                              protected=args.get("protected_positions", []), mutations=args.get("mutations", []))
        if args.get("ligands"):
            result["pocket_context"] = pocket_context(pdb["text"], result, args["ligands"], args.get("pocket_radius", 6))
        result["sources"] = [{k: source[k] for k in ("path", "sha256")} for source in (fasta, pdb) if source]
        return await self._publish(result, call, "蛋白输入检查")

    async def _checked_inputs(self, files, ref):
        previous = await files.read_text(ref["path"], sha256=ref["sha256"], snapshot=True)
        inputs = json.loads(previous["text"])["data"]
        if inputs.get("kind") == "protein_inputs_index" and inputs.get("schema_version") == 1:
            index = inputs
            source = index["input_snapshot"]
            previous = await files.read_text(source["path"], sha256=source["sha256"], snapshot=True)
            inputs = {**json.loads(previous["text"])["data"], "pocket_pages": index["pocket_pages"]}
        if inputs.get("kind") != "protein_inputs" or inputs.get("schema_version") != 1 or inputs.get("status") != "checked":
            raise ValueError("需要已通过的输入检查记录。")
        # Revalidate both original fingerprints on reuse; no silent stale mapping.
        sources = inputs.get("sources")
        if not isinstance(sources, list) or len(sources) not in {1, 2}:
            raise ValueError("输入检查记录缺少原始来源，必须重新检查。")
        originals = [await files.read_text(source["path"], sha256=source["sha256"], max_bytes=ANALYSIS_BYTES) for source in sources]
        verified = check_inputs(originals[0]["text"], originals[1]["text"] if len(originals) == 2 else None, chain=inputs.get("chain"),
                                protected=inputs.get("protected_positions", []))
        if any(verified[key] != inputs.get(key) for key in ("sequence", "sequence_sha256", "mapping", "protected_positions")):
            raise ValueError("保存的输入检查事实与原文件不一致，必须重新检查。")
        if "pocket_context" in inputs:
            if len(originals) != 2:
                raise ValueError("口袋上下文缺少原结构来源。")
            context = inputs["pocket_context"]
            verified_context = pocket_context(originals[1]["text"], verified, context["ligands"], context["radius_angstrom"])
            if context != verified_context:
                raise ValueError("口袋上下文与原结构不一致，需要重新检查。")
            if "pocket_pages" in inputs:
                rows = [row for row in context["residues"] if row["within_radius"]]
                pages = inputs["pocket_pages"]
                if not isinstance(pages, list) or len(pages) != (len(rows) + 99) // 100:
                    raise ValueError("口袋页索引与完整证据不一致。")
                warnings = []
                for number, page in enumerate(pages, 1):
                    try:
                        saved = await files.read_text(page["path"], sha256=page["sha256"], snapshot=True)
                    except (FileNotFoundError, PermissionError) as exc:
                        # Worker RPC wraps host errors; only its exact missing-file
                        # code is optional. Approval/ownership failures still stop.
                        if isinstance(exc, PermissionError) and not str(exc).startswith("[host.filenotfounderror] "):
                            raise
                        warnings.append({"code": "pocket_page_missing", "input_snapshot": dict(ref),
                                         "page": number, "path": page["path"]})
                        continue
                    data = json.loads(saved["text"])["data"]
                    expected = {"kind": "protein_pocket_page", "schema_version": 1, "page": number,
                                **{key: context[key] for key in ("radius_angstrom", "ligands", "distance_definition")},
                                "sources": sources, "rows": rows[(number - 1) * 100:number * 100]}
                    if data != expected:
                        raise ValueError("口袋页内容与完整证据不一致。")
                inputs = {**inputs, "recovery_warnings": warnings}
        return inputs

    async def _results(self, args, call):
        files = call.artifacts
        if files is None:
            raise RuntimeError("宿主缺少授权产物接口；不会绕过授权读取。")
        ref = args["input_snapshot"]
        inputs = await self._checked_inputs(files, ref)
        recovery_warnings = list(inputs.get("recovery_warnings", []))
        sources = args.get("score_sources") or [{"label": "scores", "path": args["results_path"],
                    "file_format": args["file_format"], "metadata": args.get("metadata"), "threshold": args.get("threshold")}]
        analyses, refs, score_inputs, source_bindings = [], [], {}, {}
        for source in sources:
            source_inputs = inputs
            if source.get("input_snapshot"):
                source_inputs = await self._checked_inputs(files, source["input_snapshot"])
                recovery_warnings.extend(source_inputs.get("recovery_warnings", []))
                if source_inputs["sequence_sha256"] != inputs["sequence_sha256"]:
                    raise ValueError("评分来源的结构与研究目标序列不一致。")
                source_inputs = {**source_inputs, "protected_positions": inputs["protected_positions"]}
                score_inputs[source["label"]] = source["input_snapshot"]
            scores = await files.read_text(source["path"], max_bytes=ANALYSIS_BYTES)
            analyses.append((source["label"], analyze_rows(scores["text"], source["file_format"], source.get("metadata"), source_inputs, source.get("threshold"))))
            refs.append({k: scores[k] for k in ("path", "sha256")})
            source_bindings[source["label"]] = {**refs[-1], "file_format": source["file_format"],
                                                 "input_snapshot": dict(source.get("input_snapshot") or ref)}
        joint = any(key in args for key in ("score_sources", "conservation_path", "context_snapshots", "required_sources", "conservation_max", "min_coverage", "pocket_only"))
        if joint:
            conservation = None
            if args.get("conservation_path"):
                source = await files.read_text(args["conservation_path"], max_bytes=ANALYSIS_BYTES)
                conservation = json.loads(source["text"])
                refs.append({k: source[k] for k in ("path", "sha256")})
            contexts = [inputs["pocket_context"]] if "pocket_context" in inputs else []
            for context_ref in args.get("context_snapshots", []):
                other = await self._checked_inputs(files, context_ref)
                recovery_warnings.extend(other.get("recovery_warnings", []))
                if other["sequence_sha256"] != inputs["sequence_sha256"] or "pocket_context" not in other:
                    raise ValueError("结构上下文需要同一目标序列及明确的配体距离证据。")
                contexts.append(other["pocket_context"])
            result = combine_evidence(analyses, inputs, conservation=conservation, contexts=contexts,
                required=args.get("required_sources", []), conservation_max=args.get("conservation_max"),
                min_coverage=args.get("min_coverage"), pocket_only=args.get("pocket_only", False))
            result["context_snapshots"] = args.get("context_snapshots", [])
            result["score_input_snapshots"] = score_inputs
            result["source_bindings"] = source_bindings
        else:
            result = analyses[0][1]
        result["input_snapshot"] = dict(ref)
        result["sources"] = refs
        if recovery_warnings:
            result["recovery_warnings"] = [warning for i, warning in enumerate(recovery_warnings)
                                           if warning not in recovery_warnings[:i]]
            result["limitations"].append("口袋交付页缺失，不能宣称所有交付物完整；分析仍基于已重新核对的完整输入证据。缺页引用见 recovery_warnings。")
        return await self._publish(result, call, "蛋白已有结果分析")

    async def _publish(self, result, call, title):
        artifacts = []
        preview = result.get("candidates", [])[:20]
        evidence_preview = [{"mutation": row["mutation"], "accepted": row["accepted"], "reason": row["reason"],
                             "scores": {label: {k: score[k] for k in ("score", "accepted", "reason")}
                                        for label, score in row["raw"]["scores"].items()},
                             "missing_sources": row["raw"]["missing_sources"],
                             "conservation": ({k: row["raw"]["conservation"][k] for k in ("position", "original_aa", "coverage", "information_bits")}
                                              if row["raw"]["conservation"] else None), "pockets": row["raw"]["pockets"]}
                            for row in result.get("rows", [])[:20]] if "metrics" in result else None
        if result.get("kind") == "protein_analysis" and len(json.dumps(result, ensure_ascii=False, indent=2).encode()) > 128 * 1024:
            # Keep evidence out of model context and within the existing per-frame
            # budget. Full rows retain rank/raw data; the root is a small index.
            pages, page, size = [], [], 0
            for row in result["rows"]:
                row_bytes = len(json.dumps(row, ensure_ascii=False, allow_nan=False, indent=2).encode()) + 128
                if row_bytes > 160 * 1024:
                    raise ValueError("单条评分记录超出160 KiB分析页预算（宿主上限192 KiB）；未发布部分分析。")
                if page and size + row_bytes > 160 * 1024:
                    pages.append(page)
                    page, size = [], 0
                page.append(row)
                size += row_bytes
            if page:
                pages.append(page)
            refs = []
            for index, rows in enumerate(pages, 1):
                ref = await call.artifacts.publish_json({"kind": "protein_analysis_page", "schema_version": 1,
                    "page": index, "rows": rows})
                refs.append({k: ref[k] for k in ("path", "sha256")})
                artifacts.append(ref["artifact_path"])
            result = {key: value for key, value in result.items() if key not in {"rows", "candidates"}}
            result.update(row_pages=refs, candidate_preview=preview,
                          row_storage="paged; rank records candidate order, raw preserves source rows")
        if "pocket_context" in result:
            # Publish the unchanged full evidence first: an optional page index
            # must not shrink the existing input budget or leave pages on overflow.
            complete = await call.artifacts.publish_json(result)
            artifacts.append(complete["artifact_path"])
            pocket = result["pocket_context"]
            rows = [row for row in pocket["residues"] if row["within_radius"]]
            refs = []
            for offset in range(0, len(rows), 100):
                ref = await call.artifacts.publish_json({"kind": "protein_pocket_page", "schema_version": 1,
                    "page": offset // 100 + 1, "radius_angstrom": pocket["radius_angstrom"],
                    "ligands": pocket["ligands"], "distance_definition": pocket["distance_definition"],
                    "sources": result["sources"], "rows": rows[offset:offset + 100]})
                refs.append({k: ref[k] for k in ("path", "sha256")})
                artifacts.append(ref["artifact_path"])
            snapshot = await call.artifacts.publish_json({"kind": "protein_inputs_index", "schema_version": 1,
                "status": result["status"], "input_snapshot": {k: complete[k] for k in ("path", "sha256")},
                "pocket_pages": refs})
        else:
            snapshot = await call.artifacts.publish_json(result)
        summary = {"status": result["status"], "snapshot": {k: snapshot[k] for k in ("path", "sha256")},
                   "sources": result["sources"],
                   "next_step": result["next_step"], "limitations": result["limitations"]}
        if result.get("recovery_warnings"):
            summary["recovery_warnings"] = result["recovery_warnings"]
        if "counts" in result:
            summary["counts"] = result["counts"]
            summary["candidate_preview"] = preview
            summary["analysis_scope"] = result["analysis_scope"]
            summary["analyzed_rows"] = result["analyzed_rows"]
            summary["exclusion_reasons"] = result["exclusion_reasons"]
            summary["page_count"] = len(result.get("row_pages", []))
            if evidence_preview is not None:
                summary.update(evidence_preview=evidence_preview, metrics={label: {k: meta[k] for k in METADATA["required"]}
                                                                         for label, meta in result["metrics"].items()},
                               source_counts=result["source_counts"], constraints=result["constraints"])
        else:
            summary["sequence_sha256"] = result["sequence_sha256"]
            summary["chain"] = result["chain"]
            summary["mapped_residue_count"] = len(result["mapping"])
            summary["mutation_checks"] = result["mutation_checks"][:20]
            if "pocket_context" in result:
                summary["pocket"] = {"residue_count": sum(r["within_radius"] for r in result["pocket_context"]["residues"]),
                                     "radius_angstrom": result["pocket_context"]["radius_angstrom"],
                                     "row_pages": refs}
        return ToolOutput(title, json.dumps(summary, ensure_ascii=False), metadata={
            "artifact_paths": [snapshot["artifact_path"], *artifacts], "research_snapshot": summary["snapshot"],
            "analysis_status": result["status"]})
