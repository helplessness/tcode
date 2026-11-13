"""只读生成两篇论文的待审清单；不改变草稿、不确认、不发布。"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'core'))
from enzyme_ontology_core.bundle import digest
from enzyme_ontology_core.extraction import LocalKnowledge, normalize_candidates


def recommendation(question, label, page):
    code = question['code']
    if code == 'invalid_relation_quarantined':
        if question['raw_assertion']['predicate'] == 'derived_from':
            return '不把化学合成步骤改成蛋白谱系。维持隔离，不产生发布操作；若需要表达化学转化，应另建 Reaction 与参与物证据。'
        if question['object_type'] == 'MechanisticIntermediate':
            return '候选中间体不应作为普通反应参与物。结合原文提出 has_intermediate，并将计算/文献转述分开；重新校验前维持隔离。'
        return '维持隔离。核对主语是否为 Reaction；不得只为通过校验把化合物强行改成 Reaction。'
    if code == 'variant_attribution_review':
        return '不确认当前普通蛋白的催化主张。保留定向进化叙述；仅在正文明确变体及活性时新建变体断言，不能将图注解释为野生型活性。'
    if code == 'variant_identity_review':
        if 'D-G3P' in label:
            return 'D-G3P 是底物名称，不是突变编号；此条为名称规则误报。仍保留泛称身份待解析，不据此改成 EnzymeVariant。'
        return '将论文中可明确追溯的工程化构建体建议为 EnzymeVariant；AAS/ProThDP2-M4 与 FLS-M3 分别处理，不与野生型合并。'
    if code == 'identity_ambiguity':
        return '不选择现有建议 ID。CEAS 家族、EC 酶类别、ProThDP 构建体和具体 UniProt 蛋白不能仅凭名称互相映射；补齐原始构建体来源后再映射。参考文献题名只作为引文索引。'
    if code == 'scientific_evidence_review':
        return '逐条核对原文：实验观察、计算机制、文献转述分别建断言；补齐可定位引用号，或保留 unspecified。引用窗口包含计算措辞不意味着整段都是计算结论；不得一键修改整段证据类别。'
    if any(term in label.lower() for term in ('chemical catalyst', 'sharpless', 'jamison', 'smith et', 'shi ae')):
        return '排除该 Protein 候选及其蛋白专属关系。作者、化学方法和未鉴定化学催化剂不是已确认蛋白；保留原文作为化学背景，不臆造结构或蛋白 ID。'
    if any(term in label for term in ('AAS', 'ProThDP2-M4', 'FLS-M3')):
        return '用第 5–7 页显式定义确认论文内的变体及别名；不增加未提供的序列/UniProt ID，不向野生型传播活性。'
    if label in ('HK', 'G6PDH'):
        return '仅保留为活性测定的辅助酶；不因它出现在实验配方中就加入 MAAP 主通路。具体构建体来源未确认。'
    if any(term in label.lower() for term in ('candidate proteins', 'his-tagged', 'wild-type', 'thdp', 'epoxidase', 'enzyme for')):
        return '保留为上下文描述或未解析身份；暂不发布为独立已确认蛋白。仅在明确上下文中绑定具体构建体，不能把全文同类泛称全部合并。'
    return '保留论文内名称和通路/测定角色；当前缺少精确构建体或序列来源，不映射到唯一蛋白 ID，也不向整个酶类推广活性。'


def build(run_root, output):
    run_root, output = Path(run_root).resolve(), Path(output).resolve()
    if output == run_root or run_root in output.parents:
        raise ValueError('review output must be outside the source run')
    initial = json.loads((run_root / 'report.json').read_text())
    knowledge = LocalKnowledge(PLUGIN / 'knowledge')
    packet = {'schema_version': 'paper-review-1', 'status': 'proposed', 'approved': False,
              'source_run': str(run_root), 'public_bundle_digest': knowledge.manifest['bundle_digest'],
              'papers': [], 'issues': [], 'evidence': {}}
    before = {}
    try:
        for paper in (1, 2):
            dbpath = run_root / f'draft-{paper}' / 'drafts.sqlite'
            before[str(dbpath)] = hashlib.sha256(dbpath.read_bytes()).hexdigest()
            db = sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True)
            scope, workspace = db.execute('SELECT scope,workspace_id FROM scope').fetchone()
            source = initial['papers'][paper - 1]
            original = Path(source['original'])
            assert hashlib.sha256(original.read_bytes()).hexdigest() == source['original_sha256']
            packet['papers'].append({'paper': paper, 'original': str(original), 'sha256': source['original_sha256']})
            grouped = {}
            for item_id, ordinal, payload, candidates in db.execute('SELECT item_id,ordinal,payload,candidates FROM span ORDER BY item_id,ordinal'):
                if candidates is None:
                    raise ValueError('incomplete source run')
                span, saved = json.loads(payload), json.loads(candidates)
                assert hashlib.sha256(span['text'].encode()).hexdigest() == span['text_sha256']
                ref = f'P{paper}-S{ordinal:03d}'
                packet['evidence'][ref] = {'paper': paper, 'item_id': item_id, 'ordinal': ordinal, **span}
                raw = {'entities': [{k: e[k] for k in ('key', 'label', 'type', 'quote', 'external_ids', 'identity_scope') if k in e} for e in saved['entities']],
                       'assertions': [{k: a[k] for k in ('subject_key', 'object_key', 'predicate', 'polarity', 'kind', 'qualifiers', 'quote')} for a in saved['assertions']]}
                checked = normalize_candidates(raw, span, knowledge, scope + ':' + workspace + '|' + span['source_id'])
                labels = {e['key']: e['label'] for e in saved['entities']}
                for q in {digest(q): q for q in [*saved['questions'], *checked['questions']]}.values():
                    key = digest(q)
                    entity = next((e for e in saved['entities'] if e['entity_id'] == q.get('entity_id')), None)
                    assertion = q.get('raw_assertion') or (saved['assertions'][q['candidate']] if q.get('candidate', 999999) < len(saved['assertions']) else None)
                    label = entity['label'] if entity else str((labels.get(assertion['subject_key']), assertion['predicate'], labels.get(assertion['object_key']))) if assertion else q['code']
                    quote = (entity or assertion or {}).get('quote', span['text'])
                    assert quote in span['text']
                    anchor = {'evidence_id': ref, 'label': label, 'quote': quote,
                              'entity': entity, 'assertion': assertion,
                              'suggestion': recommendation(q, label, span['locator']['page'])}
                    issue = grouped.setdefault(key, {'issue_id': f'P{paper}-{key[:12]}', 'paper': paper,
                        'status': 'pending', 'question': q, 'anchors': []})
                    issue['anchors'].append(anchor)
            packet['issues'].extend(grouped.values())
            db.close()
        packet['source_databases_unchanged'] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in before.items())
        assert packet['source_databases_unchanged']
        packet['packet_digest'] = digest(packet)
        output.mkdir(parents=True, exist_ok=True)
        (output / 'review_packet.json').write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n')
        lines = ['# 两篇论文逐项待审清单', '', '全部为建议，未确认、未发布。相同问题在不同片段中的出现单独列出，不能把一个确认动作当成全篇科学批准。', '',
                 f'共 {len(packet["issues"])} 个问题组，{sum(len(i["anchors"]) for i in packet["issues"])} 个片段内问题出现；完整原文和哈希见 review_packet.json。', '']
        for issue in packet['issues']:
            lines += [f'## {issue["issue_id"]} · {issue["question"]["code"]}', '', '状态：待确认。', '']
            for a in issue['anchors']:
                loc = packet['evidence'][a['evidence_id']]['locator']
                lines += [f'论文 {issue["paper"]}，第 {loc["page"]} 页，片段 {a["evidence_id"]}；对象：{a["label"]}', '',
                          *['> ' + line for line in a['quote'].splitlines()], '', '建议：' + a['suggestion'], '']
        (output / '逐项待审清单.md').write_text('\n'.join(lines))
        return {'issues': len(packet['issues']), 'occurrences': sum(len(i['anchors']) for i in packet['issues']), 'evidence_spans': len(packet['evidence']), 'packet_digest': packet['packet_digest']}
    finally:
        knowledge.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.run_root, args.output), ensure_ascii=False))
