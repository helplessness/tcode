import { For, Show, createEffect, createMemo, createSignal, onCleanup, onMount } from 'solid-js';
import type { SubagentItem } from '../api/subagents';
import type { TeamPeerThread } from '../api/teams';
import { normalizeSubagentStatus } from '../utils/subagentTree';
import { clampOfficeZoom, idleActions, officeActorPalette, officeActorState, officeToolGlyph, reportLines, shouldReport } from '../utils/officeMotion';
import { preferenceKeys, readBooleanPreference } from '../utils/userPreferences';
import './CollaborationOffice.css';

const IDLE_STATE_INTERVAL_MS = 10_000;

type Props = {
  items: SubagentItem[]; sessionId: string; manager: string; active: boolean;
  peerThreads: TeamPeerThread[];
  statusOf: (item: SubagentItem) => string;
  toolsOf: (item: SubagentItem) => string[];
  activityOf: (item: SubagentItem) => string;
  contractOf: (item: SubagentItem) => SubagentItem['contract'];
  onDiscuss: (item: SubagentItem) => void;
};

function Person() {
  return <g class="office-person" aria-hidden="true" shape-rendering="crispEdges">
    <ellipse cx="0" cy="24" rx="17" ry="4" fill="#303c3624"/>
    <path d="M-9-24H9V-18H13V-5H-13V-18H-9Z" fill="var(--hair,#493f40)"/>
    <path d="M-9-15H9V-3H5V1H-5V-3H-9Z" fill="var(--skin,#efc297)"/>
    <path d="M-6-11H-3V-8H-6ZM3-11H6V-8H3Z" fill="#363c42"/>
    <path d="M-9 1H9V16H-9Z" fill="var(--shirt,#638c80)"/>
    <path class="office-arms" d="M-14 3H-9V13H-14ZM9 3H14V13H9Z" fill="var(--skin,#efc297)"/>
    <path d="M-9 16H-2V24H-11V20H-9ZM2 16H9V20H11V24H2Z" fill="var(--trousers,#465464)"/>
  </g>;
}

export default function CollaborationOffice(props: Props) {
  const [selected, setSelected] = createSignal('');
  const [visits, setVisits] = createSignal<Record<string, number>>({});
  const [reports, setReports] = createSignal<Record<string, number>>({});
  const [idle, setIdle] = createSignal<Record<string, number>>({});
  const [spawns, setSpawns] = createSignal<Record<string, number>>({});
  const [paused, setPaused] = createSignal(!readBooleanPreference(preferenceKeys.officeAnimations, true));
  const [pauseInactive, setPauseInactive] = createSignal(readBooleanPreference(preferenceKeys.officePauseInactive, true));
  const [rememberZoom, setRememberZoom] = createSignal(readBooleanPreference(preferenceKeys.officeRememberZoom, true));
  const officeZoomKey = `tcode.settings.officeZoom:${props.sessionId}`;
  const [zoom, setZoom] = createSignal(rememberZoom() ? clampOfficeZoom(Number(localStorage.getItem(officeZoomKey) || 1)) : 1);
  const [viewportSize, setViewportSize] = createSignal({width:600,height:360});
  let viewport!: HTMLDivElement;
  let pan: {x:number;y:number;left:number;top:number} | undefined;
  let previous = new Map<string, string>();
  let known = new Set<string>();
  let owner = '';
  const members = createMemo(() => [...props.items].sort((a, b) => a.created_at - b.created_at || a.session_id.localeCompare(b.session_id)));
  const resting = createMemo(() => members().filter(item => {
    const status = normalizeSubagentStatus(props.statusOf(item));
    return status !== 'running' && status !== 'failed' && (idle()[item.session_id] ?? 0) === 0;
  }).slice(0, 2));
  const isResting = (item: SubagentItem) => resting().some(restingItem => restingItem.session_id === item.session_id);
  const focused = () => members().find(item => item.session_id === selected());
  const running = () => members().filter(item => normalizeSubagentStatus(props.statusOf(item)) === 'running').length;
  const workerName = (item: SubagentItem) => String(item.worker_name || item.team_worker?.name || item.agent_name.replace(/^worker_/, '')).toLowerCase();
  const memberPoint = (worker: string) => {
    const index = members().findIndex(item => workerName(item) === String(worker || '').toLowerCase());
    return index < 0 ? null : {x:105+(index%2)*170,y:154+Math.floor(index/2)*138,index};
  };
  const peerExchange = createMemo(() => {
    const thread = [...props.peerThreads].sort((a,b) => b.updated_at-a.updated_at)
      .find(item => item.status === 'pending' || Date.now() - Number(item.updated_at || 0) * 1000 < 15000);
    if (!thread) return null;
    const from = memberPoint(thread.sender_worker); const to = memberPoint(thread.target_worker);
    return from && to ? {thread,from,to} : null;
  });
  const height = () => Math.max(646, 115 + Math.max(1, Math.ceil(members().length / 2)) * 138);
  const fit = () => Math.min(viewportSize().width / 600, viewportSize().height / height());
  const scale = () => fit() * zoom();
  const changeZoom = (value: number) => {
    const ratio = clampOfficeZoom(value) / zoom();
    const left = (viewport.scrollLeft + viewport.clientWidth / 2) * ratio - viewport.clientWidth / 2;
    const top = (viewport.scrollTop + viewport.clientHeight / 2) * ratio - viewport.clientHeight / 2;
    const next = clampOfficeZoom(value);
    setZoom(next);
    if (rememberZoom()) localStorage.setItem(officeZoomKey, String(next));
    requestAnimationFrame(() => viewport.scrollTo({left,top}));
  };
  const resetView = () => {setZoom(1);if(rememberZoom())localStorage.setItem(officeZoomKey,'1');viewport?.scrollTo({left:0,top:0});};
  onMount(() => {
    const observer = new ResizeObserver(() => {
      if (viewport.clientWidth && viewport.clientHeight) setViewportSize({width:viewport.clientWidth-24,height:Math.max(1,viewport.clientHeight-24)});
    });
    observer.observe(viewport);
    const wheel = (event: WheelEvent) => {if(event.ctrlKey || event.metaKey){event.preventDefault();changeZoom(zoom()*Math.exp(-event.deltaY*.005));}};
    viewport.addEventListener('wheel',wheel,{passive:false});
    const settingsChanged = () => {
      setPaused(!readBooleanPreference(preferenceKeys.officeAnimations, true));
      setPauseInactive(readBooleanPreference(preferenceKeys.officePauseInactive, true));
      setRememberZoom(readBooleanPreference(preferenceKeys.officeRememberZoom, true));
    };
    window.addEventListener('tcode:settings-changed', settingsChanged);
    onCleanup(() => {observer.disconnect();viewport.removeEventListener('wheel',wheel);window.removeEventListener('tcode:settings-changed', settingsChanged);});
  });
  const name = (item: SubagentItem) => item.team_worker?.role || item.worker_role || item.display_name || item.title || item.agent_name;
  createEffect(() => {
    if (owner !== props.sessionId) {
      owner = props.sessionId; previous = new Map(); known = new Set(members().map(item => item.session_id)); setVisits({}); setReports({}); setIdle({}); setSpawns({}); setSelected(''); resetView();
    }
    const next = new Map<string, string>();
    const now = Date.now();
    for (const item of members()) {
      const status = props.statusOf(item);
      next.set(item.session_id, status);
      if (props.active && !known.has(item.session_id)) setSpawns(value => ({...value,[item.session_id]:now}));
      if (props.active && shouldReport(previous.get(item.session_id), status)) {
        setVisits(value => ({ ...value, [item.session_id]: now }));
        setReports(value => ({ ...value, [item.session_id]: Math.floor(Math.random() * reportLines.length) }));
      } else if (normalizeSubagentStatus(status) !== 'completed') {
        setVisits(value => value[item.session_id] ? Object.fromEntries(Object.entries(value).filter(([id]) => id !== item.session_id)) : value);
      }
    }
    known = new Set(members().map(item => item.session_id));
    previous = next;
  });
  createEffect(() => {
    if (!props.active) { setVisits({}); return; }
    const timer = window.setInterval(() => {
      setVisits(value => Object.fromEntries(Object.entries(value).filter(([, time]) => Date.now() - time < 12000)));
      setSpawns(value => Object.fromEntries(Object.entries(value).filter(([, time]) => Date.now() - time < 4500)));
      if (!paused()) setIdle(Object.fromEntries(members().map(item => [item.session_id, Math.floor(Math.random() * idleActions.length)])));
    }, IDLE_STATE_INTERVAL_MS);
    onCleanup(() => window.clearInterval(timer));
  });

  return <section class="office-view" aria-label="团队像素办公室" data-paused={paused() || (pauseInactive() && !props.active)}>
    <header class="office-heading"><div><h2>团队办公室</h2><p>{members().length} 位成员 · {running()} 位工作中</p></div>
      <button aria-pressed={paused()} onClick={() => setPaused(!paused())}>{paused() ? '播放动画' : '暂停动画'}</button>
    </header>
    <div class="office-camera" aria-label="办公室视图控制">
      <button aria-label="缩小办公室" disabled={zoom() <= .25} onClick={() => changeZoom(zoom()/1.25)}>−</button>
      <output aria-label="办公室缩放比例">{Math.round(zoom()*100)}%</output>
      <button aria-label="放大办公室" disabled={zoom() >= 4} onClick={() => changeZoom(zoom()*1.25)}>＋</button>
      <button onClick={resetView}>适应窗口</button><span>拖拽平移 · ⌘/Ctrl＋滚轮缩放</span>
    </div>
    <div ref={viewport} class="office-scroll" tabindex="0" aria-label="办公室画布，可滚动、拖拽和缩放"
      onPointerDown={event => {if(event.button!==0 || (event.target as Element).closest('[role="button"]'))return;pan={x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture(event.pointerId);}}
      onPointerMove={event => {if(pan)viewport.scrollTo({left:pan.left+pan.x-event.clientX,top:pan.top+pan.y-event.clientY});}}
      onPointerUp={() => {pan=undefined;}} onPointerCancel={() => {pan=undefined;}} onLostPointerCapture={() => {pan=undefined;}}
      onKeyDown={event => {if(event.target!==viewport)return;if(['+','=','-','0'].includes(event.key)){event.preventDefault();if(event.key==='0')resetView();else changeZoom(zoom()*(event.key==='-'?.8:1.25));}}}>
      <div class="office-scene-space" style={{width:`${Math.max(viewportSize().width,600*scale())}px`,height:`${Math.max(viewportSize().height,height()*scale())}px`}}>
      <svg class="office-scene" style={{width:`${600*scale()}px`,height:`${height()*scale()}px`}} viewBox={`0 0 600 ${height()}`} role="group" aria-label="上方为负责人办公室，下方为成员工位">
        <defs>
          <pattern id="office-wood" width="48" height="24" patternUnits="userSpaceOnUse"><rect width="48" height="24" fill="#96613f"/><path d="M0 0H48M0 24H48M24 0V24" stroke="#7f4f34" stroke-width="1"/><path d="M4 5h15m9 12h15" stroke="#aa7550" stroke-width="1" opacity=".55"/></pattern>
          <pattern id="office-checker" width="32" height="32" patternUnits="userSpaceOnUse"><rect width="32" height="32" fill="#d7d5ce"/><rect width="16" height="16" fill="#6c716f"/><rect x="16" y="16" width="16" height="16" fill="#6c716f"/></pattern>
          <filter id="office-shadow" x="-30%" y="-30%" width="160%" height="170%"><feDropShadow dx="0" dy="3" stdDeviation="2" flood-color="#15202b" flood-opacity=".24"/></filter>
        </defs>
        <rect x="6" y="6" width="588" height={height()-12} rx="5" fill="#263546" stroke="#182430" stroke-width="5"/>
        <rect x="22" y="22" width="340" height={height()-44} fill="url(#office-wood)"/>
        <rect x="378" y="22" width="200" height={height()-180} fill="#3e6b91"/>
        <rect x="378" y={height()-158} width="200" height="136" fill="url(#office-checker)"/>
        <path d={`M370 22v${height()-44}M378 ${height()-174}h200`} stroke="#172534" stroke-width="9"/>
        <g class="office-manager-room">
          <rect x="382" y="30" width="188" height="246" rx="2" class="office-manager-floor"/>
          <path d="M382 196V30H570V276H382V244M382 196v48" class="office-manager-wall"/>
          <path d="M382 196v48" class="office-manager-threshold"/>
          <g transform="translate(382 196)"><g class="office-door-leaf" classList={{'is-open':Object.keys(visits()).length > 0}}><path d="M0 0V48"/><circle cx="-4" cy="25" r="2.5"/></g></g>
          <rect x="396" y="43" width="154" height="25" rx="2" class="office-zone-plaque"/><text x="407" y="60" class="office-room-label">负责人办公室</text>
          <g class="office-wall-shelf" filter="url(#office-shadow)"><rect x="398" y="82" width="74" height="33"/><path d="M403 87v22m13-22v22m17-22v22m17-22v22"/></g>
          <g class="office-clock" transform="translate(522 96)"><circle r="15"/><path d="M0-9v10l7 4"/></g>
          <g class="office-plant" transform="translate(548 151)"><rect x="-12" y="5" width="24" height="16" rx="2"/><path d="M0 6C-3-18-16-18-17-7M0 6C4-20 18-20 18-7M0 6C0-25 9-28 11-17"/></g>
          <g filter="url(#office-shadow)"><rect x="411" y="170" width="130" height="34" rx="2" fill="#b78a62"/><rect x="456" y="151" width="40" height="30" fill="#40565a" stroke="#eee3cd" stroke-width="3"/><path d="M419 204v12m114-12v12" stroke="#72523b" stroke-width="5"/></g>
          <g transform="translate(476 142)" style={{'--shirt':'#7374a2','--hair':'#403d58','--trousers':'#4b5268'}}><Person/><path d="M-9-28H-5L-2-34 2-29 7-35 10-27Z" fill="#e7bd55" stroke="#9c7933" stroke-width="1.5"/></g>
          <text x="476" y="239" text-anchor="middle" class="office-name office-manager-name">{props.manager.slice(0, 22)}</text>
        </g>
        <text x="39" y="55" class="office-room-label office-room-label--light">AGENT FLOOR</text>
        <g class="office-lounge" filter="url(#office-shadow)">
          <rect x="400" y="310" width="156" height="43" rx="5"/><rect x="394" y="356" width="42" height="82" rx="5"/>
          <rect x="455" y="368" width="82" height="55" rx="3" class="office-coffee-table"/><circle cx="496" cy="395" r="8" class="office-coffee-cup"/>
        </g>
        <For each={resting()}>{(item, index) => <text x={index() ? 522 : 428} y="380" text-anchor="middle" class="office-resting-name">{name(item).slice(0,6)}</text>}</For>
        <g class="office-plant" transform="translate(545 475)"><rect x="-12" y="5" width="24" height="16" rx="2"/><path d="M0 6C-3-18-16-18-17-7M0 6C4-20 18-20 18-7M0 6C0-25 9-28 11-17"/></g>
        <text x="478" y="456" text-anchor="middle" class="office-room-label office-room-label--light">LOUNGE / 休息区</text>
        <rect x="413" y={height()-117} width="130" height="42" rx="3" fill="#aa7f58" stroke="#76583f" stroke-width="3"/>
        <path d={`M423 ${height()-75}v17m110-17v17`} stroke="#76583f" stroke-width="6"/><rect x="442" y={height()-137} width="72" height="24" rx="3" fill="#d8ccb6" stroke="#82715f" stroke-width="3"/>
        <For each={members()}>{(item, index) => {
          const x = () => 105 + (index() % 2) * 170;
          const y = () => 154 + Math.floor(index() / 2) * 138;
          const status = () => normalizeSubagentStatus(props.statusOf(item));
          const tool = () => {
            const tools = props.toolsOf(item);
            return tools[tools.length - 1] || '';
          };
          const visual = () => officeActorState(props.statusOf(item), props.activityOf(item), tool());
          const palette = () => officeActorPalette(item.session_id);
          const visiting = () => Boolean(visits()[item.session_id]);
          const action = () => idle()[item.session_id] || 0;
          const restIndex = () => resting().findIndex(restingItem => restingItem.session_id === item.session_id);
          const label = () => visiting() ? '向负责人汇报' : status() === 'running' ? '工作中' : status() === 'failed' ? '需要关注' : isResting(item) ? '休息区 · 休息' : `${status() === 'completed' ? '已完成 · ' : '待命 · '}${action() === 0 ? '等待安排' : idleActions[action()]}`;
          return <g class="office-member" role="button" tabindex="0" aria-label={`${name(item)}，${label()}，查看任务`} aria-pressed={selected() === item.session_id}
            onClick={() => setSelected(item.session_id)} onKeyDown={event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();setSelected(item.session_id);}}}>
            <title>{name(item)}：{props.activityOf(item)}</title>
            <rect class="office-seat-focus" x={x()-76} y={y()-49} width="152" height="116" rx="6"/>
            <ellipse cx={x()} cy={y()+47} rx="70" ry="14" fill="#3b261d" opacity=".18"/>
            <rect x={x()-61} y={y()+8} width="122" height="39" rx="2" fill="#bd9168" stroke="#7a553c" stroke-width="3" filter="url(#office-shadow)"/>
            <path d={`M${x()-52} ${y()+47}v14m104-14v14`} stroke="#6c4c37" stroke-width="6"/>
            <rect x={x()-39} y={y()-5} width="43" height="31" rx="2" fill={status()==='running'?'#5e9e8f':'#53686a'} stroke="#ede3d0" stroke-width="4"/>
            <rect x={x()-23} y={y()+27} width="17" height="4" fill="#6b4f3b"/>
            <rect x={x()+31} y={y()+19} width="13" height="14" rx="2" fill="#f1dfbf"/><path d={`M${x()+44} ${y()+22}q8 0 5 7h-5`} fill="none" stroke="#765d49" stroke-width="2"/>
            <circle cx={x()+56} cy={y()-32} r="4" class={status()==='running'?'office-status-light is-live':'office-status-light'}/>
            <g class="office-traveler" classList={{'office-reporting':visiting(),'office-spawning':Boolean(spawns()[item.session_id]),'is-resting':isResting(item)}} style={{'--home-x':`${x()}px`,'--home-y':`${y()}px`,'--rest-x':`${restIndex() ? 522 : 428}px`,'--rest-y':'337px','--entry-y':`${height()-82}px`}} onAnimationEnd={() => {setVisits(value => Object.fromEntries(Object.entries(value).filter(([id]) => id !== item.session_id)));setSpawns(value => Object.fromEntries(Object.entries(value).filter(([id]) => id !== item.session_id)));}}>
              <g class={`office-action office-action--${visual()==='idle'?`idle-${action()}`:visual()}`} style={{'--shirt':palette().shirt,'--hair':palette().hair,'--trousers':palette().trousers}}><Person/>
                <text x="16" y="-19" class="office-prop" aria-hidden="true">{visiting()?'✓':visual()==='failed'?'!':visual()==='interrupted'?'‼':visual()==='asking'?'?':visual()==='thinking'?'···':visual()==='tooling'?officeToolGlyph(tool()):visual()==='done'?'✓':['','▪','♫','z Z'][action()]}</text>
              </g>
              <Show when={visiting()}><g class="office-report-bubble" aria-hidden="true">
                <rect x="20" y="-56" width="112" height="29" rx="7"/>
                <path d="M25-28 17-20 39-28Z"/>
                <text x="76" y="-37" text-anchor="middle">{reportLines[reports()[item.session_id] || 0]}</text>
              </g></Show>
            </g>
            <text x={x()} y={y()+77} text-anchor="middle" class="office-name">{name(item).length>13?`${name(item).slice(0,12)}…`:name(item)}</text>
            <text x={x()} y={y()+93} text-anchor="middle" class="office-status">{label()}</text>
          </g>;
        }}</For>
        <Show when={peerExchange()}>{exchange => {
          const path = `M${exchange().from.x} ${exchange().from.y-28} Q${(exchange().from.x+exchange().to.x)/2} ${Math.min(exchange().from.y,exchange().to.y)-78} ${exchange().to.x} ${exchange().to.y-28}`;
          return <g class="office-peer-exchange" aria-label={`${exchange().thread.sender_worker} 与 ${exchange().thread.target_worker} 正在进行第 ${exchange().thread.round_index} 轮协作`}>
            <path d={path} class="office-peer-path"/>
            <circle r="6" class="office-peer-signal"><animateMotion path={path} dur="1.8s" repeatCount="indefinite"/></circle>
            <circle r="4" class="office-peer-signal office-peer-signal--reply"><animateMotion path={path} dur="1.8s" begin=".9s" repeatCount="indefinite" keyPoints="1;0" keyTimes="0;1" calcMode="linear"/></circle>
            <g transform={`translate(${(exchange().from.x+exchange().to.x)/2} ${Math.min(exchange().from.y,exchange().to.y)-82})`} class="office-peer-bubble">
              <rect x="-55" y="-14" width="110" height="28" rx="8"/><text text-anchor="middle" y="4">{exchange().thread.status==='pending'?'正在请教…':'已完成一轮问答'}</text>
            </g>
          </g>;
        }}</Show>
        <Show when={!members().length}><text x="300" y="280" text-anchor="middle" class="office-name">等待成员加入 · 发起协作后自动入座</text></Show>
      </svg>
      </div>
    </div>
    <footer class="office-details"><Show when={focused()} fallback={<p>点击小人查看委托任务。动画跟随真实状态。</p>}>{item => <>
      <strong>{name(item())}</strong><p>{props.activityOf(item())}</p>
      <Show when={props.contractOf(item())?.goal}><p>目标：{props.contractOf(item())?.goal}</p></Show>
      <Show when={props.contractOf(item())?.deliverable}><p>交付：{props.contractOf(item())?.deliverable}</p></Show>
      <Show when={props.contractOf(item())?.stop_when}><p>完成条件：{props.contractOf(item())?.stop_when}</p></Show>
      <button onClick={() => props.onDiscuss(item())}>与此成员对话 ↗</button>
    </>}</Show></footer>
  </section>;
}
