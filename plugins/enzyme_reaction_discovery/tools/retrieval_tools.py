"""本地、版本绑定的 Wiki 片段检索；不包含化学规则或目标答案。

SQLite FTS5 indexes reaction text and enzyme annotations separately. RRF can
merge their text rankings with version-bound semantic hits; neither the index
nor its score asserts catalytic activity.
"""
from __future__ import annotations
from .reasoning_policy import RATIONALE_PROPERTIES, draft_rationale, MECHANISM_SUBMISSION_POLICY, mechanism_submission_window

import hashlib
import json
from copy import deepcopy
import os
import re
import secrets
import sqlite3
import unicodedata
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .knowledge_tools import _rag_tokens
from .wiki_tools import native_pathway_scaffolds, native_reaction_neighbors, wiki_packet, citation_index, citation_id, reference_index
from .wiki_chunks import CHUNK_VERSION, evidence_fragments
from .markdown_wiki import PROTEIN_PATTERN

INDEX_VERSION = "7"
MAX_SEARCH_ROUNDS = 3
MAX_READ_ROUNDS = 4
MAX_SELECTED_REFS = 24
MAX_EVIDENCE_CHARS = 26000
MAX_DELIVERY_CHARS = 14000
PAGE_SIZE = 8
POOL_SIZE = 200
CHANNEL_LIMIT = 100
MAX_EXTRA_PAGES = 2
MAX_CONTEXT_CALLS = 10
MAX_SUMMARIES = 40
MAX_CANDIDATE_DRAFT = 8


def _mechanism_dimensions(statement):
    """Classify evidence by reaction roles for bounded promotion hints.

    This is a retrieval aid, not a chemistry score or a target-answer rule.
    It deliberately uses broad event roles so a different substrate can still
    expose a useful local mechanism graph.
    """
    text = str(statement or "")
    dimensions = set()
    if re.search(r"反应中心|羰基|醛基|共价|辅酶|辅因子|中间体|活化|活性位点|催化环境|carbonyl|covalent|cofactor|intermediate|activation|active.?site", text, re.I):
        dimensions.add("carbonyl_activation")
    if re.search(r"离去|释放|转移|基团去向|断裂|phosphate|leaving|release|transfer|cleavage", text, re.I):
        dimensions.add("group_fate")
    if re.search(r"捕获|亲核|第二底物|成键|加合物|缩合|形成.{0,12}键|建立.{0,12}键|nucleoph|capture|addition|condens|bond.?form", text, re.I):
        dimensions.add("capture_or_bond")
    if re.search(r"碳骨架|骨架保留|碳数|碳链|[一二三四五六七八九十]碳|\bC\d+\b|重排|裂解|skeleton|carbon.?chain|rearrang", text, re.I):
        dimensions.add("skeleton_or_topology")
    if re.search(r"双键|消除|脱水|不饱和|β.?消除|elimin|dehydrat|unsatur", text, re.I):
        dimensions.add("elimination_or_unsaturation")
    return sorted(dimensions)


def _mechanism_promotion_hints(packet):
    """Return read evidence that deserves an explicit promote/reject decision."""
    hints = []
    for card in packet.get("cards", []):
        for claim in card.get("claims", []):
            dimensions = _mechanism_dimensions(claim.get("statement"))
            # At least two independent event roles are required; a substrate
            # name or a generic cofactor mention alone is never enough.
            if len(dimensions) < 2:
                continue
            hints.append({
                "ref": claim["ref"],
                "card_id": card["card_id"],
                "display_name": card["display_name"],
                "dimensions": dimensions,
                "instruction": "必须作为 wiki_analogy 明确纳入某条机制，或在 knowledge_gaps 说明为何被化学冲突淘汰；不能静默丢弃。",
            })
    # Keep the context bounded and diverse across proteins.
    hints.sort(key=lambda row: (-len(row["dimensions"]), row["card_id"], row["ref"]))
    selected, seen_cards = [], set()
    for row in hints:
        if row["card_id"] in seen_cards:
            continue
        selected.append(row)
        seen_cards.add(row["card_id"])
        if len(selected) >= 6:
            break
    return selected


def _derive_mechanism_bridge_query(snapshot):
    """Build one answer-blind query from unresolved reaction event roles.

    No enzyme, family, cofactor, benchmark substrate, nucleophile class or
    expected intermediate is introduced here. Concrete mechanisms may only
    enter after retrieval and source-card reading.
    """
    document = snapshot.get("documents", {}).get("01_reaction_analysis.json", {})
    analysis = document.get("reaction_analysis", {})
    text = canonical({
        "net_changes": analysis.get("net_changes", []),
        "components": analysis.get("component_interpretations", []),
        "assumptions": analysis.get("assumptions", []),
    })
    event_roles = []
    if re.search(r"醛|羰基|羧酸|氧化态|carbonyl|aldehyde|oxidation", text, re.I):
        event_roles.append("反应中心活化或氧化态变化")
    if re.search(r"离去|释放|断裂|脱去|转移|leaving|release|cleavage|transfer", text, re.I):
        event_roles.append("离去或转移基团的去向")
    if re.search(r"骨架|碳数|碳链|保留|重排|裂解|skeleton|carbon.?chain|rearrang", text, re.I):
        event_roles.append("骨架保留、重排或裂解约束")
    if re.search(r"双键|消除|脱水|不饱和|成键|断键|elimin|dehydrat|unsatur|bond", text, re.I):
        event_roles.append("目标成键、断键或不饱和化事件")
    if not event_roles:
        event_roles.append("尚未闭合的反应中心与净键变化")
    roles = "；".join(event_roles[:4])
    return (
        f"局部反应事件图：{roles}；检索能够解释这些事件的催化活化、短寿命中间体、"
        "天然终止与产物释放方式，并比较改变终止方式时仍未闭合的化学步骤"
    )


def canonical(value):
    """生成用于摘要和版本比较的稳定 JSON 字符串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def query_key(text):
    """将查询压缩为不受大小写和空白影响的对比键。"""
    # 语义检索中 A→B 与 B→A 不是同一个问题；不得按无序词袋去重。
    return " ".join(unicodedata.normalize("NFKC", text).split())


def exact_identifiers(text):
    """识别完整编号并统一格式；部分 EC 按字面匹配，不展开成通配条件。"""
    text = unicodedata.normalize("NFKC", str(text)).upper()
    go = {"GO:" + value for value in re.findall(r"(?<![A-Z0-9])GO\s*[:_]\s*([0-9]{7})(?![A-Z0-9])", text)}
    ec = {"EC:" + value for value in re.findall(r"(?<![A-Z0-9.])([1-7](?:\.(?:[0-9]+|-)){3})(?![A-Z0-9_-]|\.[0-9-])", text)}
    proteins = {"UniProt:" + value for value in re.findall(r"(?<![A-Z0-9])(" + PROTEIN_PATTERN + r")(?![A-Z0-9-])", text)}
    return go | ec | proteins


def question_key(question):
    """问题的稳定交付标识；不据此判断化学覆盖。"""
    return hashlib.sha256(query_key(question).casefold().encode()).hexdigest()[:16]


def summary_row(row):
    """只精简传输摘要；完整排名、来源和正文继续保存在池与回读证据中。"""
    fields = ("ref", "field_path", "card_id", "display_name", "enzyme_id", "enzyme_family", "ec_number",
              "retrieval_score", "weak_lead", "matched_identifiers", "identity_identifiers")
    data = {key: row[key] for key in fields if key in row}
    if data.get("ref"):
        data["read_id"] = "R" + citation_id(data["ref"])[1:]
    text = str(row.get("excerpt", ""))
    try:
        fragment = json.loads(text)
    except (ValueError, TypeError):
        fragment = None
    if isinstance(fragment, dict):
        text = str(fragment.get("mechanistic_role", text))
    elif text.lstrip().startswith("{"):
        match = re.search(r'"mechanistic_role"\s*:\s*"((?:[^"\\]|\\.)*)', text)
        if match:
            try:
                text = json.loads('"' + match[1] + '"')
            except ValueError:
                text = match[1]
    start = 0
    for term in sorted(row.get("gap_match", {}).get("matched_terms", []), key=len, reverse=True):
        position = text.casefold().find(term.casefold())
        if 2 <= len(term) <= 48 and position >= 0:
            start = max(0, position - 120)
            break
    offset = row.get("excerpt_offset", 0) + start
    if offset:
        data["excerpt_offset"] = offset
    data.update(excerpt=text[start:start + 360], excerpt_is_partial=bool(row.get("excerpt_is_partial") or start or len(text) > 360),
                usable_as_evidence=False, source_count=len(row.get("evidence_refs", [])) if "evidence_refs" in row else row.get("source_count", 0))
    if row.get("gap_match"):
        match = row["gap_match"]
        data["gap_match"] = {
            "question_id": match.get("question_id") or question_key(match.get("question", "")),
            "matched_terms": [term for term in match.get("matched_terms", []) if len(term) <= 48][:6],
            "coverage_status": "not_assessed"}
    return data


def page_rows(pool, offset, seen, *, limit=PAGE_SIZE, first=True, allow_weak=False, max_chars=8500, deferred=None):
    """从固定结果池取一页未送达记录，不重新执行检索。"""
    results, counts = [], Counter()
    if limit <= 0:
        return results, set()
    for row in pool[offset:]:
        if row["ref"] in seen or (row.get("weak_lead") and not allow_weak):
            continue
        if first and counts[row["card_id"]] >= 2:
            continue  # Delayed, not removed: later pages drop the per-protein cap.
        summary = summary_row(row)
        if len(canonical(results + [summary])) > max_chars:
            if deferred is not None:
                deferred.append(row)
            break  # 保留最早未送达项；不再用后面的短条目挤掉它。
        results.append(summary)
        counts[row["card_id"]] += 1
        if len(results) == limit:
            break
    return results, {row["ref"] for row in results}


def gap_terms(text):
    """去掉证据状态和格式噪声，不引入酶名、辅因子别名或化学答案。"""
    text = re.sub(r"https?://\S+", " ", str(text))
    # 同一催化能力常用缩写与全名并存；统一通用化学缩写，避免相关卡片只因写法不同落到深层池。
    text = re.sub(r"(?i)\b(?:2\s*[-‐‑–—]?\s*OG|α\s*[-‐‑–—]?\s*KG|2\s*[-‐‑–—]?\s*酮戊二酸|α\s*[-‐‑–—]?\s*酮戊二酸)\b", "2-oxoglutarate", text)
    text = re.sub(r"未经过\s*Wiki\s*核实|未验证|未核实|本轮|本库|知识库|数据库|证据|缺口|未知|原文|条目|未收录|未找到|没有|是否|需要|尚未|实验|验证|本蛋白|已知|反应式|具体|支持", " ", text)
    return {token for token in _rag_tokens(text) if not token.isdigit()} - {
        "wiki", "unknown", "unverified", "evidence", "scope", "the", "and", "of", "is",
        "with", "for", "from", "to", "in", "by", "a", "an",
        "dependent", "independent", "belongs", "family", "enzyme", "binding"}


def gap_row_text(row):
    """只用科学正文、身份与家族，不让重复 scope/适用范围声明主导相关性。"""
    text = row.get("excerpt", "")
    try:
        fragment = json.loads(text)
    except (ValueError, TypeError):
        fragment = None
    if isinstance(fragment, dict):
        text = fragment.get("mechanistic_role", "")
    elif isinstance(text, str) and text.lstrip().startswith("{"):
        # 摘要经常是截短的 JSON：只取机制字段已有文本，不把后面的 scope 当正文。
        match = re.search(r'"mechanistic_role"\s*:\s*"((?:[^"\\]|\\.)*)', text)
        text = match.group(1) if match else ""
    return " ".join([str(text), str(row.get("enzyme_family", "")), str(row.get("display_name", ""))])


def gap_ranked_rows(rows, gaps, seen, *, with_scores=False, deferred=()):
    """逐问题排序、交替送达；只用科学正文匹配，避免一个宽问题占满页面。"""
    queries = [gap_terms(text) for text in gaps]
    row_tokens = {row["ref"]: gap_terms(gap_row_text(row)) for row in rows}
    card_tokens = {}
    for row in rows:
        card_tokens.setdefault(row["card_id"], set()).update(row_tokens[row["ref"]])
    frequencies = Counter(token for tokens in card_tokens.values() for token in tokens)
    maximum = max((row.get("retrieval_score", 0) for row in rows), default=0) or 1
    queues = []
    for question, query in zip(gaps, queries):
        priority = {ref for ref in deferred if not isinstance(deferred, dict)
                    or not deferred[ref] or deferred[ref] == question}
        # 仅以池中可查到的词衡量覆盖，不让长问题中未入库词稀释有效匹配。
        searchable = query & frequencies.keys()
        denominator = sum(1 / frequencies[t] for t in searchable) or 1
        scored = []
        for index, row in enumerate(rows):
            if row["ref"] in seen:
                continue
            tokens = row_tokens[row["ref"]]
            local = sum(1 / frequencies[t] for t in tokens & query) / denominator
            protein = sum(1 / frequencies[t] for t in card_tokens[row["card_id"]] & query) / denominator
            lexical = 0.75 * local + 0.25 * protein
            score = 0.75 * lexical + 0.25 * row.get("retrieval_score", 0) / maximum
            match = {"question": question,
                     "matched_terms": sorted(tokens & query)[:12],
                     "unmatched_terms": sorted(query - tokens)[:12],
                     "coverage_status": "not_assessed"}
            scored.append((score, lexical, -index, {**row, "gap_match": match}))
        if not scored:
            continue
        best = max(item[1] for item in scored)
        # 有明确正文匹配时不以泛词或零匹配片段凑满；无词面匹配仍保留语义召回。
        if best:
            retained = [item for item in scored if item[1] >= best * 0.5]
            # 保留少量“稀有锚点”候选：低于相对阈值但确实命中问题词的蛋白，
            # 可能正是不同辅因子/终止拓扑的竞争线索；数量仍有界，避免稀释摘要。
            fallback = sorted(
                (item for item in scored if 0 < item[1] < best * 0.5),
                key=lambda item: (sum(1 / frequencies[t] for t in row_tokens[item[3]["ref"]] & query), item[0]),
                reverse=True,
            )[:12]
            fallback = [(max(item[0], best * 0.51), item[1], item[2],
                         {**item[3], "gap_match": {**item[3]["gap_match"], "fallback_anchor": True}})
                        for item in fallback]
            scored = retained + fallback
        scored.sort(key=lambda item: (item[3]["ref"] in priority, *item[:3]), reverse=True)
        cards, representatives, remaining = set(), [], []
        for item in scored:
            (remaining if item[3]["card_id"] in cards else representatives).append(item)
            cards.add(item[3]["card_id"])
        queues.append(representatives + remaining)
    ordered, delivered = [], set()
    while any(queues):
        for queue in queues:
            while queue and queue[0][3]["ref"] in delivered:
                queue.pop(0)
            if queue:
                item = queue.pop(0)
                ordered.append(item)
                delivered.add(item[3]["ref"])
    return [(item[0], item[3]) for item in ordered] if with_scores else [item[3] for item in ordered]


class WikiIndex:
    """
    Wiki 片段的 SQLite/FTS5 索引与 FAISS 元数据容器。

    字段：
    - ``library``：只读 Wiki 知识库。
    - ``path``：当前工作区的 SQLite 缓存路径。
    - ``faiss_path``：随知识版本发布的可选只读 FAISS 文件。
    - ``lock_path``：防止多进程同时建索引的锁文件。
    """
    def __init__(self, library, workspace, workspace_dir=None):
        self.library = library  # 索引的唯一知识来源。
        self._workspace = workspace  # 缓存路径解析与读写边界。
        # 知识资产随包发布；只共享知识索引，不共享运行查询、选择证据或报告。
        root = Path(library._plugin_root).resolve()
        cards = root / "knowledge" / "cards"
        shared_path = getattr(workspace, "resolve_knowledge_cache_path", None)
        self.path = Path(shared_path("wiki-index.sqlite3") if shared_path else workspace.resolve_write_path(str(root / ".cache/wiki-index.sqlite3")))
        cache = self.path.parent
        # 可随知识卡交付的独立 FAISS 文件放在 cards 目录；SQLite 仍留在
        # .cache，保存模型签名、稳定 ID、embedding 缓存和校验元数据。
        # faiss.index 是随知识卡发布的只读知识资产。在线检索不得尝试在
        # 插件安装目录中补写它；更新由离线索引构建命令完成。
        self.faiss_path = Path(workspace.resolve_read_path(str(cards / "faiss.index")))  # 随 Wiki 发布的只读向量索引。
        self.lock_path = Path(shared_path("wiki-index.sqlite3.build.lock")) if shared_path else cache / "wiki-index.sqlite3.build.lock"

    def materialize_faiss(self, signature):
        """将指定代次的序列化 FAISS 索引原样写为独立文件。"""
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("SELECT data FROM vector_index WHERE signature=?", (signature,)).fetchone()
        if not row or not row[0]:
            raise ValueError("faiss_index_not_found")
        shared_path = getattr(self._workspace, "resolve_knowledge_cache_path", None)
        target = Path(shared_path("faiss.index") if shared_path else self._workspace.resolve_write_path(".cache/enzyme_pathway/faiss.index"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.read_bytes() == row[0]:
            return target
        temp = target.with_name(target.name + ".tmp-" + secrets.token_hex(8))
        try:
            temp.write_bytes(row[0])
            os.replace(temp, target)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        return target

    @contextmanager
    def vector_build_guard(self):
        """跨进程非阻塞锁；忙时使用词法，不等待或循环抢锁。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except BlockingIOError:
                        pass
                yield acquired
            finally:
                if acquired:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def ensure(self):
        manifest = self.library.manifest()
        fingerprint = self.library.fingerprint()
        # Warm searches must not take a shared write lock or execute DDL.
        if self.path.is_file():
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                try:
                    metadata = dict(db.execute("SELECT key,value FROM meta"))
                except sqlite3.OperationalError:
                    metadata = {}
            if metadata.get("fingerprint") == fingerprint and metadata.get("index_version") == INDEX_VERSION:
                return {**metadata, "reused": True, "changed_cards": 0}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, sha TEXT NOT NULL, enzyme_id TEXT UNIQUE NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, ref TEXT UNIQUE NOT NULL, card_id TEXT NOT NULL, summary TEXT NOT NULL)")
                columns = {row[1] for row in db.execute("PRAGMA table_info(records)")}
                for column in ("stable_key", "embedding_text", "text_sha"):
                    if column not in columns:
                        db.execute(f"ALTER TABLE records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                db.execute("CREATE TABLE IF NOT EXISTS embeddings (stable_key TEXT NOT NULL, text_sha TEXT NOT NULL, signature TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(stable_key,signature))")
                db.execute("CREATE TABLE IF NOT EXISTS vector_index (fingerprint TEXT NOT NULL, signature TEXT PRIMARY KEY, dim INTEGER NOT NULL, ids TEXT NOT NULL, checksum TEXT NOT NULL, data BLOB NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS index_generations (generation TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS vector_generations (fingerprint TEXT NOT NULL, signature TEXT NOT NULL, dim INTEGER NOT NULL, ids TEXT NOT NULL, checksum TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY(fingerprint,signature))")
                db.execute("CREATE TABLE IF NOT EXISTS identifiers (term TEXT NOT NULL, card_id TEXT NOT NULL, PRIMARY KEY(term,card_id))")
                db.execute("CREATE TABLE IF NOT EXISTS identifier_hits (term TEXT NOT NULL, record_id INTEGER NOT NULL, scope TEXT NOT NULL, PRIMARY KEY(term,record_id,scope))")
                db.execute("CREATE INDEX IF NOT EXISTS identifier_hits_record ON identifier_hits(record_id)")
                db.execute("CREATE INDEX IF NOT EXISTS records_card ON records(card_id)")
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(reaction, annotation)")
                metadata = dict(db.execute("SELECT key,value FROM meta"))
                if metadata.get("fingerprint") == fingerprint and metadata.get("index_version") == INDEX_VERSION:
                    db.commit()
                    return {**metadata, "reused": True, "changed_cards": 0}
                old = dict(db.execute("SELECT id,sha FROM cards"))
                if metadata.get("fingerprint"):
                    # Recoverable old generations contain Wiki only, never queries.
                    snapshot = {"meta": metadata, "cards": list(db.execute("SELECT * FROM cards")),
                                "records": list(db.execute("SELECT * FROM records")),
                                "fts": list(db.execute("SELECT rowid,reaction,annotation FROM claims_fts"))}
                    db.execute("INSERT OR IGNORE INTO index_generations VALUES (?,?)",
                               (metadata["fingerprint"] + ":" + metadata.get("index_version", ""), canonical(snapshot)))
                    db.execute("INSERT OR IGNORE INTO vector_generations SELECT * FROM vector_index")
                entries = {e["card_id"]: e for e in manifest["cards"]}
                changed = {cid for cid, e in entries.items() if old.get(cid) != e["sha256"]}
                if metadata.get("index_version") != INDEX_VERSION:
                    changed = set(entries)
                removed = set(old) - set(entries)
                for cid in sorted(changed | removed):
                    db.execute("DELETE FROM identifier_hits WHERE record_id IN (SELECT id FROM records WHERE card_id=?)", (cid,))
                    db.execute("DELETE FROM claims_fts WHERE rowid IN (SELECT id FROM records WHERE card_id=?)", (cid,))
                    db.execute("DELETE FROM records WHERE card_id=?", (cid,))
                    db.execute("DELETE FROM cards WHERE id=?", (cid,))
                    db.execute("DELETE FROM identifiers WHERE card_id=?", (cid,))
                for cid in sorted(changed):
                    card = self.library.card(cid)
                    identity = card["identity"]
                    identity_level = identity.get("identity_level", "named_enzyme" if identity["enzyme_id"].startswith("EC:") else "protein")
                    db.execute("INSERT INTO cards VALUES (?,?,?)", (cid, entries[cid]["sha256"], identity["enzyme_id"]))
                    identifiers = [identity.get(k, "") for k in ("enzyme_id", "display_name", "english_name", "ec_class")]
                    identifiers.extend(identity.get("aliases", []))
                    identifiers.append(identity["enzyme_id"].rsplit(":", 1)[-1])
                    db.executemany("INSERT OR IGNORE INTO identifiers VALUES (?,?)",
                                   [(query_key(t).casefold(), cid) for t in identifiers if t])
                    annotation_fields = {
                        "identity": identity, "search_terms": card.get("search_terms", []),
                        "summary": card["wiki"]["summary"],
                        "roles": card["wiki"]["functional_roles"],
                        "mechanism": card["mechanism"],
                        "classification": card["classification"],
                        "limitations": card["known_limitations"],
                    }
                    annotation = " ".join(_rag_tokens(annotation_fields))
                    if card.get("source_format") == "markdown":
                        # 正文只入对应片段；不得让同类酶旁活性污染本蛋白天然反应的检索字段。
                        annotation_fields = {"identity": identity, "summary": card["wiki"]["summary"],
                                             "cofactors": card["classification"]["cofactor_classes"]}
                        annotation = " ".join(_rag_tokens(annotation_fields))
                    for field, reaction in evidence_fragments(card):
                        ref = f"{cid}:{field}"
                        summary = {
                            "ref": ref, "card_id": cid, "enzyme_id": identity["enzyme_id"],
                            "identity_level": identity_level,
                            "display_name": identity["display_name"], "ec_number": identity["ec_class"],
                            "enzyme_family": identity["enzyme_family"], "card_version": card["card_version"],
                            "field_path": field, "reaction_id": reaction["reaction_id"],
                            "evidence_refs": reaction.get("evidence_refs", []),
                            "excerpt": canonical({k: reaction[k] for k in ("name", "mechanistic_role", "scope")})[:360],
                            "excerpt_is_partial": True, "usable_as_evidence": False,
                            "activity_status": reaction.get("activity_status", "curator_annotation_unverified"),
                            "attribution": reaction.get("attribution", "按原文核对"),
                        }
                        stable_key = f"{cid}:reaction:{reaction['reaction_id']}"
                        summary["stable_id"] = stable_key
                        rowid = db.execute("INSERT INTO records(ref,card_id,summary) VALUES (?,?,?)",
                                           (ref, cid, canonical(summary))).lastrowid
                        searchable = {key: reaction[key] for key in (
                            "name", "activity_type", "substrates", "products", "cofactors",
                            "mechanistic_role", "mechanistic_events", "key_intermediates", "scope",
                        ) if key in reaction}
                        if "markdown" in reaction:
                            searchable = {"attribution": reaction["attribution"],
                                          "markdown": reaction["markdown"], "scope": reaction["scope"]}
                            embedding_text = (identity["display_name"] + "\n" + identity["enzyme_id"] + "\n"
                                              + identity["english_name"] + "\n" + identity["organism"] + "\n"
                                              + canonical(searchable))
                        else:
                            embedding_text = canonical({"enzyme": annotation_fields, "reaction": searchable})
                        if len(embedding_text) > 12000:
                            raise ValueError(f"Wiki embedding text exceeds budget: {ref}; split the paragraph")
                        db.execute("UPDATE records SET stable_key=?,embedding_text=?,text_sha=? WHERE id=?",
                                   (stable_key, embedding_text, hashlib.sha256(embedding_text.encode()).hexdigest(), rowid))
                        db.execute("INSERT INTO claims_fts(rowid,reaction,annotation) VALUES (?,?,?)",
                                   (rowid, " ".join(_rag_tokens(searchable)), annotation))
                        # 身份仅来自明确字段；正文中的编号只表示提及，不构成活性归属证据。
                        for scope, value in (("identity", identity["enzyme_id"] + " " + identity.get("ec_class", "")),
                                             ("fragment", canonical(searchable))):
                            db.executemany("INSERT OR IGNORE INTO identifier_hits VALUES (?,?,?)",
                                           [(term, rowid, scope) for term in sorted(exact_identifiers(value))])
                if self.library.fingerprint() != fingerprint:
                    raise ValueError("Wiki changed while indexing; initialize a fresh run")
                # Old vectors/generations remain recoverable; no automatic pruning.
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS records_stable_key ON records(stable_key)")
                values = {"fingerprint": fingerprint, "index_version": INDEX_VERSION,
                          "knowledge_version": manifest["knowledge_version"],
                          "card_count": str(len(entries)),
                          "claim_count": str(db.execute("SELECT count(*) FROM records").fetchone()[0])}
                db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", values.items())
                db.commit()
                return {**values, "reused": False, "changed_cards": len(changed), "removed_cards": len(removed)}
            except BaseException:
                db.rollback()
                raise

    def search(self, queries, *, limit=8, semantic_hits=None, semantic_status=None, expected_fingerprint=None,
               include_pool=False, min_similarity=0.25):
        status = self.ensure()
        if expected_fingerprint and status["fingerprint"] != expected_fingerprint:
            raise ValueError("Wiki changed during retrieval; initialize a fresh run")
        if not 1 <= len(queries) <= 3:
            raise ValueError("queries requires 1-3 short independent search questions")
        if any(not isinstance(q, str) or not q.strip() or len(q) > 800 for q in queries):
            raise ValueError("each query must be non-empty and at most 800 characters")
        scores, channels, per_query, similarities, lexical_ids = {}, {}, [], {}, set()
        identifier_matches, identity_matches = {}, {}
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute("BEGIN")
            if db.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone() != (status["fingerprint"],):
                raise ValueError("Wiki index changed during retrieval; initialize a fresh run")
            by_key = dict(db.execute("SELECT stable_key,id FROM records")) if semantic_hits else {}
            record_cards = dict(db.execute("SELECT id,card_id FROM records"))
            for qi, query in enumerate(queries):
                tokens = list(dict.fromkeys(_rag_tokens(query)))[:64]
                local = {}
                for column in ("reaction", "annotation"):
                    if not tokens:
                        continue
                    # Generated quoted tokens, not user-supplied FTS or SQL syntax.
                    match = column + " : (" + " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens) + ")"
                    rows = db.execute(f"SELECT rowid,{column} FROM claims_fts WHERE claims_fts MATCH ? ORDER BY rank LIMIT ?", (match, CHANNEL_LIMIT))
                    for rank, (rid, indexed_text) in enumerate(rows, 1):
                        # A hit on a single generic word must not outweigh several
                        # exact query terms merely by appearing in both channels.
                        coverage = len(set(tokens) & set(indexed_text.split())) / len(tokens)
                        contribution = coverage / (60 + rank)
                        local[rid] = local.get(rid, 0) + contribution
                        channels.setdefault(rid, []).append(f"query_{qi+1}:{column}")
                exact = db.execute("SELECT r.id FROM records r JOIN identifiers i ON i.card_id=r.card_id WHERE i.term=? ORDER BY r.id LIMIT ?",
                                   (query_key(query).casefold(), CHANNEL_LIMIT)).fetchall()
                for rank, (rid,) in enumerate(exact, 1):
                    local[rid] = local.get(rid, 0) + 1 / (60 + rank)
                    channels.setdefault(rid, []).append(f"query_{qi+1}:identifier")
                identifiers = sorted(exact_identifiers(query))
                if identifiers:
                    placeholders = ",".join("?" for _ in identifiers)
                    # 编号通道独立于 FTS/向量通道的 Top-K；先准确召回，再统一分页。
                    for rid, term, scope in db.execute(
                            f"SELECT record_id,term,scope FROM identifier_hits WHERE term IN ({placeholders})", identifiers):
                        local.setdefault(rid, 0)
                        identifier_matches.setdefault(rid, set()).add(term)
                        if scope == "identity":
                            identity_matches.setdefault(rid, set()).add(term)
                        channel = f"query_{qi+1}:exact_identifier:{scope}"
                        if channel not in channels.setdefault(rid, []):
                            channels[rid].append(channel)
                # Participant names use the same lexical/semantic ranks as all
                # other query terms; no bonus or reserved substrate-only lane.
                lexical_ids.update(local)
                if semantic_hits is not None:
                    # 两条 FTS 通道先合成词法排名，再与语义排名等权 RRF；无重排序模型。
                    local = {rid: 0.5 / (60 + rank) for rank, rid in enumerate(
                        sorted(local, key=lambda rid: (-local[rid], rid)), 1)}
                    for rank, (key, similarity) in enumerate(semantic_hits[qi], 1):
                        rid = by_key.get(key)
                        if rid is None:
                            raise ValueError("semantic index contains a stale claim id")
                        local[rid] = local.get(rid, 0) + 0.5 / (60 + rank)
                        channels.setdefault(rid, []).append(f"query_{qi+1}:semantic")
                        similarities[rid] = max(similarities.get(rid, -1), similarity)
                for rid, contribution in local.items():
                    scores[rid] = scores.get(rid, 0) + contribution
                per_query.append(sorted(local, key=lambda rid: (-local[rid], rid)))
            # Give each independent question equal exposure. Within a query,
            # first expose distinct cards, then allow additional source fragments.
            # This schedules evidence, not chemical fitness or candidate ranking.
            diverse_queries = []
            for rows in per_query:
                seen_cards, representatives, remaining = set(), [], []
                for rid in rows:
                    card_id = record_cards[rid]
                    if card_id not in seen_cards:
                        representatives.append(rid)
                        seen_cards.add(card_id)
                    else:
                        remaining.append(rid)
                diverse_queries.append(representatives + remaining)
            ordered, selected_ids = [], set()
            for rank in range(max((len(rows) for rows in diverse_queries), default=0)):
                for rows in diverse_queries:
                    available = next((rid for rid in rows if rid not in selected_ids), None)
                    if available is not None:
                        ordered.append(available)
                        selected_ids.add(available)
            ordered.extend(rid for rid in sorted(scores, key=lambda rid: (-scores[rid], rid)) if rid not in ordered)
            pool = []
            for rid in ordered:
                row = json.loads(db.execute("SELECT summary FROM records WHERE id=?", (rid,)).fetchone()[0])
                pool.append({**row, "retrieval_score": round(scores[rid], 6), "matched_channels": channels[rid],
                             "matched_identifiers": sorted(identifier_matches.get(rid, set())),
                             "identity_identifiers": sorted(identity_matches.get(rid, set())),
                             "semantic_similarity": similarities.get(rid),
                             "weak_lead": rid not in lexical_ids and similarities.get(rid, -1) < min_similarity})
            # Weak semantic-only leads stay available for explicit gap exploration.
            # 明确编号优先于近似语义；同层沿用混合排序。多编号覆盖优先，
            # 覆盖相同时优先明确身份，其余正文命中仍保留“提及”的来源边界。
            pool.sort(key=lambda row: (-len(row["matched_identifiers"]), -len(row["identity_identifiers"]), row["weak_lead"]))
            pool = pool[:POOL_SIZE]
            results, _ = page_rows(pool, 0, set(), limit=limit)
        if self.library.fingerprint() != status["fingerprint"]:
            raise ValueError("Wiki changed during retrieval; initialize a fresh run")
        return {
            "knowledge_version": status["knowledge_version"], "knowledge_fingerprint": status["fingerprint"],
            "results": results, "result_count": len(results), "gap": not results,
            "coverage_status": "not_assessed", "match_status": "matches" if pool else "no_matches",
            "pool_size": len(pool), "pool_truncated": len(scores) > POOL_SIZE,
            **({"_pool": pool} if include_pool else {}),
            "candidate_pool_count": len(scores), "library_card_count": int(status["card_count"]),
            "library_claim_count": int(status["claim_count"]), "index_reused": status["reused"],
            "closed_library_only": True, "complete_library": False,
            "retrieval_mode": "fts5_faiss_rrf" if semantic_hits is not None else "fts5_reaction_annotation_rrf",
            "semantic": semantic_status or {"status": "disabled", "reason": "lexical_only"},
            "channels": {"reaction_bm25": True, "annotation_bm25": True,
                         "semantic_embedding": semantic_hits is not None, "chemical_structure": False},
            "ranking_scope": "词面/语义相关性，不是化学适配度；摘要不能代替原始证据。未召回不代表不存在。",
        }


class RetrievalSession:
    """
    单次运行、单个阶段的有界检索与证据审计会话。

    字段：
    - ``store`` / ``library``：产物存储与只读 Wiki。
    - ``stage``：``mechanism`` 或 ``screen``。
    - ``snapshot`` / ``run``：上游已提交产物和运行元数据。
    - ``state``：查询、分页、已读引用与预算的可恢复状态。
    - ``handoff``：筛选阶段从机制阶段继承的已授权证据。
    """

    def __init__(self, store, library, stage, snapshot):
        if stage not in {"mechanism", "screen"}:
            raise ValueError("unknown retrieval stage")
        self.store, self.library, self.stage, self.snapshot = store, library, stage, snapshot  # 会话依赖与阶段快照。
        self.run = snapshot["run"]  # 已绑定的运行元数据。
        self.path = store._run_path(self.run["run_id"], f"retrieval_{stage}.json")  # 可恢复审计文件。
        self.fingerprint = library.fingerprint()  # 本轮必须始终一致的 Wiki 指纹。
        if self.run.get("knowledge_fingerprint") != self.fingerprint:
            raise ValueError("Wiki changed or this is a legacy run; initialize a fresh run")
        self.upstream = hashlib.sha256(canonical(snapshot["documents"]).encode()).hexdigest()
        self.previous_state = None
        empty = {
            "schema_version": "2.0", "knowledge_fingerprint": self.fingerprint,
            "evidence_format": CHUNK_VERSION, "index_version": INDEX_VERSION,
            "upstream_digest": self.upstream, "rounds": [], "available_refs": [],
            "selected_refs": [], "read_rounds": 0, "pools": {}, "cursors": {}, "pages": {},
            "extra_pages": 0, "context_calls": 0, "errors": [], "summary_count": 0,
            "gap_followups": [], "gap_summary_chars": 0,
            "deferred_summaries": [], "gap_question_delivery": {}, "cursor_gaps": {},
            "inherited_refs": [], "inherited_delivery_attempted": False,
            "epoch": secrets.token_hex(16),
        }
        self.state = store._read_json_if_exists(self.path) or empty
        if self.state.get("schema_version") != "2.0" or self.state.get("index_version") != INDEX_VERSION:
            raise ValueError("retrieval format changed; initialize a fresh run")
        if self.state["knowledge_fingerprint"] != self.fingerprint:
            raise ValueError("Wiki changed; initialize a fresh run")
        if self.state["upstream_digest"] != self.upstream:
            # A bounded Workflow replay can legitimately revise upstream science.
            # Start a new evidence epoch, but never reuse its stale citations.
            self.previous_state, self.state = self.state, empty
            # 科学判断换代，已读且封存的 Wiki 原文不换代；同库指纹下可只读复用。
            snapshots = self.previous_state.get("evidence_snapshots", {})
            self.state["selected_refs"] = list(dict.fromkeys(
                ref for ref in [*self.previous_state.get("selected_refs", []),
                                *self.previous_state.get("inherited_refs", [])] if ref in snapshots))
            self.state["evidence_snapshots"] = {ref: snapshots[ref] for ref in self.state["selected_refs"]}
            self.state["identity_bindings"] = deepcopy(self.previous_state.get("identity_bindings", {}))
            # A legitimate upstream replay gets fresh citations, not fresh cost.
            for key in ("read_rounds", "extra_pages", "context_calls", "summary_count", "errors", "gap_followups", "gap_summary_chars", "gap_checks", "gap_question_delivery"):
                self.state[key] = self.previous_state.get(key, self.state.get(key, {}))
            self.state["spent_queries"] = self.previous_state.get("spent_queries", 0) + len(self.previous_state["rounds"])
            self.state["spent_evidence_chars"] = self.previous_state.get("spent_evidence_chars", 0) + self.previous_state.get("evidence_chars", 0)
        self.handoff = self._load_handoff() if self.stage == "screen" else None
        if self.handoff:
            digest = self.handoff["handoff_digest"]
            existing_digest = str(self.state.get("handoff_digest") or "")
            if existing_digest and existing_digest != digest:
                raise ValueError("stage-2 evidence handoff changed; initialize a fresh screening evidence epoch")
            self.state["handoff_digest"] = digest
            self.state.setdefault("inherited_refs", [])
            valid_refs = set(self._handoff_refs())
            if not set(self.state["inherited_refs"]) <= valid_refs:
                raise ValueError("screening evidence state contains refs outside the stage-2 handoff")
        if self.stage == "screen":
            # 同一运行的蛋白提名不随证据 epoch 丢失；只携带身份，不继承科学判断。
            self.remember_candidate_identities((self.previous_state or {}).get("candidate_nominations", []))
            self.remember_candidate_identities((self.previous_state or {}).get("wiki_candidate_nominations", []))
            self.remember_candidate_identities((self.previous_state or {}).get("candidate_draft", []))
            self.remember_candidate_identities(self.state.get("candidate_draft", []))
            committed_wiki_source = ''
            for name in ("04_candidates.json", "screening_items.draft.json"):
                saved = store._read_json_if_exists(store._run_path(self.run["run_id"], name)) or {}
                if saved.get("run_id") == self.run["run_id"]:
                    if name == '04_candidates.json':
                        committed_wiki_source = hashlib.sha256(canonical(saved).encode()).hexdigest()
                    rows = saved.get("enzyme_candidates", []) if name == "04_candidates.json" else list(saved.get("candidates", {}).values())
                    self.remember_candidate_identities(rows)
            # 本 epoch 的历史清单固定，暂存新候选只更新总账，避免每次暂存改变提交指纹。
            self.state.setdefault("revision_candidate_identities", self.candidate_identities())
            self.state.setdefault("revision_wiki_identities", deepcopy(self.state.get("wiki_candidate_nominations", [])))
            if self.state.get('revision_wiki_source') != committed_wiki_source:
                # 已提交筛选换代才刷新历史视图；单项暂存不改变已交付 context_digest。
                self.state['revision_wiki_identities'] = deepcopy(self.state.get('wiki_candidate_nominations', []))
                self.state['revision_wiki_source'] = committed_wiki_source
            # 固定本 epoch 的身份视图，暂存更新不得使已交付 context_digest 漂移。
            self.state.setdefault("revision_identity_bindings", deepcopy(self.state.get("identity_bindings", {})))
            if self.previous_state and self.previous_state.get("candidate_draft"):
                # 修订继续原始防锚定提名，不让模型重新抄写名称产生身份冲突。
                # 仅复用提名与理由；新评分、机制关联和处置仍须重新提交。
                self.state["candidate_draft"] = self.previous_state["candidate_draft"]
                self.state["candidate_draft_digest"] = self.previous_state["candidate_draft_digest"]
                self.state["candidate_baseline_sent"] = True

    def remember_candidate_identities(self, rows):
        """保存本运行已提出的命名蛋白身份；不按同家族、EC 或名称相似度合并。"""
        memory = {row["proposal_id"]: dict(row) for row in self.state.get("candidate_nominations", [])}
        wiki_memory = {row['card_id']: dict(row) for row in self.state.get('wiki_candidate_nominations', [])}
        for source in rows:
            if source.get('candidate_kind') == 'wiki_enzyme':
                card_ids = list(source.get('card_ids') or ([source['card_id']] if source.get('card_id') else []))
                if len(card_ids) == 1 and source.get('enzyme_name'):
                    identifier = card_ids[0]
                    row = {key: deepcopy(source[key]) for key in ('candidate_id', 'enzyme_name', 'identity_level') if source.get(key)}
                    row.update(card_id=identifier, candidate_kind='wiki_enzyme')
                    row.update(draft_rationale(source.get('proposal_rationale') or source))
                    wiki_memory[identifier] = row
                continue
            if (source.get("candidate_kind") not in (None, "model_enzyme")
                    or source.get("identity_level") not in {"protein", "named_enzyme", "family"}
                    or not source.get("proposal_id") or not source.get("enzyme_name")
                    or source.get("name_basis") in {"target_description", "designed_construct"}):
                continue
            row = {key: source[key] for key in ("proposal_id", "enzyme_name", "identity_level", "specific_name", "prior_specific_name", "name_evidence") if source.get(key)}
            row.update(draft_rationale(source.get("proposal_rationale") or source))
            previous = memory.get(row["proposal_id"])
            if previous and (previous["enzyme_name"] != row["enzyme_name"] or (
                    previous.get("specific_name") and row.get("specific_name") and previous["specific_name"] != row["specific_name"])):
                raise ValueError("candidate nomination ID cannot change protein identity across revisions")
            memory[row["proposal_id"]] = {**(previous or {}), **row}
            if row.get("identity_level") == "family" and row.get("prior_specific_name") and not row.get("specific_name"):
                memory[row["proposal_id"]].pop("specific_name", None)
            verified = source.get("identity_verification") or {}
            if verified.get("status") == "wiki_identity_linked":
                self.state.setdefault("identity_bindings", {})[row["proposal_id"]] = {
                    **row, "candidate_kind": "model_enzyme",
                    "identity_verification": deepcopy(verified)}
        self.state["candidate_nominations"] = list(memory.values())
        self.state['wiki_candidate_nominations'] = list(wiki_memory.values())

    def candidate_identities(self):
        return [dict(row) for row in self.state.get("candidate_nominations", [])]

    def candidate_baseline(self) -> dict[str, Any]:
        """Mark the chemistry-only screening pass before any Wiki evidence is shown."""

        if self.stage != "screen":
            raise ValueError("candidate baseline is only available in the screening stage")
        self.state["candidate_baseline_sent"] = True
        self.save()
        return {
            "status": "candidate_draft_required",
            "instruction": (
                "先只根据目标反应生成独立候选草案；此回包没有 Wiki 卡、检索摘要或继承证据。"
                "下一次调用提交 candidate_draft 后，才能检索和读取 Wiki。"
            ),
        }

    def bind_candidate_draft(self, value: Any) -> list[dict[str, Any]]:
        """Persist one immutable model-authored candidate slate before Wiki delivery."""

        if self.stage != "screen":
            raise ValueError("candidate draft is only available in the screening stage")
        if not self.state.get("candidate_baseline_sent"):
            raise ValueError("first call enzyme_screen_context with run_id only to receive the chemistry-only reaction context")
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_CANDIDATE_DRAFT:
            raise ValueError(f"candidate_draft requires 1-{MAX_CANDIDATE_DRAFT} chemistry-first candidates")
        allowed = {"proposal_id", "enzyme_name", "enzyme_family", "identity_level", "fit_reason", *RATIONALE_PROPERTIES}
        normalized, identifiers, names = [], set(), set()
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) - allowed:
                raise ValueError(f"candidate_draft[{index}] contains unsupported fields")
            proposal_id = str(item.get("proposal_id") or "").strip()
            enzyme_name = str(item.get("enzyme_name") or "").strip()
            enzyme_family = str(item.get("enzyme_family") or "").strip()
            identity_level = str(item.get("identity_level") or "").strip()
            fit_reason = str(item.get("fit_reason") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", proposal_id):
                raise ValueError(f"candidate_draft[{index}].proposal_id is invalid")
            if not enzyme_name or len(enzyme_name) > 120:
                raise ValueError(f"candidate_draft[{index}].enzyme_name must contain 1-120 characters")
            if len(enzyme_family) > 120:
                raise ValueError(f"candidate_draft[{index}].enzyme_family exceeds 120 characters")
            if identity_level not in {"protein", "named_enzyme", "family"}:
                raise ValueError(f"candidate_draft[{index}].identity_level is invalid")
            if not fit_reason or len(fit_reason) > 400:
                raise ValueError(f"candidate_draft[{index}].fit_reason must contain 1-400 characters")
            from jsonschema import Draft202012Validator
            Draft202012Validator({"type": "object", "properties": RATIONALE_PROPERTIES}).validate(item)
            if proposal_id in identifiers or enzyme_name.casefold() in names:
                raise ValueError("candidate_draft proposal_id and enzyme_name must be unique")
            identifiers.add(proposal_id)
            names.add(enzyme_name.casefold())
            row = {
                "proposal_id": proposal_id,
                "enzyme_name": enzyme_name,
                "identity_level": identity_level,
                "fit_reason": fit_reason,
            }
            if enzyme_family:
                row["enzyme_family"] = enzyme_family
            row.update(draft_rationale(item))
            normalized.append(row)
        existing = list(self.state.get("candidate_draft") or [])
        if existing and canonical(existing) != canonical(normalized):
            raise ValueError("candidate_draft is immutable after Wiki evidence becomes available")
        self.state["candidate_draft"] = normalized
        self.remember_candidate_identities(normalized)
        self.state["candidate_draft_digest"] = hashlib.sha256(canonical(normalized).encode()).hexdigest()
        self.save()
        return normalized

    def candidate_draft(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.state.get("candidate_draft") or []]

    @staticmethod
    def _document_digest(value):
        return hashlib.sha256(canonical(value).encode()).hexdigest()

    @staticmethod
    def _refs_in(value, allowed):
        reference_fields = {
            "source_refs", "wiki_refs", "support_refs", "contradiction_refs",
            "evidence_refs",
        }
        found = set()

        def collect(item):
            if isinstance(item, str):
                if item in allowed:
                    found.add(item)
            elif isinstance(item, dict):
                for child in item.values():
                    collect(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    collect(child)

        def visit(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in reference_fields:
                        collect(child)
                    else:
                        visit(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    visit(child)

        visit(value)
        return found

    @staticmethod
    def _claims(packet):
        return {
            claim["ref"]: claim
            for card in packet.get("cards", [])
            for claim in card.get("claims", [])
        }

    @staticmethod
    def _subset_packet(packet, refs):
        selected = set(refs)
        result = {key: value for key, value in packet.items() if key != "cards"}
        cards = []
        for card in packet.get("cards", []):
            claims = [claim for claim in card.get("claims", []) if claim.get("ref") in selected]
            if claims:
                cards.append({**card, "claims": claims})
        result["cards"] = cards
        result["card_count"] = len(cards)
        return result

    @staticmethod
    def _merge_packets(base, *packets):
        cards = {}
        order = []
        for packet in packets:
            for card in packet.get("cards", []):
                card_id = card["card_id"]
                if card_id not in cards:
                    cards[card_id] = {**card, "claims": []}
                    order.append(card_id)
                known = {item["ref"] for item in cards[card_id]["claims"]}
                cards[card_id]["claims"].extend(
                    item for item in card.get("claims", []) if item["ref"] not in known
                )
        result = {**base, "cards": [cards[card_id] for card_id in order]}
        result["card_count"] = len(result["cards"])
        return result

    def seal_handoff(self, documents, packet):
        """封存全部已读原文，区分实际引用与未采用线索，不替模型推荐候选。"""

        if self.stage != "mechanism":
            raise ValueError("only the mechanism stage can seal an evidence handoff")
        allowed = set(self.state["selected_refs"])
        used = self._refs_in(documents, allowed)
        refs = list(self.state["selected_refs"])
        subset = self._subset_packet(packet, refs)
        payload = {
            "schema_version": "1.0",
            "source_stage": "mechanism_and_capability",
            "knowledge_fingerprint": self.fingerprint,
            "evidence_format": CHUNK_VERSION,
            "source_artifact_digests": {
                name: self._document_digest(documents[name])
                for name in ("02_mechanisms.json", "03_capabilities.json")
            },
            "refs": refs,
            "cited_refs": [ref for ref in refs if ref in used],
            "cards": subset["cards"],
        }
        payload["handoff_digest"] = self._document_digest(payload)
        self.state["handoff"] = payload
        return payload

    def _load_handoff(self):
        previous = self.store._read_json_if_exists(
            self.store._run_path(self.run["run_id"], "retrieval_mechanism.json")
        ) or {}
        handoff = previous.get("handoff")
        return self.validate_handoff(handoff, self.fingerprint, self.snapshot["documents"])

    @classmethod
    def validate_handoff(cls, handoff, fingerprint, documents):
        """复用原机制证据前核对封存内容、知识版本和上游产物绑定。"""
        if not isinstance(handoff, dict):
            return None  # Legacy runs retain the explicit re-read path.
        supplied_digest = str(handoff.get("handoff_digest") or "")
        payload = {key: value for key, value in handoff.items() if key != "handoff_digest"}
        if supplied_digest != cls._document_digest(payload):
            raise ValueError("stage-2 evidence handoff digest is invalid")
        if (
            handoff.get("schema_version") != "1.0"
            or handoff.get("source_stage") != "mechanism_and_capability"
            or handoff.get("knowledge_fingerprint") != fingerprint
            or handoff.get("evidence_format") != CHUNK_VERSION
        ):
            raise ValueError("stage-2 evidence handoff is incompatible with this run")
        expected = {
            name: cls._document_digest(documents.get(name))
            for name in ("02_mechanisms.json", "03_capabilities.json")
        }
        if handoff.get("source_artifact_digests") != expected:
            raise ValueError("stage-2 evidence handoff does not match committed stage-2 artifacts")
        refs = list(handoff.get("refs") or [])
        claims = cls._claims(handoff)
        if len(refs) != len(set(refs)) or set(refs) != set(claims):
            raise ValueError("stage-2 evidence handoff refs and snapshots do not match")
        return handoff

    def _handoff_refs(self):
        return list(self.handoff.get("refs") or []) if self.handoff else []

    def _authorized_refs(self):
        # 授权来自当前上游绑定的封存证据，不取决于本阶段是否重复显示过正文。
        # 独立候选草案冻结前仍不开放 Wiki，未读的检索摘要也不因此获得授权。
        sealed = self._handoff_refs() if self.stage == "screen" and self.candidate_draft() else []
        return list(dict.fromkeys([*sealed, *self.state.get("inherited_refs", []), *self.state["selected_refs"]]))

    def _packet_for_refs(self, refs):
        requested = list(dict.fromkeys(refs))
        base = wiki_packet(self.library, self.run)
        handoff_refs = set(self._handoff_refs())
        inherited = self._subset_packet(self.handoff, [ref for ref in requested if ref in handoff_refs]) if self.handoff else base
        local_refs = [ref for ref in requested if ref not in handoff_refs]
        local = wiki_packet(self.library, self.run, refs=local_refs) if local_refs else base
        packet = self._merge_packets(base, inherited, local)
        if self.stage == "mechanism":
            # Expose a deterministic promote-or-reject decision for local
            # mechanism graphs. This does not score evidence or create a
            # hypothesis; the agent must still classify the cited events.
            packet["mechanism_promotion_candidates"] = _mechanism_promotion_hints(packet)
            packet["mechanism_promotion_policy"] = (
                "命中只提供竞争资格；至少覆盖两个独立事件维度的已读片段，必须纳入 wiki_analogy 并保留 anchor_wiki_refs，"
                "或在 knowledge_gaps 说明具体硬冲突，不能静默丢弃。"
            )
        return packet

    def packet(self):
        packet = self._packet_for_refs(self._authorized_refs())
        if self.stage == "screen":
            if self.handoff and self.candidate_draft():
                cited = set(self.handoff.get("cited_refs", self.handoff["refs"]))
                packet["upstream_evidence_index"] = [{
                    "ref": claim["ref"], "card_id": card["card_id"],
                    "heading": claim.get("source_locator", {}).get("heading", ""),
                    "cited_in_stage2": claim["ref"] in cited,
                    "instruction": "已读线索不代表推荐；细节须按 ref 回读，引用不证明目标活性"}
                    for card in self.handoff["cards"] for claim in card.get("claims", [])]
            packet["candidate_nominations"] = [dict(row) for row in self.state.get("revision_candidate_identities", [])]
            packet['wiki_candidate_nominations'] = deepcopy(self.state.get('revision_wiki_identities', []))
            packet["identity_bindings"] = deepcopy(self.state.get("revision_identity_bindings", {}))
        return packet

    def _restore_handoff(self):
        """复用已绑定当前上游的只读证据；不发查询、不刷新探索预算或科学判断。"""
        if self.stage != "screen" or not self.handoff or not self.candidate_draft():
            return
        # 只恢复已核验的封存原文供服务端验证；inherited_refs 仍由实际正文交付更新。
        # 不能因缓存恢复就声称本阶段已向模型显示了所有证据。
        packet = self.packet()
        self.state.setdefault("evidence_snapshots", {}).update(
            {claim["ref"]: claim for card in packet["cards"] for claim in card["claims"]})
        self.save()

    def delivery(self, context, response):
        """Send upstream science once; keep the full canonical digest for commits.

        Only the transport view is reduced. Artifacts, evidence, retrieval ranks
        and the context used to validate a commit are never rewritten.
        """
        data = {**context, **response}
        if not self.include_upstream:
            for key in ("reaction", "mechanisms", "cofactors", "knowledge_gaps", "catalytic_capabilities", "evidence_ledger"):
                data.pop(key, None)
            # 已交付的身份清单不用每次重复；提交校验仍使用完整 canonical context。
            generation = dict(data.get("candidate_generation") or {})
            if generation:
                generation["prior_candidates_count"] = len(generation.pop("prior_candidates", []))
                data["candidate_generation"] = generation
        data["context_delivery"] = "full" if self.include_upstream else "delta"
        data["commit_receipt"] = {"context_digest": context["context_digest"],
                                  "instruction": "提交前复制本次最新指纹；每次读新原文后可能改变，不能沿用前一条工具回包。"}
        if self.stage == "screen":
            # 身份信息与反应片段独立交付。已读卡的头信息不必靠化学查询再召回，
            # 但同蛋白对应仍由模型显式提交并由程序校验，不从名称相似度自动绑定。
            data["protein_identity_evidence"] = [{
                "card_id": card["card_id"],
                "display_name": card.get("display_name", ""),
                "enzyme_id": card.get("enzyme_id", ""),
                "organism": card.get("organism", ""),
                "identity_level": card.get("identity_level", ""),
                "source_path": card.get("source_path", ""),
                "read_refs": [claim["ref"] for claim in card.get("claims", [])],
                "scope": "本卡身份元数据；不是目标活性证据",
            } for card in context.get("cards", [])]
            data["identity_review_tasks"] = [{
                "proposal_id": row["proposal_id"],
                "enzyme_name": row["enzyme_name"],
                "action": "核对已读卡名称、来源物种及变体边界；同一具体蛋白则显式提交 identity_level=protein 与 identity_binding。仅缺目标活性不妨碍身份核实。无匹配卡时按名称/来源定向检索，仍不确定则保留未核实，不循环请求。",
            } for row in self.candidate_identities()
                if row.get("identity_level") in {"protein", "named_enzyme"}
                and row["proposal_id"] not in self.state.get("identity_bindings", {})]
            data["candidate_identity_receipt"] = {
                "existing_proposal_ids": [row["proposal_id"] for row in self.candidate_draft()],
                "instruction": "已有 proposal_id 的候选提交省略 enzyme_name，由程序复用冻结名称；只更新科学判断。真正的新酶使用新 ID 并说明 discovery_reason；不得改写旧 ID 的身份。"}
        self.state["upstream_context_sent"] = True
        return data

    @staticmethod
    def _summary_view(row):
        # Bibliographic SRC IDs are not readable claim refs. Keep full provenance
        # in the audit and original evidence, not as competing IDs in summaries.
        return summary_row(row)

    def _page(self, pool_id, token=None, *, allow_weak=False, reason="", gaps=()):
        if token in self.state["pages"]:
            return self.state["pages"][token]
        pool = self.state["pools"][pool_id]
        gaps = gaps or self.state.get("cursor_gaps", {}).get(token, [])
        first = token is None
        exhausted = ((not first and not gaps and self.state["extra_pages"] >= MAX_EXTRA_PAGES)
                     or self.state["summary_count"] >= MAX_SUMMARIES)
        gaps = self.order_gap_questions(gaps)
        pending = {item["ref"]: item for item in self.state.get("deferred_summaries", [])
                   if item["ref"] not in self.state["available_refs"]}
        candidates = (gap_ranked_rows(pool["rows"], gaps, set(self.state["available_refs"]),
                                     deferred={ref: item.get("question", "") for ref, item in pending.items()})
                      if gaps else sorted(pool["rows"], key=lambda row: row["ref"] not in pending))
        remaining_chars = max(0, MAX_DELIVERY_CHARS - self.state.get("gap_summary_chars", 0))
        # 每页最多花原缺口总额度的一个查询轮份额；给后续不同机制留空间。
        max_chars = min(8500, remaining_chars, MAX_DELIVERY_CHARS // MAX_SEARCH_ROUNDS) if gaps else 8500
        blocked = []
        rows, refs = page_rows(candidates, 0, set(self.state["available_refs"]),
                               limit=min(PAGE_SIZE, MAX_SUMMARIES - self.state["summary_count"]),
                               first=first or bool(gaps), allow_weak=allow_weak,
                               max_chars=max_chars, deferred=blocked) if not exhausted else ([], set())
        self.state["available_refs"] = sorted(set(self.state["available_refs"]) | refs)
        self.state["summary_count"] += len(rows)
        if gaps and rows:
            self.state["gap_summary_chars"] = self.state.get("gap_summary_chars", 0) + len(canonical(rows))
            for row in rows:
                key = row["gap_match"]["question_id"]
                usage = self.state.setdefault("gap_question_delivery", {}).setdefault(key, {"summary_count": 0, "summary_chars": 0})
                usage["summary_count"] += 1
                usage["summary_chars"] += len(canonical(row))
        for row in blocked:
            pending[row["ref"]] = {"ref": row["ref"], "question": row.get("gap_match", {}).get("question", "")}
        self.state["deferred_summaries"] = [item for ref, item in pending.items() if ref not in refs]
        if not first and not exhausted:
            self.state["extra_pages"] += 1
        unread = [row for row in pool["rows"] if row["ref"] not in self.state["available_refs"]]
        next_cursor = None
        if unread and rows and not exhausted and (gaps or self.state["extra_pages"] < MAX_EXTRA_PAGES) and self.state["summary_count"] < MAX_SUMMARIES:
            binding = [self.run["run_id"], self.stage, self.fingerprint, INDEX_VERSION,
                       self.state["epoch"], pool_id, len(self.state["cursors"]), pool["digest"]]
            next_cursor = "pg_" + hashlib.sha256(canonical(binding).encode()).hexdigest()
            self.state["cursors"][next_cursor] = pool_id
            if gaps:
                self.state.setdefault("cursor_gaps", {})[next_cursor] = gaps
        page = {**pool["metadata"], "results": [self._summary_view(row) for row in rows],
                "result_count": len(rows), "returned_result_count": len(rows),
                "has_more": bool(unread), "next_cursor": next_cursor,
                "unread_count": len(unread), "weak_leads_remaining": sum(row.get("weak_lead", False) for row in unread),
                "budget_exhausted": exhausted or (bool(unread) and next_cursor is None),
                "coverage_status": "not_assessed", "page_reason": reason,
                "deferred_count": len(self.state["deferred_summaries"]),
                "delivery_stop_reason": ("deferred_for_chars" if next_cursor else "summary_budget_cannot_fit")
                                        if blocked else None,
                "summary_chars": len(canonical(rows)) if rows else 0,
                "gap_questions": [{"question_id": question_key(q), "question": q} for q in gaps]}
        if token:
            self.state["pages"][token] = page
        pool.setdefault("deliveries", []).append({"refs": sorted(refs), "reason": reason, "cursor": token})
        return page

    def order_gap_questions(self, questions):
        """优先给已交付字数较少的问题分配本页位置，不把交付次数当科学覆盖。"""
        usage = self.state.get("gap_question_delivery", {})
        return sorted(dict.fromkeys(questions), key=lambda q: usage.get(question_key(q), {}).get("summary_chars", 0))

    def gap_focus(self, gaps, questions=None):
        """只复用模型提出的问题；科学限制不能直接变成新 embedding 查询。"""
        focus = list(dict.fromkeys(questions or []))
        if not focus:
            original = list(dict.fromkeys(q for batch in self.state["rounds"] for q in batch.get("queries", [])))
            tokens = {q: gap_terms(q) for q in original}
            frequencies = Counter(token for terms in tokens.values() for token in terms)
            for gap in gaps:
                terms = gap_terms(gap)
                ranked = sorted(self.order_gap_questions(original),
                    key=lambda q: sum(1 / frequencies[t] for t in tokens[q] & terms), reverse=True)
                if ranked and tokens[ranked[0]] & terms and ranked[0] not in focus:
                    focus.append(ranked[0])
            for question in self.order_gap_questions(original):
                if len(focus) >= 3:
                    break
                if question not in focus:
                    focus.append(question)
        return self.order_gap_questions(focus[:3])

    def gap_budget_exhausted(self):
        """补查和补查预约共用停止线，为原文读取与提交保留空间。"""
        return ((self.stage == "mechanism" and self.state.get("mechanism_actions", 0) >
                 MECHANISM_SUBMISSION_POLICY["max_steps"] - MECHANISM_SUBMISSION_POLICY["closing_turns"])
                or self.state["context_calls"] >= MAX_CONTEXT_CALLS - 2
                or self.state["summary_count"] >= MAX_SUMMARIES
                or self.state["read_rounds"] >= MAX_READ_ROUNDS
                or len(self.state["selected_refs"]) >= MAX_SELECTED_REFS
                or self.state.get("evidence_chars", 0) + self.state.get("spent_evidence_chars", 0) >= MAX_EVIDENCE_CHARS
                or self.state.get("gap_summary_chars", 0) >= MAX_DELIVERY_CHARS)

    def queue_gap_check(self, gaps):
        """成功暂存后预约一次缺口检查；不重试工具、不改写已存科学内容。"""
        gaps = list(dict.fromkeys(g for g in gaps if isinstance(g, str) and g.strip()))[:3]
        if not gaps and not self.state.get("pending_gap_check"):
            return None
        if self.gap_budget_exhausted():
            self.state.pop("pending_gap_check", None)
            self.state["gap_coverage"] = {"status": "unresolved", "stop_reason": "bounded_exploration_budget"}
            self.save()
            return None
        focus = self.gap_focus(gaps)
        key = hashlib.sha256(canonical(sorted(query_key(q).casefold() for q in focus)).encode()).hexdigest()
        if gaps and focus and key not in self.state.get("gap_checks", {}):
            self.state.setdefault("pending_gap_check", gaps)
            self.save()
        return self.gap_check_receipt()

    def gap_check_receipt(self):
        if not self.state.get("pending_gap_check"):
            return None
        return {"status": "gap_check_pending",
                "next_tool": "enzyme_mechanism_context" if self.stage == "mechanism" else "enzyme_screen_context",
                "instruction": "条目已保存，无须重发；补查预约不阻止后续有效条目暂存。下一次 context 优先检查缺口摘要，明确请求的 read_refs 正常执行。预算允许时回读；提交仍检查未解决缺口，预算耗尽则保留未知并收尾。"}

    async def continue_gaps(self, gaps, retriever=None, questions=None, *, finish=False):
        """提交前的有界探索，不是校验重试；证据没有进展时允许带未知提交。"""
        if questions is not None and (not isinstance(questions, list) or len(questions) > 3 or any(
                not isinstance(q, str) or not q.strip() or len(q) > 240 for q in questions)):
            raise ValueError("retrieval_questions requires up to 3 non-empty questions of at most 240 characters")
        gaps = list(dict.fromkeys(str(g).strip() for g in
                    [*self.state.get("pending_gap_check", []), *gaps] if str(g).strip()))[:3]
        # 科学限制不是检索问题。优先用模型明确提交的短问题；旧调用复用其既有查询，
        # 不把“未收录/未验证/需要实验”整段负面描述当新 embedding 查询。
        focus = self.gap_focus(gaps, questions)
        self.state.pop("pending_gap_check", None)
        if not focus:
            self.state["gap_coverage"] = {"status": "unresolved", "stop_reason": "needs_retrieval_question"}
            self.save()
            return None
        history = self.state.setdefault("gap_followups", [])
        key = hashlib.sha256(canonical(sorted(query_key(g).casefold() for g in focus)).encode()).hexdigest()
        checked = key in self.state.setdefault("gap_checks", {})
        prior = [row for row in history if row["key"] == key]
        cached = {}
        for pool in self.state["pools"].values():
            for row in pool["rows"]:
                previous = cached.get(row["ref"])
                if previous is None or row.get("retrieval_score", 0) > previous.get("retrieval_score", 0):
                    cached[row["ref"]] = row
        pending = {item["ref"] for item in self.state.get("deferred_summaries", [])
                   if item["ref"] not in self.state["available_refs"] and
                   (not item.get("question") or item["question"] in focus)}
        choices = gap_ranked_rows(list(cached.values()), focus, set(self.state["available_refs"]), deferred=pending)
        has_deferred = bool(pending & {row["ref"] for row in choices})
        authorized = len(self._authorized_refs())
        stop = None
        if not gaps:
            stop = "no_open_gaps"
        elif any(row.get("terminal") for row in history):
            stop = "retrieval_error"
        elif prior and prior[-1].get("delivery_blocked"):
            stop = "summary_budget_cannot_fit"
        elif len(prior) >= 2 and authorized <= prior[-2]["authorized_count"]:
            stop = "no_new_read_evidence"
        elif self.gap_budget_exhausted():
            stop = "bounded_exploration_budget"
        elif finish and checked and not has_deferred:
            stop = "finish_after_gap_check"
        if stop:
            self.state["gap_coverage"] = {"status": "unresolved" if gaps else "no_open_gaps", "stop_reason": stop}
            self.save()
            return None
        # 同问题已有相关条目待续送时，先消化缓存，不额外调用 embedding。
        arguments = {"reason": "未解决机制缺口的定向查证", "unresolved_gaps": focus}
        seen = {q for row in self.state["rounds"] for q in row["query_keys"]}
        queries = [g for g in focus if len(g) <= 240 and query_key(g) not in seen]
        can_search = self.state.get("spent_queries", 0) + len(self.state["rounds"]) < MAX_SEARCH_ROUNDS
        # 缓存页没带来原文进展时，允许一次新缺口查询；不把全部预算花在旧池上。
        targeted_search = can_search and queries and not has_deferred
        if choices and not targeted_search:
            pool_id = "gap_" + hashlib.sha256(canonical([focus, list(cached)]).encode()).hexdigest()
            self.state["pools"][pool_id] = {
                "rows": list(cached.values()), "digest": pool_id,
                "metadata": {"retrieval_mode": "cached_gap_followup", "coverage_status": "not_assessed"}}
            token = "pg_" + hashlib.sha256(canonical([self.state["epoch"], pool_id, len(history), key]).encode()).hexdigest()
            self.state["cursors"][token] = pool_id
            arguments["cursor"] = token
            # 长缺口不截断成科学结论；仅用于缓存排序，不作为查询发送。
            arguments["unresolved_gaps"] = focus
        elif can_search:
            if queries:
                arguments["queries"] = queries
        if not arguments.get("cursor") and not arguments.get("queries"):
            self.state["gap_coverage"] = {"status": "unresolved", "stop_reason": "no_new_search_action"}
            self.save()
            return None
        from .semantic_tools import SemanticUnavailable
        try:
            response = await self.context(arguments, retriever)
        except (OSError, sqlite3.Error, SemanticUnavailable) as exc:
            # 后端故障不重试，不冒充无匹配或科学冲突；当前已读证据仍可提交。
            self.state["gap_coverage"] = {"status": "unresolved", "stop_reason": "retrieval_error", "error_type": type(exc).__name__}
            history.append({"key": key, "authorized_count": authorized, "refs": [], "terminal": True})
            self.save()
            return None
        result = response.get("retrieval", {})
        rows = result.get("results", [])
        if not rows:
            blocked = result.get("delivery_stop_reason") == "summary_budget_cannot_fit"
            self.state["gap_coverage"] = {"status": "unresolved", "stop_reason":
                                          "summary_budget_cannot_fit" if blocked else "no_new_summaries"}
            history.append({"key": key, "authorized_count": authorized, "refs": [], "delivery_blocked": blocked})
            self.state["gap_checks"][key] = {"questions": focus, "delivered": False}
            self.save()
            return None
        self.state["gap_checks"][key] = {"questions": focus, "delivered": True}
        history.append({"key": key, "questions": focus, "authorized_count": authorized, "refs": [r["ref"] for r in rows]})
        self.state["gap_coverage"] = {"status": "unresolved", "stop_reason": None}
        self.save()
        return {"committed": False, "followup_required": True,
                "retrieval": {key: result.get(key) for key in (
                    "results", "next_cursor", "has_more", "unread_count", "retrieval_mode",
                    "gap_questions", "deferred_count", "delivery_stop_reason", "summary_chars")},
                "evidence_delta": {key: response.get(key) for key in ("cards", "selected_refs", "notices")},
                "coverage_status": "not_assessed",
                "instruction": "发现未解决机制缺口且仍有未读线索，本次尚未提交。仅核对这些新摘要，必要时用 read_refs 回读。新证据不强制新增或保留任何候选；仅修改受影响机制/候选，沿用已有 ID。没有适配证据仍保留未知；随后再次提交。原文未读不得引用，不重复提交未变化的读取请求。"}

    def mechanism_action_window(self):
        """工具侧落实同一收尾规则；计数持久化，失败/续跑不能清零探索消耗。"""
        completed = self.state.get("mechanism_actions", 0)
        window = mechanism_submission_window(completed)
        self.state["mechanism_actions"] = completed + 1
        self.save()
        return window

    async def context(self, arguments, retriever=None):
        pending = self.state.get("pending_gap_check")
        if pending and not (arguments.get("read_refs") or arguments.get("resume")):
            # 暂存回执只发小指令；摘要仍走既有大容量 context 工具，避免截断。
            continuation = await self.continue_gaps(pending, retriever)
            if continuation:
                return {**self._packet_for_refs([]), **continuation,
                        "cards": (continuation.get('evidence_delta') or {}).get('cards') or [],
                        "selected_refs": self._authorized_refs(),
                        "notices": ["先交付已预约的缺口补查；本次其他检索参数未执行，请按新摘要决定下一步。"]}
        # 交接是既有证据恢复，不应被新检索调用额度耗尽或 resume 分支阻断。
        self._restore_handoff()
        token = arguments.get("cursor")
        policy = getattr(retriever, "policy", "lexical-default")
        if self.state.get("policy", policy) != policy:
            raise ValueError("retrieval policy changed; initialize a fresh run")
        self.state["policy"] = policy
        repeated_page = bool(token and token in self.state["pages"] and not any(
            arguments.get(key) for key in ("queries", "read_refs", "resume")))
        self.include_upstream = not self.state.get("upstream_context_sent", False)
        refs = arguments.get('read_refs') or []
        aliases = citation_index(self.packet()) if any(str(ref).startswith('W') for ref in refs) else {}
        cached_read = (not any(arguments.get(k) for k in ('queries', 'cursor')) and
                       (bool(arguments.get('resume')) or bool(refs) and
                        all(aliases.get(ref, ref) in self._authorized_refs() for ref in refs)))
        exhausted = self.state['context_calls'] >= MAX_CONTEXT_CALLS
        if not repeated_page and exhausted and not cached_read:
            return {**self._packet_for_refs([]), "selected_refs": self._authorized_refs(),
                    "retrieval": {"budget_exhausted": True, "coverage_status": "not_assessed"},
                    "notices": ["context 调用预算耗尽；不重复发送全部旧原文。已授权 ref 仍有效，必要时仅按 read_refs 定向恢复；不新增检索或读取，请提交并注明未知。"]}
        if not repeated_page and not exhausted:
            self.state["context_calls"] += 1
        try:
            return await self._context(arguments, retriever)
        except ValueError:
            self.state["errors"].append(hashlib.sha256(canonical(arguments).encode()).hexdigest())
            self.save()  # A failed call cannot refresh query/embedding budgets.
            raise

    async def _context(self, arguments, retriever=None):
        queries = list(arguments.get("queries") or [])
        requested = list(dict.fromkeys(arguments.get("read_refs") or []))
        aliases = citation_index(self.packet()) if any(str(ref).startswith("W") for ref in requested) else {}
        requested = list(dict.fromkeys(aliases.get(ref, ref) for ref in requested))
        if len(queries) > 3 or any(not isinstance(q, str) or not q.strip() or len(q) > 240 for q in queries):
            raise ValueError("queries allows up to 3 non-empty questions of at most 240 characters")
        if len(requested) > 6:
            raise ValueError("read_refs allows at most 6 claim refs per call")
        reason = str(arguments.get("reason") or "").strip()
        cursor = arguments.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or cursor not in self.state["cursors"]):
            raise ValueError("invalid or foreign pagination cursor; use this stage's next_cursor")
        if (queries or requested or cursor) and (not reason or len(reason) > 240):
            raise ValueError("retrieval requires a short reason (input event, competing explanation or missing evidence)")
        result, notices = {}, []
        handoff_refs = set(self._handoff_refs())
        if handoff_refs:
            # Suppress already-proven stage-2 evidence from new search pages.
            # It remains available through the immutable handoff packet.
            self.state["available_refs"] = sorted(
                set(self.state["available_refs"]) | handoff_refs
            )
        resume = bool(arguments.get("resume", False))
        self.include_upstream = (not self.state.get("upstream_context_sent", False)
                                 or (resume and self.state.get("resume_batches", 0) < 2))
        if (resume and (queries or cursor)) or (cursor and queries):
            raise ValueError("resume only restores cached evidence; do not combine it with new queries")
        if cursor and requested:
            raise ValueError("pagination only returns cached summaries; read_refs requires a separate call")
        if cursor:
            result = self._page(self.state["cursors"][cursor], cursor, allow_weak=bool(arguments.get("allow_weak", False)), reason=reason,
                                gaps=arguments.get("unresolved_gaps") or [])
        seen = {key for r in self.state["rounds"] for key in r["query_keys"]}
        fresh = []
        for query in queries:
            key = query_key(query)
            if key and key not in seen:
                fresh.append(query)
                seen.add(key)
        if self.stage == "mechanism" and not self.state.get("rounds") and fresh:
            # Add a role-derived query only when the caller left a free lane;
            # replacing its indirect-pathway query would narrow exploration.
            bridge_query = _derive_mechanism_bridge_query(self.snapshot)
            bridge_key = query_key(bridge_query)
            has_bridge = any(
                re.search(r"局部反应事件图|中间体|天然终止|产物释放|终止方式", query, re.I)
                for query in fresh
            )
            if bridge_query and not has_bridge and bridge_key not in seen and len(fresh) < 3:
                fresh.append(bridge_query)
                notices.append("已加入基于净变化的机制桥接查询；它只扩大跨反应类比候选，不代表目标活性已成立。")
        if queries and not fresh:
            notices.append("重复查询已跳过；使用已有结果，只有新的科学缺口才补查。")
            pages = [*self.state["rounds"], *self.state["pages"].values()]
            result["cached_pages"] = [{"next_cursor": p.get("next_cursor"),
                                        "has_more": p.get("has_more", bool(p.get("next_cursor")))}
                                       for p in pages if p.get("next_cursor")][-2:]
        if fresh and self.state.get("spent_queries", 0) + len(self.state["rounds"]) >= MAX_SEARCH_ROUNDS:
            notices.append("检索轮次已达上限；提交当前结果并注明覆盖限制，不继续重试。")
            fresh = []
        if fresh:
            previously_available = set(self.state["available_refs"])
            index = WikiIndex(self.library, self.store._workspace, self.store._workspace_dir)
            backend_path = self.store._run_path(self.run["run_id"], "retrieval_backend.json")
            backend = self.store._read_json_if_exists(backend_path) or {}
            if retriever:
                result = await retriever.search(index, fresh, backend=backend, include_pool=True)
                self.store._atomic_write(backend_path, canonical(backend) + "\n")
            else:
                result = index.search(fresh, include_pool=True)
            pool_rows = result.pop("_pool")
            pool_id = hashlib.sha256(canonical([fresh, pool_rows, self.state["epoch"]]).encode()).hexdigest()
            metadata = {k: v for k, v in result.items() if k not in {"results", "result_count"}}
            self.state["pools"][pool_id] = {"rows": pool_rows, "metadata": metadata,
                                            "digest": hashlib.sha256(canonical(pool_rows).encode()).hexdigest()}
            result = self._page(pool_id, allow_weak=bool(arguments.get("allow_weak", False)), reason=reason,
                                gaps=arguments.get("unresolved_gaps") or fresh)
            repeated = [r for r in pool_rows if r["ref"] in previously_available][:PAGE_SIZE]
            result["repeated_results"] = [{"ref": r["ref"], "retrieval_score": r["retrieval_score"]} for r in repeated]
            refs = [hit["ref"] for hit in result["results"]]
            audited_refs = refs + [r["ref"] for r in repeated]
            self.state["rounds"].append({"queries": fresh, "query_keys": [query_key(q) for q in fresh],
                                         "reason": reason, "result_refs": audited_refs,
                                         "results": [r for r in pool_rows if r["ref"] in audited_refs],
                                         "next_cursor": result["next_cursor"],
                                         "retrieval_mode": result["retrieval_mode"],
                                         "semantic": result["semantic"],
                                         "candidate_pool_count": result["candidate_pool_count"]})
        if result.get("results"):
            seed_cards = [row["card_id"] for row in result["results"]]
            seed_cards.extend(row["card_id"] for batch in self.state["rounds"][-2:]
                              for row in batch["results"])
            max_steps = self.run.get("reaction_input", {}).get("context", {}).get("max_route_steps", 8)
            scaffolds = native_pathway_scaffolds(self.library, seed_cards, max_steps)
            if scaffolds:
                self.state["native_scaffolds"] = scaffolds
                self.state["available_refs"] = sorted(set(self.state["available_refs"]) | {
                    ref for path in scaffolds for ref in path["read_refs"]
                })
        # 修订仅使科学判断失效；同库指纹下封存的已读原文仍可按原 ref 回读。
        # 新摘要、外来 run 的 ref 和未读引用不能借此获得授权。
        available = set(self.state["available_refs"]) | set(self._authorized_refs())
        if self.stage == "screen":
            previous = self.store._read_json_if_exists(self.store._run_path(self.run["run_id"], "retrieval_mechanism.json"))
            if previous and previous["knowledge_fingerprint"] == self.fingerprint:
                legacy_refs = previous["selected_refs"] if not self.handoff else []
                available.update(legacy_refs)
                result["upstream_read_refs"] = self._handoff_refs() or legacy_refs
        read_aliases = reference_index(available, "R") if any(str(ref).startswith("R") for ref in requested) else {}
        requested = list(dict.fromkeys(read_aliases.get(ref, ref) for ref in requested))
        if not set(requested) <= available:
            raise ValueError("read_refs must come from this run's retrieved results[].ref or upstream read evidence; "
                             "use results[].ref, not bibliographic SRC-… IDs")
        restored = []
        if resume:
            if self.state.get("resume_batches", 0) >= 2:
                notices.append("缓存恢复已达两批上限；不重复回放，不增加探索预算。")
            else:
                previous = set(self.state.get("restored_refs", []))
                authorized = self._authorized_refs()
                restore_refs = requested or [r for r in authorized if r not in previous][:6]
                if not set(restore_refs) <= set(authorized):
                    raise ValueError("resume read_refs must already have been read; use normal reading for new evidence")
                for ref in restore_refs:
                    if ref not in previous:
                        delivery = self._packet_for_refs([*restored, ref])
                        if len(canonical(delivery)) <= MAX_DELIVERY_CHARS:
                            restored.append(ref)
                self.state["restored_refs"] = sorted(previous | set(restored))
                self.state["resume_batches"] = self.state.get("resume_batches", 0) + 1
                if self.state["rounds"] and not self.state.get("restored_search"):
                    result["cached_results"] = [self._summary_view(hit) for hit in self.state["rounds"][-1]["results"]]
                    self.state["restored_search"] = True
                notices.append("仅恢复本 run 已检索/已读材料；未重新检索，探索预算不变。")
            requested = []
        if not resume:
            # 普通 read_refs 也可回放已封存原文，无需新查询或再次消耗读取额度。
            for ref in requested:
                if ref in self._authorized_refs():
                    tentative = self._packet_for_refs([*restored, ref])
                    if len(canonical(tentative)) + len(canonical(result)) <= MAX_DELIVERY_CHARS:
                        restored.append(ref)
            self.state["available_refs"] = sorted(set(self.state["available_refs"]) | set(restored))
        inherited_now = []
        inherited = set(self.state.get("inherited_refs", []))
        if self.handoff and not resume:
            requested_handoff = [ref for ref in requested if ref in handoff_refs and ref not in inherited]
            automatic_handoff = []
            if not self.state.get("inherited_delivery_attempted", False):
                automatic_handoff = [ref for ref in self._handoff_refs() if ref not in inherited]
                self.state["inherited_delivery_attempted"] = True
            for ref in dict.fromkeys([*requested_handoff, *automatic_handoff]):
                delivery = self._packet_for_refs([*inherited_now, ref])
                if len(canonical(delivery)) + len(canonical(result)) > MAX_DELIVERY_CHARS:
                    continue
                inherited_now.append(ref)
            if inherited_now:
                self.state["inherited_refs"] = list(
                    dict.fromkeys([*self.state.get("inherited_refs", []), *inherited_now])
                )
                self.state.setdefault("inherited_reads", []).append(
                    {
                        "refs": inherited_now,
                        "reason": reason or "阶段二已引用证据的自动交接",
                        "handoff_digest": self.handoff["handoff_digest"],
                    }
                )
                notices.append(
                    f"已从阶段二校验交接 {len(inherited_now)} 条实际引用证据；未重新读取 Wiki，且不占本阶段检索/读取预算。"
                )
            remaining_handoff = [
                ref for ref in self._handoff_refs() if ref not in self.state.get("inherited_refs", [])
            ]
            result["upstream_read_refs"] = remaining_handoff
            if remaining_handoff:
                notices.append(
                    f"另有 {len(remaining_handoff)} 条封存原文未在本阶段重送，但其知识版本和上游绑定已核验，可按原 ref 复用；需要查看正文时按 upstream_read_refs 分批读取，不重算 embedding。"
                )
        authorized_before_new = set(self._authorized_refs())
        new_refs = [
            ref for ref in requested
            if ref not in authorized_before_new and ref not in handoff_refs
        ]
        delivered = []
        if new_refs and self.state["read_rounds"] >= MAX_READ_ROUNDS:
            notices.append("证据读取轮次已达上限；保留未知，不继续重试。")
            new_refs = []
        for ref in new_refs:
            if len(self.state["selected_refs"]) >= MAX_SELECTED_REFS:
                notices.append("本阶段证据条数已达上限；未读证据不可引用。")
                break
            tentative = wiki_packet(self.library, self.run, refs=[*self.state["selected_refs"], ref])
            delivery = wiki_packet(self.library, self.run, refs=[*delivered, ref])
            if (len(canonical(tentative)) + self.state.get("spent_evidence_chars", 0) > MAX_EVIDENCE_CHARS
                    or len(canonical(delivery)) + len(canonical(result)) > MAX_DELIVERY_CHARS):
                notices.append("证据超出本轮或累计长度预算，未截断原文；未送达的 ref 不可引用。")
                continue
            self.state["selected_refs"].append(ref)
            delivered.append(ref)
        if delivered:
            self.state["read_rounds"] += 1
            self.state.setdefault("reads", []).append({"refs": delivered, "reason": reason})
        packet = self._packet_for_refs([*inherited_now, *restored, *delivered])
        self.state["evidence_chars"] = len(
            canonical(wiki_packet(self.library, self.run, refs=self.state["selected_refs"]))
        )
        self.state.setdefault("evidence_snapshots", {}).update({c["ref"]: c for card in packet["cards"] for c in card["claims"]})
        # 仅回放已封存证据时不重读全库；新读取才扩展天然反应邻接线索。
        neighbors = native_reaction_neighbors(self.library, [card["card_id"] for card in packet["cards"]]) if delivered else []
        self.state["available_refs"] = sorted(set(self.state["available_refs"]) | {
            row["read_ref"] for row in neighbors if row["read_ref"]
        })
        authorized = self._authorized_refs()
        return {
            **packet,
            "retrieval": result, "notices": notices,
            "selected_refs": authorized,
            "unread_retrieved_refs": sorted(set(self.state["available_refs"]) - set(authorized)),
            "inherited_refs": list(self.state.get("inherited_refs", [])),
            "new_read_refs": list(delivered),
            "adjacent_native_reactions": neighbors,
            "native_pathway_scaffolds": self.state.get("native_scaffolds", []),
            "evidence_handoff": {
                "status": (
                    "verified" if self.handoff else
                    "legacy_explicit_read" if self.stage == "screen" else
                    "not_applicable"
                ),
                "source_stage": "mechanism_and_capability" if self.handoff else None,
                "handoff_digest": self.handoff.get("handoff_digest") if self.handoff else None,
                "available_count": len(self._handoff_refs()),
                "authorized_count": len(self._handoff_refs()) if self.candidate_draft() else 0,
                "inherited_count": len(self.state.get("inherited_refs", [])),
                "remaining_count": len(set(self._handoff_refs()) - set(self.state.get("inherited_refs", []))),
            },
            "undelivered_refs": [r for r in requested if r not in set([*inherited_now, *restored, *delivered])],
            "remaining": {"search_rounds": max(0, MAX_SEARCH_ROUNDS - len(self.state["rounds"]) - self.state.get("spent_queries", 0)),
                          "extra_pages": max(0, MAX_EXTRA_PAGES - self.state["extra_pages"]),
                          "context_calls": MAX_CONTEXT_CALLS - self.state["context_calls"],
                          "summary_count": MAX_SUMMARIES - self.state["summary_count"],
                          "read_rounds": MAX_READ_ROUNDS - self.state["read_rounds"],
                          "evidence_refs": MAX_SELECTED_REFS - len(self.state["selected_refs"]),
                          "resume_batches": 2 - self.state.get("resume_batches", 0)},
            "protocol": "机制阶段按目标反应中心、键变化或催化能力提出 queries+reason，先回读 MD 再补查缺口；筛选阶段先使用已校验的 inherited_refs。native_pathway_scaffolds 与 adjacent_native_reactions 仅按天然反应共享 ChEBI 参与物给出可读原文线索，不是证据、反应方向或目标路线；read_refs 可直接使用线索中的 read_ref(s)，读后逐步核对主碳骨架、方向、共底物和来源。只有 selected_refs 可引用。cursor=next_cursor+reason 取同次检索下一批；resume 只恢复已授权材料，不重置预算。命中不代表化学覆盖，排名不是活性分。到预算上限保留未知，不循环请求。",
        }

    def save(self):
        if self.previous_state:
            history = self.store._run_path(
                self.run["run_id"],
                f".history/retrieval_{self.stage}.{self.previous_state['upstream_digest']}.json",
            )
            self.store._atomic_write(history, canonical(self.previous_state) + "\n")
        self.store._atomic_write(self.path, canonical(self.state) + "\n")
