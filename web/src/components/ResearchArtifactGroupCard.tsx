import { researchValueText, userFacingTitle } from '../utils/userFacingLabels';
import { For, Show } from 'solid-js';
import MarkdownContent from './MarkdownContent';
import {readableResearchMarkdown} from '../utils/researchMarkdown';
import type { ResearchArtifactGroup } from '../api/research';

type Props = {
  group: ResearchArtifactGroup;
  compact?: boolean;
};

const metricLabels: Array<[keyof ResearchArtifactGroup['counts'], string]> = [
  ['l1_passages', '原文段落'],
  ['l2_facts', '提取事实'],
  ['evidence_records', '证据记录'],
  ['pages', '页'],
  ['tables', '表格'],
  ['figure_images', '论文图片'],
  ['table_images', '表格图片'],
];

export default function ResearchArtifactGroupCard(props: Props) {
  const metrics = () => metricLabels
    .map(([key, label]) => ({ key, label, value: Number(props.group.counts?.[key] || 0) }))
    .filter((item) => item.value > 0);

  return (
    <article class="rounded-xl border border-[#38bdf8]/25 bg-[#0e171d] p-3.5">
      <div class="flex flex-wrap items-start gap-3">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2 text-[10px]">
            <span class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-2 py-0.5 font-semibold text-[#8ed5ff]">论文研究产物</span>
            <span class={`rounded-full border px-2 py-0.5 ${props.group.status === 'failed' ? 'border-[#ef4444]/30 text-[#fca5a5]' : 'border-[#38b46a]/30 text-[#9ee6bb]'}`}>
              {props.group.status === 'failed' ? '生成失败' : '已生成'}
            </span>
            <Show when={props.group.version_count > 1}>
              <span class="rounded-full border border-[#c4a7ff]/25 px-2 py-0.5 text-[#d8c8ff]">{props.group.version_count} 个版本</span>
            </Show>
          </div>
          <h4 class="mt-2 break-words text-sm font-semibold leading-5 text-[#e6edf3]">{userFacingTitle(props.group.title,'已入库论文')}</h4>
          <Show when={props.group.summary}>
            <div class="research-progress-markdown mt-2"><MarkdownContent content={readableResearchMarkdown(props.group.summary)} allowSessionActions={false}/></div>
          </Show>
        </div>
      </div>

      <div class="mt-3 flex flex-wrap gap-1.5 text-[10px] text-[#9faeb7]">
        <For each={metrics()}>
          {(item) => <span class="rounded border border-[#30383e] bg-[#171d21] px-2 py-1">{item.value} {item.label}</span>}
        </For>

        <Show when={props.group.pmid}><span class="rounded border border-[#30383e] bg-[#171d21] px-2 py-1">文献数据库编号：{props.group.pmid}</span></Show>
        <Show when={props.group.doi}><span class="rounded border border-[#30383e] bg-[#171d21] px-2 py-1">论文永久标识：{props.group.doi}</span></Show>
      </div>

      <Show when={props.group.primary_artifacts.length > 0}>
        <div class="mt-3 flex flex-wrap gap-1.5">
          <For each={props.group.primary_artifacts}>
            {(artifact) => <span class="rounded bg-[#17242b] px-2 py-1 text-[10px] text-[#8ed5ff]">{artifact.label}</span>}
          </For>
        </div>
      </Show>

      <Show when={!props.compact}>
        <details class="mt-3 border-t border-[#263038] pt-2 text-[10px] text-[#657680]">
          <summary class="cursor-pointer select-none">查看产物版本与文件</summary>
          <div class="mt-2 flex flex-wrap gap-3">
            <span>研究产物 {props.group.artifact_count} 项</span>
            <span>审计文件 {props.group.audit_artifact_count} 项</span>
            <Show when={props.group.latest_version}><span class="font-mono">最新版本：{researchValueText(props.group.latest_version,'revision')}</span></Show>
          </div>
          <div class="research-scroll-content mt-2 space-y-1">
            <For each={props.group.primary_artifacts}>
              {(artifact) => (
                <div class="grid gap-1 rounded bg-[#0b1115] px-2 py-1.5 sm:grid-cols-[100px_minmax(0,1fr)]">
                  <span class="text-[#8b99a4]">{artifact.label}</span>
                  <span class="truncate font-mono text-[#596873]" title={artifact.label}>{researchValueText(artifact.ref,'source_path')}</span>
                </div>
              )}
            </For>
          </div>
        </details>
      </Show>
    </article>
  );
}
