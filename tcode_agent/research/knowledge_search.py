"""Unified, owner-scoped recall; search snippets are not verified source reads."""
from __future__ import annotations

import base64
import json

from ..evidence.store import EvidenceStore
from ..runtime.platform_knowledge.contracts import KnowledgeOwner, PlatformKnowledgeValidationError
from ..runtime.platform_knowledge.wiki_service import WikiService
from .hypothesis_map import HypothesisMap
from .models import canonical_json, sha256_text
from .read_service import ResearchReadService
from .topic_store import ResearchTopicStore


class KnowledgeSearch:
    _inactive = {'archived', 'retracted', 'withdrawn', 'superseded', 'removed', 'missing', 'stale', 'disabled', 'invalid', 'deleted'}

    def __init__(self, **owner):
        self.owner = {k: str(owner.get(k) or '') for k in ('tenant_id', 'user_id', 'workspace_id', 'workdir')}
        identity = KnowledgeOwner(**self.owner)
        self.wiki = WikiService(identity)
        self.reader = ResearchReadService(**self.owner)
        self.map = HypothesisMap(**self.owner)
        self.scope = sha256_text(canonical_json(self.owner))

    def _reference(self, kind, identifier, project, digest):
        data = [self.scope, kind, identifier, project, digest]
        return base64.urlsafe_b64encode(canonical_json(data).encode()).decode().rstrip('=')

    def _source(self, kind, identifier, project='', *, cache=None):
        cache = {} if cache is None else cache
        key = (kind, identifier, project)
        if key in cache:
            return cache[key]
        if kind == 'wiki':
            try:
                page = self.wiki.read(identifier)
            except Exception as exc:
                if isinstance(exc, PlatformKnowledgeValidationError) or type(exc).__name__ == 'WikiNotFoundError':
                    raise LookupError('知识来源不存在或不可访问') from None
                raise
            claims = page.get('claims') or []
            data = {'title': page.get('title'), 'content': page.get('content') or page.get('summary') or '',
                    'status': page.get('status'), 'source_revision': page.get('revision'), 'evidence_ids': list(dict.fromkeys(
                        str(e) for c in claims for e in c.get('evidence_ids', []))), 'kind': '知识页面',
                    'claim_states': [{k: c.get(k) for k in ('statement', 'status', 'support_status')} for c in claims[:30]]}
        elif kind == 'evidence':
            data = self.map.source('evidence', identifier)
            if data['status'] == 'missing' and not data.get('content'):
                raise LookupError('来源缺失或原文校验未通过')
            data = {**data, 'kind': data.get('source_label') or '来源证据', 'evidence_ids': []}
        elif kind in {'finding', 'episode', 'experience'}:
            item = self.reader.get_object(project, kind, identifier)['item']
            def readable(value):
                if isinstance(value, dict):
                    return '\n'.join(readable(v) for k, v in value.items() if not k.endswith('_id') and not k.endswith('_ids'))
                if isinstance(value, list):
                    return '\n'.join('- ' + readable(v) for v in value)
                return str(value or '')
            content = '\n\n'.join('## ' + label + '\n' + readable(item[key]) for key, label in (
                ('statement', '记录的结论'), ('summary', '研究记录内容'), ('scope', '适用范围'),
                ('decisions', '已记录的决定'), ('failed_paths', '尝试过的路线'), ('findings', '阶段发现'))
                if item.get(key))
            data = {'title': item.get('title') or item.get('statement') or item.get('summary') or '研究记录',
                    'content': content, 'provenance': item.get('provenance'),
                    'status': item.get('status') or item.get('state') or 'active',
                    'evidence_ids': item.get('evidence_ids') or [], 'source_revision': item.get('revision'),
                    'kind': '研究结论' if kind == 'finding' else '研究经历'}
        else:
            raise ValueError('未知来源类型')
        linked = list(dict.fromkeys(str(value) for value in data.get('evidence_ids', [])))
        supporting = []
        for evidence_id in linked[:8]:
            try:
                source = self._source('evidence', evidence_id, cache=cache)
            except LookupError:
                source = {'title': '关联依据不可用', 'status': 'missing', 'content_hash': '', 'content': ''}
            supporting.append({**source, 'usable': source.get('status') not in self._inactive,
                               'evidence_id': evidence_id})
        # Bind citations to linked-source state as well as the saved page body.
        digest = sha256_text(canonical_json({**data, 'supporting': [
            [s['evidence_id'], s['content_hash'], s['status']] for s in supporting]}))
        result = {**data, 'content': str(data['content'])[:20000], 'title': str(data.get('title') or '来源记录')[:300],
                  'content_hash': digest, 'content_truncated': data.get('content_truncated', False) or len(str(data['content'])) > 20000,
                  'supporting_sources': supporting, 'supporting_source_count': len(linked),
                  'supporting_sources_truncated': len(linked) > 8}
        cache[key] = result
        return result

    def read(self, reference):
        if not isinstance(reference, str) or len(reference) > 2400:
            raise ValueError('无效来源引用')
        try:
            scope, kind, identifier, project, expected = json.loads(base64.urlsafe_b64decode(reference + '=' * (-len(reference) % 4)))
            if scope != self.scope or not all(isinstance(v, str) for v in (kind, identifier, project, expected)):
                raise ValueError()
        except Exception:
            raise ValueError('来源引用与当前工作区不匹配') from None
        data = self._source(kind, identifier, project)
        changed = data['content_hash'] != expected
        supporting = [{**s, 'citation': '[knowledge:' + self._reference('evidence', s['evidence_id'], '', s['content_hash']) + ']'
                       if s['content_hash'] else ''} for s in data['supporting_sources']]
        support_complete = not data['supporting_sources_truncated'] and all(s['usable'] for s in supporting)
        usable = data.get('status') not in self._inactive and support_complete
        state = ('withdrawn' if data.get('status') in {'retracted', 'withdrawn', 'removed', 'deleted'}
                 else 'missing' if data.get('status') == 'missing'
                 else 'historical' if not usable or changed else 'available')
        notice = '已读取当前保存来源；不等于科研验证。'
        if changed:
            notice += '引用对应的内容、版本或关联依据已变化，请核对当前内容后重新回答。'
        if not support_complete:
            notice += '关联依据存在撤回、失效、缺失或尚未检查项，不能把整页内容当作已获当前依据支持。'
        if not usable:
            notice += '本记录仅可用于说明历史或当前状态，不能支持新的事实论断。'
        notice += data.get('source_notice', '')
        return {'source_checked': True, 'changed': changed, 'usable': usable, 'state': state,
                'citation': '[knowledge:' + self._reference(kind, identifier, project, data['content_hash']) + ']',
                **data, 'supporting_sources': supporting,
                'notice': notice}

    def search(self, question, alternatives=(), *, limit=8, project_offset=0):
        queries = list(dict.fromkeys(str(q).strip()[:500] for q in [question, *alternatives] if str(q).strip()))[:5]
        if not queries:
            raise ValueError('请输入检索问题')
        hits, warnings, source_cache = {}, [], {}
        modes = set()

        def add(kind, identifier, project, title, snippet, rank):
            key = (kind, identifier, project)
            weight = {'evidence': 1.5, 'wiki': 1.3, 'finding': 1.2, 'experience': 0.9, 'episode': 0.7}[kind]
            if not identifier:
                return
            if key in hits:
                hits[key]['score'] += weight / (60 + rank)
                return
            try:
                data = self._source(kind, identifier, project, cache=source_cache)
            except (ValueError, LookupError, PlatformKnowledgeValidationError):
                return
            if data.get('status') in self._inactive:
                return
            hits[key] = {'title': data['title'], 'kind': data['kind'],
                         'snippet': data['content'][:800], 'status': data.get('status'),
                         'reference': self._reference(kind, identifier, project, data['content_hash']),
                         'source_checked': False, 'score': weight / (60 + rank)}

        try:
            projects = ResearchTopicStore.list_projects(**self.owner, include_archived=True, limit=51, offset=project_offset)
        except Exception:
            projects = []
            warnings.append('研究历史暂不可用，不能据此判断没有相关资料。')
        for query in queries:
            try:
                wiki_results = [self.wiki.search(query, limit=limit)]
                # Chinese FTS may keep a complete phrase as one token. Existing
                # LIKE fallback needs each rewritten term, not the whole term list.
                if not wiki_results[0].get('hits') and len(query.split()) > 1:
                    wiki_results.extend(self.wiki.search(term, limit=limit) for term in query.split()[:3] if len(term) >= 2)
                for wiki in wiki_results:
                    modes.add(str(wiki.get('search_mode') or '词项检索'))
                    for rank, item in enumerate(wiki.get('hits', []), 1):
                        add('wiki', item.get('page_id'), '', item.get('title'), item.get('summary'), rank)
            except PlatformKnowledgeValidationError:
                warnings.append('当前工作区暂无可用 Wiki。')
            except Exception:
                warnings.append('Wiki 检索暂不可用，不能据此判断没有相关资料。')
            try:
                for rank, item in enumerate(EvidenceStore.search(query=query, workdir=self.owner['workdir'], limit=limit), 1):
                    add('evidence', item.evidence_id, '', item.title, item.content, rank)
            except Exception:
                warnings.append('证据检索暂不可用，不能据此判断没有相关资料。')
            for project in projects[:50]:
                for kind, method, id_key in (('finding', self.reader.list_findings, 'finding_id'),
                        ('episode', self.reader.list_episodes, 'episode_id'),
                        ('experience', self.reader.list_experience, 'experience_id')):
                    try:
                        result = method(project.research_id, query=query, limit=limit)
                    except Exception:
                        warnings.append('部分研究记录暂不可用，检索覆盖不完整。')
                        continue
                    for rank, item in enumerate(result['items'], 1):
                        add(kind, item.get(id_key), project.research_id,
                            item.get('title') or item.get('statement') or project.title,
                            item.get('statement') or item.get('summary') or item.get('content'), rank)
        ranked = sorted(hits.values(), key=lambda h: -h['score'])[:limit]
        return {'schema': 'tcode.knowledge-search.v1', 'items': ranked, 'queries': queries,
                'scope': '当前用户、当前工作区 Wiki、证据和历史课题（含归档课题）',
                'projects_scanned': min(len(projects), 50),
                'next_project_offset': project_offset + 50 if len(projects) > 50 else None,
                'retrieval': {'wiki_modes': sorted(modes), 'query_expansion': len(queries) > 1, 'degraded': bool(warnings)},
                'warnings': list(dict.fromkeys(warnings)),
                'notice': '搜索摘要不能代替核对。先用 read 读取少量命中来源及关联依据，再引用回答；未命中不证明资料不存在。'}
