"""Ordinary-chat recall over trusted platform readers."""
import asyncio
import json
from typing import Literal
from pydantic import Field
from ...research.knowledge_search import KnowledgeSearch
from ...session.manager import SessionManager
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class KnowledgeSearchParameters(ParameterSchema):
    action: Literal['search', 'read'] = 'search'
    question: str = Field(default='', max_length=500)
    alternatives: list[str] = Field(default_factory=list, max_length=4)
    reference: str = Field(default='', max_length=2400)
    limit: int = Field(default=6, ge=1, le=10)
    project_offset: int = Field(default=0, ge=0)


class KnowledgeSearchTool(Tool):
    id = 'knowledge_search'
    parameters = KnowledgeSearchParameters
    description = """Unified automatic knowledge recall for EVERY ordinary or research chat.
Before answering factual questions, search Wiki, evidence and historical research results
outside conversation context. Pass the user's question plus 2–4 short semantic reformulations
in alternatives: short entity/term queries, synonyms, scientific terminology and English equivalents; preserve entities,
numbers and negation. Do not guess an answer to use as a search fact. This is read-only;
it does not require switching agents or delegating to a paper worker.
Then action=read with an exact returned reference checks current saved content, source state,
and linked evidence/PDF integrity. Use the returned [knowledge:...] citation after the supported
claim; copy the complete returned citation verbatim, including its closing ], without shortening the reference.
The frontend shows a View source button rather than an internal ID. Search snippets
are NOT checked evidence. Explain unavailable, changed, withdrawn, conflicting or insufficient
sources. Research judgments and Wiki summaries are not scientific proof.
Wiki is an editable, fallible knowledge projection, not an authority or a closed answer database.
Combine checked sources with your own general knowledge and reasoning for a complete answer,
and use other admitted tools when further verification matters. Distinguish sourced facts,
general explanation/inference, and unknowns; cite only claims the actual source supports.
No local hit is not a reason to refuse a general question you can answer. Do not narrate routine
retrieval or local misses in ordinary answers. Explain missing support only if the user asks
about saved research/source support, or it materially limits the answer; use natural wording
instead of internal workspace terminology. Follow explicit
source-only constraints when the user requests them. If next_project_offset
is present and retrieval is insufficient, continue paging with the same queries.
Respect explicit requests not to search or use tools. Never claim a search occurred without a receipt."""

    async def execute(self, args, context: ToolContext):
        try:
            parsed = KnowledgeSearchParameters(**args)
            extra = context.extra or {}
            identity = {k: str(extra.get(k) or '') for k in ('tenant_id', 'user_id', 'workspace_id')}
            if not all(identity.values()):
                raise ValueError('缺少当前用户或工作区')
            session = await SessionManager.get(context.session_id, **identity)
            if session is None:
                raise LookupError('会话不存在或不可访问')
            if context.abort and context.abort.is_set():
                raise ValueError('检索已取消')
            service = KnowledgeSearch(**identity, workdir=session.workdir)
            result = await asyncio.to_thread(service.read, parsed.reference) if parsed.action == 'read' else await asyncio.to_thread(
                service.search, parsed.question, parsed.alternatives, limit=parsed.limit, project_offset=parsed.project_offset)
            output = json.dumps(self._display(result, parsed.action), ensure_ascii=False)
            if len(output) > 30000:
                result['supporting_sources'] = result.get('supporting_sources', [])[:2]
                for source in result['supporting_sources']:
                    content = str(source.get('content') or '')
                    source['content'] = content[:4000]
                    source['content_truncated'] = len(content) > 4000
                result['content'] = str(result.get('content') or '')[:10000]
                result['content_truncated'] = True
                output = json.dumps(self._display(result, parsed.action), ensure_ascii=False)
                if len(output) > 30000:
                    raise ValueError('来源结果过大，请缩小查询范围')
            return ToolResult(title='检索已有知识' if parsed.action == 'search' else '核对知识来源', output=output,
                              metadata={'knowledge_retrieval': {'action': parsed.action, 'source_checked': result.get('source_checked', False)}})
        except (ValueError, LookupError) as exc:
            return ToolResult(title='已有知识检索', output='', error=str(exc))

    @staticmethod
    def _display(result, action):
        labels = {'active':'当前有效', 'archived':'已归档', 'retracted':'已撤回', 'withdrawn':'已撤回',
                  'superseded':'已被新记录替代', 'removed':'已移除', 'missing':'来源缺失',
                  'supported':'已有支持记录', 'manual_unverified':'人工记录，尚未验证',
                  'proposed':'待批准', 'draft':'草稿', 'published':'已发布', 'disputed':'存在争议',
                  'context_limited':'适用范围受限', 'verified':'已核对记录', 'unknown':'尚未确定',
                  'canonical':'当前知识页面', 'pending_review':'待审核', 'stale':'可能已过时',
                  'conflicting':'存在冲突', 'rebuilding':'正在重新整理', 'partially_supported':'部分内容已有支持'}
        def source(data):
            return {'名称': data.get('title') or '关联依据', '类型': data.get('kind') or '来源记录',
                    '状态': labels.get(data.get('status'), '状态需核对'), '正文': data.get('content') or '',
                    '定位': data.get('locator') or '', '引用': data.get('citation') or '',
                    '当前可用': data.get('usable', False), '原始文件可用': data.get('original_available'),
                    '原始文件内容已校验': data.get('original_verified', False),
                    '引用用途': '仅限正文实际支持的论断' if data.get('usable') else '仅作历史或状态说明，不能作为当前依据',
                    '内容是否截取': data.get('content_truncated', False)}
        if action == 'search':
            return {'检索表达': result['queries'], '范围': result['scope'], '候选资料': [
                {'名称': h['title'], '类型': h['kind'], '摘要': h['snippet'],
                 '状态': labels.get(h.get('status'), '状态需核对'), '来源读取参数': h['reference'],
                 '核对情况': '仅搜索命中，请用 read 读取当前来源'} for h in result['items']],
                '下一页历史课题起点': result['next_project_offset'], '提醒': result['notice'], '限制': result['warnings']}
        return {'核对情况': '已读取当前保存来源', '来源是否变化': '已变化，请重新核对' if result['changed'] else '未变化',
                '当前可用': result['usable'], **source(result),
                '关联依据': [source(s) for s in result.get('supporting_sources', [])],
                '关联依据总数': result.get('supporting_source_count', 0),
                '关联依据是否检查完整': not result.get('supporting_sources_truncated', False),
                '知识论断状态': [{'内容': c.get('statement'), '状态': labels.get(c.get('status'), '状态需核对'),
                    '支持情况': labels.get(c.get('support_status'), '支持情况需核对')} for c in result.get('claim_states', [])],
                '提醒': result['notice'] + ' 已有资料只是线索，请综合知识与分析回答；Wiki 不应被视为最终事实。'}

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry
        ToolRegistry.register(cls())
