import {For,Show,createMemo} from 'solid-js';
import './ResearchRecordDetails.css';
import {researchDetailRows} from '../utils/userFacingLabels';
export default function ResearchRecordDetails(props:{value:unknown}) {
  const details=createMemo(()=>researchDetailRows(props.value));
  return <div class="research-record-details" tabIndex={0} aria-label="研究记录内容"><dl><For each={details().rows}>{row=><div><dt>{row.label}</dt><dd>{row.value}</dd></div>}</For></dl><Show when={!details().rows.length}><p>没有已保存的说明内容。</p></Show><Show when={details().omitted}><p>还有更多记录；请缩小查看范围，或打开相应资料。</p></Show></div>;
}
