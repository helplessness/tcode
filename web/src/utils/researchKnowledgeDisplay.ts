import type { ResearchKnowledgeIssue, ResearchKnowledgeLink } from '../api/research';
import { readableSourceLabel } from './evidenceDisplay';

export type ResearchKnowledgeIssueGroup = {
  code: string;
  severity: string;
  title: string;
  description: string;
  action: string;
  count: number;
  sources: string[];
};

const issueCopy: Record<string, { title: string; description: string; action: string }> = {
  knowledge_object_missing: {
    title: '来源对象不存在',
    description: '系统在当前工作区 中找不到已关联的论文、证据或 Wiki 内容。',
    action: '先点击“自动修复来源”。如果仍然存在，请恢复对应资料，或撤回依赖该资料的结论。',
  },
  knowledge_link_stale: {
    title: '旧来源关系待整理',
    description: '这些是以前生成、但已不再由当前研究内容支持的关联。',
    action: '点击“自动修复来源”，系统会将它们移出当前快照，并保留历史审计。',
  },
  knowledge_link_conflicting: {
    title: '来源内容存在冲突',
    description: '当前结论关联到了相互矛盾的内容。',
    action: '请在“待解决的结论冲突”中核对两边证据后再作决定。',
  },
  paper_record_changed: {
    title: '论文解析结果已更新',
    description: '当前关联保留的论文版本与文献库中的最新版本不同。',
    action: '点击“自动修复来源”更新到最新版本。',
  },
  wiki_page_revision_changed: {
    title: 'Wiki 页面已更新',
    description: '当前关联使用的是 Wiki 旧版本。',
    action: '点击“自动修复来源”重新关联最新版本。',
  },
  wiki_claim_revision_changed: {
    title: 'Wiki 结论已更新',
    description: '已关联的 Wiki 结论所在页面发生了修订。',
    action: '点击“自动修复来源”重新核对最新版本。',
  },
  wiki_claim_not_export_ready: {
    title: 'Wiki 结论尚未完成核验',
    description: '这条 Wiki 结论尚未确认、已过期或存在冲突。',
    action: '请在 Wiki 中核对该结论的支持证据和当前状态。',
  },
  finding_without_evidence: {
    title: '研究结论没有来源',
    description: '有一条正在使用的研究结论没有任何可追溯证据。',
    action: '请撤回该结论，或从带有论文、证据 或 Wiki 来源的研究进展中重新记录。',
  },
  finding_only_session_evidence: {
    title: '结论仅能回到私人对话',
    description: '这条结论有会话记录，但尚未连接可导出的论文、证据 或 Wiki 来源。',
    action: '它不阻断打包；如需让接收者独立核验，请再补充一条持久证据。',
  },
  conflict_side_without_evidence: {
    title: '冲突的一方缺少证据',
    description: '当前有未解决冲突，但其中一方无法追溯到证据。',
    action: '请在“待解决的结论冲突”中补充双方证据或完成冲突裁决。',
  },
  paper_evidence_without_anchor: {
    title: '论文证据缺少原文位置',
    description: '证据已记录，但没有页码、段落、表格或其他原文定位。',
    action: '请重新解析论文，或为该证据补充可核对的原文位置。',
  },
  paper_passage_missing: {
    title: '论文原文片段已不存在',
    description: '结论引用的 L1 Passage 在当前论文解析版本中找不到。',
    action: '请重新解析论文，然后点击“自动修复来源”。',
  },
  passage_anchor_broken: {
    title: '论文片段的原文定位已断开',
    description: 'L1 Passage 仍存在，但它引用的原始 Chunk 已不存在。',
    action: '请重新解析论文，再重新整理来源。',
  },
  knowledge_evidence_retracted: {
    title: '引用的证据已撤回',
    description: '当前结论仍在引用一条已经撤回的证据。',
    action: '请补充新证据，或撤回、修正依赖它的研究结论。',
  },
  pack_source_collection_incomplete: {
    title: '研究历史未完整收集',
    description: '当前研究数量超过单次导出上限，系统为避免生成残缺包而阻止导出。',
    action: '请调整 ResearchPack 导出上限配置，或缩小要导出的研究范围。',
  },
  scope_has_no_shareable_experience: {
    title: '当前范围还没有可分享的研究经历',
    description: '系统找到了课题，但尚未找到已完成、带产物或带结论的研究动作。',
    action: '请先完成一次研究动作并保存研究进度，然后重新生成导出预览。',
  },
  dependency_evidence_missing: {
    title: '研究经历引用的证据不存在',
    description: '准备打包的研究经历仍引用已经不存在的 证据。',
    action: '请恢复该 证据，或撤回、修正依赖它的研究结论后重新预览。',
  },
  dependency_event_missing: {
    title: '研究经历的原始记录不存在',
    description: '准备打包的研究经历无法找到其依赖的原始研究事件。',
    action: '请先点击“自动修复来源”；如果仍存在，请检查相关 Session 是否被清理。',
  },
  dependency_activity_missing: {
    title: '研究经历的过程记录不存在',
    description: '准备打包的经历引用了已经不存在的研究活动。',
    action: '请保存一次研究进度以重新整理过程记录，然后重新生成导出预览。',
  },
  dependency_episode_missing: {
    title: '研究经历的阶段记录不存在',
    description: '准备打包的经历引用了已经不存在的阶段总结。',
    action: '请保存一次研究进度以重新生成阶段总结，然后重新预览。',
  },
};

const objectTypeLabels: Record<string, string> = {
  paper: '论文',
  passage: '论文原文片段',
  evidence: '证据',
  wiki_page: 'Wiki 页面',
  wiki_claim: 'Wiki 结论',
  finding: '研究结论',
};

export const researchSourceTypeLabel = (type: unknown) => objectTypeLabels[String(type || '')] || '来源资料';
export const researchSourceLabel = (link: ResearchKnowledgeLink) => readableSourceLabel(
  link.metadata?.title || link.metadata?.source_title || link.metadata?.page_title || link.metadata?.statement || link.metadata?.summary_text,
  `${researchSourceTypeLabel(link.object_type)}（名称暂不可用）`,
);

export const researchRelationLabel = (relation: unknown) => ({ supports: '支持', contradicts: '反驳', context_only: '背景来源', derived_from: '派生来源', cites: '引用', relates_to: '相关' }[String(relation)] || '相关');

const compact = (value: unknown, limit = 120) => {
  const valueText = String(value || '').replace(/\s+/g, ' ').trim();
  if (!valueText) return '';
  return valueText.length > limit ? `${valueText.slice(0, limit)}…` : valueText;
};

const sourceLabel = (
  issue: ResearchKnowledgeIssue,
  links: Map<string, ResearchKnowledgeLink>,
) => {
  const link = links.get(String(issue.link_id || ''));
  const metadata = link?.metadata || {};
  const readable = compact(
    metadata.title || metadata.page_title || metadata.statement || metadata.summary_text,
  );
  if (readable) return readableSourceLabel(readable, objectTypeLabels[String(issue.object_type || link?.object_type || '')] || '来源资料');
  const objectType = String(issue.object_type || link?.object_type || '');
  const objectId = compact(
    issue.object_id || issue.evidence_id || issue.doc_id || issue.passage_id
      || issue.page_id || issue.claim_id || issue.finding_id || issue.conflict_id
      || link?.object_id,
    100,
  );
  if (!objectId) return '';
  return `${objectTypeLabels[objectType] || '来源'}（名称暂不可用）`;
};

export const groupResearchKnowledgeIssues = (
  issues: ResearchKnowledgeIssue[] = [],
  links: ResearchKnowledgeLink[] = [],
): ResearchKnowledgeIssueGroup[] => {
  const linkMap = new Map(links.map((link) => [link.link_id, link]));
  const grouped = new Map<string, ResearchKnowledgeIssueGroup>();
  for (const issue of issues) {
    const code = String(issue.code || 'knowledge_issue');
    const severity = String(issue.severity || 'warning');
    const key = `${severity}:${code}`;
    const fallback = {
      title: severity === 'error' ? '有一项来源问题会阻断打包' : '有一项来源建议需要注意',
      description: readableSourceLabel(compact(issue.message, 240), '系统发现了一项需要核对的来源关系。'),
      action: '请先点击“自动修复来源”；如果仍然存在，请根据问题说明补充或修正来源。',
    };
    const copy = issueCopy[code] || fallback;
    const current = grouped.get(key) || {
      code,
      severity,
      ...copy,
      count: 0,
      sources: [],
    };
    current.count += 1;
    const source = sourceLabel(issue, linkMap);
    if (source && !current.sources.includes(source) && current.sources.length < 10) {
      current.sources.push(source);
    }
    grouped.set(key, current);
  }
  const severityRank = (severity: string) => (severity === 'error' ? 0 : severity === 'warning' ? 1 : 2);
  return [...grouped.values()].sort(
    (left, right) => severityRank(left.severity) - severityRank(right.severity) || right.count - left.count,
  );
};
