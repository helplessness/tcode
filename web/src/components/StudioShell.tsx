import { For, Show, createEffect, createMemo, createSignal, on, onCleanup, onMount, type JSX } from 'solid-js';
import { defaultStudioLayout, parseStudioLayout, studioDimensions, inspectorAnchors, snapInspector, rubberWidth, drawerSpringStep, studioPages, studioNavigationPagesForSurface, studioPanels, type StudioPage, type StudioPanel, type ResizablePanel, type StudioSurface } from '../utils/studioLayout';
import type { RuntimeNotice } from '../utils/sessionRuntimeNotices';
import '../studio.css';
import '../desktop.css';
import '../interface.css';
import DesktopServices from './DesktopServices';
import ComputerUsePanel from './ComputerUsePanel';

export type StudioShellProps = {
  surface: StudioSurface;
  page: StudioPage; onNavigate: (page: StudioPage)=>void; identity: string;
  workspace: string; sessionTitle: string; status: string;
  sidebar: JSX.Element; inspector: JSX.Element; dock: JSX.Element; children: JSX.Element;
  actions: JSX.Element; sidebarRequest: number; onNewSession: () => void; onCommandPalette: () => void;
  onOpenSkillCenter: () => void; skillCount: number;
  onOpenServiceSettings: (section: 'mcp' | 'desktop') => void;
  browserActive?: boolean;
  inspectorTabLabels?: string[];
  inspectorRequest?: number; inspectorCloseRequest?: number; inspectorTitle?: string; onInspectorVisibility?: (visible: boolean) => void;
  notices: RuntimeNotice[]; onNoticeSelect: (notice: RuntimeNotice) => void;
  noticeCounts: Record<'jobs' | 'review' | 'workflow', number>;
  contextTabs?: ReadonlyArray<{id: string; label: string}>;
  activeContextTab?: string;
  onContextTabSelect?: (id: string) => void;
  mergedMainActive?: boolean;
  onMergedChange?: (merged: boolean) => void;
  splitRequest?: number;
};

const ActivityIcon = (props: { name: 'new' | '任务' | '研究库' | '自动化' | '扩展' | '浏览器' | '设置' }) => {
  const paths: Record<typeof props.name, JSX.Element> = {
    new: <><path d="M12 5v14M5 12h14" /></>,
    任务: <><path d="M5 6.5h14v9H9l-4 3v-12Z" /><path d="M8.5 10h7M8.5 13h4" /></>,
    研究库: <><path d="M4.5 5.5A3.5 3.5 0 0 1 8 3h3v16H8a3.5 3.5 0 0 0-3.5 2V5.5Z" /><path d="M19.5 5.5A3.5 3.5 0 0 0 16 3h-3v16h3a3.5 3.5 0 0 1 3.5 2V5.5Z" /></>,
    自动化: <><circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2M9 2h6" /></>,
    扩展: <><path d="M9 4.5V8H5.5a2.5 2.5 0 1 0 0 5H9v3.5a2.5 2.5 0 1 0 5 0V13h3.5a2.5 2.5 0 1 0 0-5H14V4.5a2.5 2.5 0 1 0-5 0Z" /></>,
    浏览器: <><circle cx="12" cy="12" r="9" /><ellipse cx="12" cy="12" rx="4" ry="9" /><path d="M3 12h18" /></>,
    设置: <><circle cx="12" cy="12" r="3" /><path d="M19 13.5v-3l-2.1-.7a7 7 0 0 0-.7-1.7l1-2-2.1-2.1-2 1a7 7 0 0 0-1.7-.7L10.5 2h-3l-.7 2.1a7 7 0 0 0-1.7.7l-2-1L1 5.9l1 2a7 7 0 0 0-.7 1.7L0 10.5v3l2.1.7a7 7 0 0 0 .7 1.7l-1 2L3.9 20l2-1a7 7 0 0 0 1.7.7l.9 2.3h3l.7-2.1a7 7 0 0 0 1.7-.7l2 1 2.1-2.1-1-2a7 7 0 0 0 .7-1.7l2.3-.9Z" transform="translate(2 1) scale(.83)" /></>,
  };
  return <svg class="studio-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths[props.name]}</svg>;
};

export default function StudioShell(props: StudioShellProps) {
  const noticeStorageKey = 'tcode.hidden-runtime-notices.v1';
  const [hiddenNotices, setHiddenNotices] = createSignal<string[]>( (() => {
    try { const value = JSON.parse(localStorage.getItem(noticeStorageKey) || '[]'); return Array.isArray(value) ? value.filter(item => typeof item === 'string') : []; } catch { return []; }
  })());
  const noticeKey = (notice: RuntimeNotice) => JSON.stringify([props.identity, props.workspace, notice.runtimeId, notice.revision || '']);
  const visibleNotices = createMemo(() => props.notices.filter(notice => !hiddenNotices().includes(noticeKey(notice))));
  const hideNotice = (notice: RuntimeNotice) => {
    const keys = [...hiddenNotices().filter(key => key !== noticeKey(notice)), noticeKey(notice)].slice(-200);
    setHiddenNotices(keys);
    try { localStorage.setItem(noticeStorageKey, JSON.stringify(keys)); } catch { /* 本次会话仍生效。 */ }
  };
  const isDesktop = () => props.surface === 'desktop';
  const defaults=()=>{const value=defaultStudioLayout();value.hidden={rail:false,tree:false,inspector:true,dock:true,tabs:true};value.sizes.tree=isDesktop()?.20:.22;value.sizes.inspector=.38;return value;};
  const [layout,setLayout]=createSignal(defaults());
  const [focused,setFocused]=createSignal(false);
  const browserOpen = () => !!props.browserActive;
  const [viewport,setViewport]=createSignal({width:1440,height:900});
  const [drawerLiveWidth,setDrawerLiveWidth]=createSignal<number|null>(null);
  const [layoutMotion,setLayoutMotion]=createSignal(false);
  let springFrame=0;
  let motionFrame=0;
  let transitionAnimations:Animation[]=[];
  const [opened,setOpened]=createSignal<StudioPage[]>(['console']);
  const [activity,setActivity]=createSignal<'任务'|'研究库'|'自动化'|'扩展'|'设置'>('任务');
  const [selectedFeature,setSelectedFeature]=createSignal<StudioPage | null>('console');
  const settingsMode=()=>props.page==='settings';
  let previousWorkspace = '';
  let root!: HTMLDivElement;
  let menu!: HTMLDetailsElement;
  let actionsMenu!: HTMLDetailsElement;
  let noticeMenu!: HTMLDetailsElement;
  let sidebarSplit: {start:number;share:number;height:number;pointerId:number}|undefined;
  let dragging: {id:ResizablePanel;start:number;size:number;prior:number;priorInspector:number;handle:HTMLElement;pointerId:number;raw:number;velocity:number;time:number}|undefined;
  const storageKey=()=>`tcode-${isDesktop()?'desktop':'studio'}-layout-v1:${props.identity}`;
  const hidden=(id:StudioPanel)=>focused()||settingsMode()||layout().hidden[id];
  const mainMinimum=()=>isDesktop()&&viewport().width>=1100?560:360;
  const dimensions=createMemo(()=>studioDimensions({...layout(),sizes:{...layout().sizes,inspector:Math.min(.38,layout().sizes.inspector)}},viewport().width,viewport().height,focused(),isDesktop()?64:52,mainMinimum()));
  const leftBoundary=()=>dimensions().rail+(hidden('tree')?0:dimensions().tree);
  const anchors=()=>inspectorAnchors(viewport().width,leftBoundary());
  const merged=()=>!hidden('inspector')&&viewport().width>780&&(layout().inspectorMerged||(!dragging&&drawerLiveWidth()===null&&layout().sizes.inspector*viewport().width>=viewport().width-leftBoundary()-1));
  const drawerWidth=()=>drawerLiveWidth()??(merged()?viewport().width-leftBoundary():Math.max(anchors()[0],Math.min(anchors()[anchors().length-1],viewport().width*layout().sizes.inspector)));
  let splitShare=.38;
  createEffect(()=>props.onMergedChange?.(merged()));
  createEffect(on(()=>props.splitRequest,request=>{if(!request)return;const share=Math.min(splitShare,dimensions().inspector/viewport().width);settleDrawer(share*viewport().width);}));
  const reducedMotion=()=>document.documentElement.dataset.reduceMotion==='true'||window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function stopSpring(){cancelAnimationFrame(springFrame);cancelAnimationFrame(motionFrame);springFrame=0;motionFrame=0;transitionAnimations.forEach(animation=>animation.cancel());transitionAnimations=[];setLayoutMotion(false);}
  function settleDrawer(target:number,velocity=0){
    const start=drawerWidth(),wasMerged=merged();
    const main=root.querySelector<HTMLElement>('.studio-work'),mainWidth=main?.getBoundingClientRect().width;
    stopSpring();const max=anchors()[anchors().length-1];
    const changingMode=wasMerged!==(target===max),fromMain=wasMerged&&props.mergedMainActive;
    setLayoutMotion(changingMode&&!reducedMotion());
    if(target===max&&!layout().inspectorMerged)splitShare=Math.min(.38,layout().sizes.inspector);
    setLayout(previous=>({...previous,inspectorMerged:target===max}));resize('inspector',target);save();
    if(reducedMotion()){setDrawerLiveWidth(null);return;}
    if(changingMode)motionFrame=requestAnimationFrame(()=>{
      motionFrame=0;
      if(!layoutMotion())return;
      if(main&&mainWidth!==undefined)transitionAnimations.push(main.animate([{width:`${mainWidth}px`},{width:`${viewport().width-leftBoundary()-(target===max?0:dimensions().inspector)}px`}],{duration:420,easing:'cubic-bezier(.2,.7,.2,1)'}));
      const content=root.querySelector<HTMLElement>(target===max?'.companion-tabs':'.studio-work');
      if(content)transitionAnimations.push(content.animate([{opacity:.35,transform:'translateY(5px)'},{opacity:1,transform:'translateY(0)'}],{duration:320,easing:'cubic-bezier(.2,.7,.2,1)'}));
      const companion=root.querySelector<HTMLElement>('.studio-context>div');
      if(fromMain&&companion)transitionAnimations.push(companion.animate([{opacity:0},{opacity:1}],{duration:280,easing:'ease-out'}));
    });
    const began=performance.now();
    let width=Math.min(start,max),speed=target===max&&start>=max-16?-.55:Math.max(-2,Math.min(2,velocity)),time=began;setDrawerLiveWidth(width);
    const tick=(now:number)=>{const next=drawerSpringStep(width,speed,target,now-time);time=now;width=next.width;speed=next.velocity;setDrawerLiveWidth(width);
      if(Math.abs(width-target)<.4&&Math.abs(speed)<.015&&(!changingMode||now-began>=440)){springFrame=0;setDrawerLiveWidth(null);setLayoutMotion(false);transitionAnimations=[];}else springFrame=requestAnimationFrame(tick);};
    springFrame=requestAnimationFrame(tick);
  }
  createEffect(()=>{if(hidden('inspector')){stopSpring();setDrawerLiveWidth(null);}});
  const activeTitle=()=>studioPages.find(p=>p.id===props.page)?.label || '主聊天';
  const navigationPages=()=>studioNavigationPagesForSurface(props.surface);
  const activities=()=>['任务','研究库','自动化','扩展','设置'].filter(group=>navigationPages().some(page=>page.group===group)) as ('任务'|'研究库'|'自动化'|'扩展'|'设置')[];
  createEffect(()=>{
    const key=storageKey();
    try {const saved=localStorage.getItem(key);setLayout(saved?parseStudioLayout(saved):defaults());}catch{setLayout(defaults());}
    setFocused(false);
  });
  createEffect(on(() => [props.workspace, props.page] as const, ([workspace, page]) => {
    // Ignore initial hydration/temporary empty scope, preserving deep links.
    // Only workspace changes reset tabs; session changes in the same one do not.
    const changed = Boolean(workspace && previousWorkspace && workspace !== previousWorkspace);
    if (workspace) previousWorkspace = workspace;
    if (changed) {
      setOpened(['console']);
      props.onNavigate('console');
      return;
    }
    const pageGroup = studioPages.find(item => item.id === page)?.group;
    if (pageGroup) {
      setActivity(pageGroup as '任务'|'研究库'|'自动化'|'扩展'|'设置');
      setSelectedFeature(page);
    }
    if (page !== 'settings') setOpened(previous => previous.includes(page) ? previous : [...previous, page]);
  }));
  createEffect(()=>{
    if(props.sidebarRequest>0){setFocused(false);setLayout(previous=>({...previous,hidden:{...previous.hidden,tree:false}}));}
  });
  const save=()=>{try{localStorage.setItem(storageKey(),JSON.stringify(layout()));}catch{/* Layout remains usable without storage. */}};
  createEffect(on(() => props.inspectorRequest, request => {
    if (request) { setFocused(false); setLayout(previous => ({...previous,hidden:{...previous.hidden,inspector:false}})); save(); }
  }));
  createEffect(on(() => props.inspectorCloseRequest, request => {
    if (request) { setLayout(previous => ({...previous,hidden:{...previous.hidden,inspector:true}})); save(); }
  }));
  createEffect(() => props.onInspectorVisibility?.(!hidden('inspector')&&!(merged()&&props.mergedMainActive)));
  function toggle(id:StudioPanel){
    if(focused()){setFocused(false);setLayout(previous=>({...previous,hidden:{rail:true,tree:true,inspector:true,dock:true,tabs:true}}));}
    setLayout(previous=>({...previous,hidden:{...previous.hidden,[id]:!previous.hidden[id]}}));save();
  }
  function bounds(id:ResizablePanel){
    const d=dimensions(),{width,height}=viewport();
    if(id==='tree')return [180,Math.max(180,Math.min(460,width-d.rail-(isDesktop()&&!hidden('inspector')?280:d.inspector)-mainMinimum())),width];
    if(id==='inspector'){
      const points=anchors();return [points[0],points[points.length-1],width];
    }
    return [100,Math.max(100,Math.min(height*.5,height-280)),height];
  }
  function resize(id:ResizablePanel,pixels:number){
    const [min,max,total]=bounds(id);
    setLayout(previous=>({...previous,sizes:{...previous.sizes,[id]:Math.max(min,Math.min(max,pixels))/total}}));
    // Give an explicit left-pane resize priority over the saved right-pane width.
    if(id==='tree'&&isDesktop()&&!hidden('inspector')&&!merged()){
      const available=viewport().width-dimensions().rail-Math.max(min,Math.min(max,pixels))-mainMinimum();
      setLayout(previous=>({...previous,sizes:{...previous.sizes,inspector:Math.min(previous.sizes.inspector,available/total)}}));
    }
  }
  function finishDrag(cancel=false){
    if(!dragging)return;
    const last=dragging;dragging=undefined;
    if(cancel)setLayout(previous=>({...previous,sizes:{...previous.sizes,[last.id]:last.prior,...(last.id==='tree'?{inspector:last.priorInspector}:{})}}));
    if(last.handle.hasPointerCapture(last.pointerId))last.handle.releasePointerCapture(last.pointerId);
    delete document.body.dataset.studioResizing;
    if(last.id==='inspector'){
      if(cancel){stopSpring();setDrawerLiveWidth(null);}else {const velocity=performance.now()-last.time>100?0:last.velocity,target=snapInspector(last.raw,velocity,anchors());settleDrawer(target,target===last.raw?0:velocity);}
    }else if(!cancel)save();
  }
  const divider=(id:ResizablePanel)=>(
    <div class={`studio-divider studio-divider-${id}`} role="separator" tabindex="0"
      hidden={hidden(id)} aria-label={`调整${studioPanels[id]}${id==='dock'?'高度':'宽度'}`}
      aria-orientation={id==='dock'?'horizontal':'vertical'} aria-controls={`studio-${id}`}
      aria-valuemin={Math.round(bounds(id)[0])} aria-valuemax={Math.round(bounds(id)[1])} aria-valuenow={Math.round(id==='inspector'?drawerWidth():dimensions()[id])}
      title="拖动调整比例；双击重置；方向键微调；Enter 收起"
      onPointerDown={event=>{if(event.button!==0)return;event.preventDefault();event.currentTarget.focus();const size=id==='inspector'?drawerWidth():dimensions()[id];stopSpring();dragging={id,start:id==='dock'?event.clientY:event.clientX,size,raw:size,velocity:0,time:performance.now(),prior:layout().sizes[id],priorInspector:layout().sizes.inspector,handle:event.currentTarget,pointerId:event.pointerId};event.currentTarget.setPointerCapture(event.pointerId);document.body.dataset.studioResizing=id==='dock'?'row':'column';}}
      onPointerMove={event=>{if(!dragging||dragging.pointerId!==event.pointerId)return;const delta=(id==='dock'?event.clientY:event.clientX)-dragging.start;const raw=dragging.size+delta*(id==='tree'?1:-1),now=performance.now();dragging.velocity=(raw-dragging.raw)/Math.max(1,now-dragging.time);dragging.raw=raw;dragging.time=now;resize(id,raw);if(id==='inspector')setDrawerLiveWidth(rubberWidth(raw,bounds(id)[0],bounds(id)[1]));}}
      onPointerUp={()=>finishDrag()} onPointerCancel={()=>finishDrag(true)} onLostPointerCapture={()=>finishDrag()}
      onDblClick={()=>{if(id==='inspector'){settleDrawer(snapInspector(defaultStudioLayout().sizes.inspector*viewport().width,0,anchors()));return;}setLayout(previous=>({...previous,sizes:{...previous.sizes,[id]:defaultStudioLayout().sizes[id]}}));save();}}
      onKeyDown={event=>{
        if(event.key==='Enter'){event.preventDefault();toggle(id);menu.querySelector('summary')?.focus();return;}
        const delta:Record<string,number>=id==='dock'?{ArrowUp:16,ArrowDown:-16}:id==='tree'?{ArrowRight:16,ArrowLeft:-16}:{ArrowLeft:16,ArrowRight:-16};
        if(!(event.key in delta)&&!['Home','End'].includes(event.key))return;
        event.preventDefault();if(id==='inspector'){const points=anchors(),current=drawerWidth(),max=points[points.length-1];settleDrawer(event.key==='Home'?points[0]:event.key==='End'?max:current>max*.75&&delta[event.key]<0?Math.max(points[0],max*.75):snapInspector(current+delta[event.key],0,points));return;}resize(id,event.key==='Home'?bounds(id)[0]:event.key==='End'?bounds(id)[1]:dimensions()[id]+delta[event.key]);save();
      }} />
  );
  onMount(()=>{
    const observer=new ResizeObserver(()=>{finishDrag(true);stopSpring();setDrawerLiveWidth(null);setViewport({width:root.clientWidth,height:root.clientHeight});});observer.observe(root);
    const blur=()=>finishDrag(true);
    const release=()=>finishDrag();
    const key=(event:KeyboardEvent)=>{if(event.key==='Escape'){finishDrag(true);if(menu.open){menu.open=false;menu.querySelector('summary')?.focus();}if(actionsMenu?.open){actionsMenu.open=false;actionsMenu.querySelector('summary')?.focus();}if(noticeMenu?.open){noticeMenu.open=false;noticeMenu.querySelector('summary')?.focus();}}};
    const click=(event:MouseEvent)=>{if(!menu.contains(event.target as Node))menu.open=false;if(actionsMenu && !actionsMenu.contains(event.target as Node))actionsMenu.open=false;if(noticeMenu && !noticeMenu.contains(event.target as Node))noticeMenu.open=false;};
    window.addEventListener('pointerup',release,true);window.addEventListener('mouseup',release,true);
    window.addEventListener('blur',blur);window.addEventListener('keydown',key);document.addEventListener('click',click);
    onCleanup(()=>{observer.disconnect();finishDrag(true);stopSpring();window.removeEventListener('pointerup',release,true);window.removeEventListener('mouseup',release,true);window.removeEventListener('blur',blur);window.removeEventListener('keydown',key);document.removeEventListener('click',click);});
  });
  return <div ref={root} class="studio-shell" classList={{'studio-layout-motion':layoutMotion()}} data-merged={merged()} data-merged-main={merged()&&props.mergedMainActive} data-drawer-overlay={!hidden('inspector')&&drawerWidth()>dimensions().inspector+1} data-focus={focused()} data-page={props.page} style={{'--studio-drawer-width':`${drawerWidth()}px`,'--studio-rail':`${dimensions().rail}px`,'--studio-tree':`${hidden('tree')?0:dimensions().tree}px`,'--studio-inspector':`${hidden('inspector')||merged()?0:dimensions().inspector}px`,'--studio-dock':`${dimensions().dock}px`}}>
    <a class="studio-skip" href="#studio-main">跳到主工作区</a>
    <header class="studio-topbar" hidden={settingsMode()}>
      <button class="studio-brand" onClick={()=>props.onNavigate('console')} aria-label="返回主聊天"><span>t</span><b>Tcode</b></button>
      <span class="studio-breadcrumb" title={props.workspace}>{props.workspace || '选择工作区'} <i>/</i> {activeTitle()}</span>
      <details ref={noticeMenu} class="studio-notices" data-review={visibleNotices().some(notice => notice.kind === 'review')} hidden={!visibleNotices().length}>
        <summary aria-label={`运行提醒，共 ${visibleNotices().length} 项`} title="待处理运行；查看不会自动完成审核">
          <span class="studio-notices-dot" aria-hidden="true" />
          <span>运行提醒 {visibleNotices().length}</span>
        </summary>
        <div class="studio-notices-menu">
          <b>{props.workspace ? '当前工作区' : '当前账号'}</b>
          <p class="px-2 text-xs">查看不会完成审核。隐藏只关闭这条提醒；运行有新变化时会再次提醒。</p>
          <For each={visibleNotices()}>{notice=><div>
            <button onClick={()=>{noticeMenu.open=false;props.onNoticeSelect(notice);}}>
            <span class={`studio-notice-kind studio-notice-kind-${notice.kind}`}>{notice.kind==='review'?(notice.page==='review'?'待审核':'待处理提案'):notice.kind==='workflow'?'Workflow 运行':'异步运行'}</span>
            <span class="studio-notice-title" title={notice.title}>{notice.title}</span>
            <span aria-hidden="true">↗</span>
          </button>
          <button aria-label={`隐藏提醒：${notice.title}`} onClick={() => hideNotice(notice)}>隐藏提醒</button>
          </div>}</For>
        </div>
      </details>
      <div class="studio-tools" data-testid="app-topbar-actions">
        <Show when={isDesktop()}><button class="studio-command-trigger" onClick={props.onCommandPalette} title="打开全局命令面板">搜索 <kbd>⌘K</kbd></button></Show>
        <Show when={isDesktop()}><ComputerUsePanel/></Show><DesktopServices onOpenSettings={props.onOpenServiceSettings} />
        <button aria-pressed={focused()} onClick={()=>setFocused(!focused())}>{focused()?'退出专注':'专注模式'}</button>
        <div class="studio-inspector-toggle-wrap">
          <button class="studio-inspector-toggle" aria-label={`${hidden('inspector') ? '打开' : '隐藏'}右侧面板，${props.inspectorTabLabels?.length || 0} 个标签`} aria-expanded={!hidden('inspector')} aria-describedby="studio-open-tabs-hint" onClick={()=>toggle('inspector')}><span>{props.inspectorTabLabels?.length || 0}</span></button>
          <div id="studio-open-tabs-hint" class="studio-open-tabs-hint" role="tooltip"><b>已打开的标签页</b><For each={props.inspectorTabLabels || []}>{label=><div>{label}</div>}</For><Show when={!props.inspectorTabLabels?.length}><div>暂无打开的标签页</div></Show></div>
        </div>
        <details ref={menu} class="studio-layout-menu"><summary>布局与面板</summary><div>
          <b>显示区域</b><For each={Object.keys(studioPanels) as StudioPanel[]}>{id=><label><input type="checkbox" checked={!hidden(id)} onChange={()=>toggle(id)}/>{studioPanels[id]}</label>}</For>
          <p>拖动边界调整比例，双击重置。方向键可微调，Enter 收起。</p>
          <button onClick={()=>{setLayout(defaults());setFocused(false);save();}}>恢复默认布局</button>
          <small>比例与开关按账号保存在{isDesktop()?'此电脑':'此浏览器'}</small>
        </div></details>
        <Show when={isDesktop()} fallback={props.actions}><details ref={actionsMenu} class="studio-layout-menu desktop-actions"><summary>更多操作</summary><div onClick={event=>{if((event.target as HTMLElement).closest('button'))actionsMenu.open=false;}}>{props.actions}</div></details></Show>
      </div>
    </header>
    <nav class="studio-rail" aria-label="活动导航" hidden={hidden('rail')}>
      <button class="studio-rail-new" aria-label="新建会话" onClick={props.onNewSession}><ActivityIcon name="new"/><span class="studio-rail-label" role="tooltip">新建会话</span></button>
      <For each={activities()}>{group=><>
        <button aria-label={group} aria-current={activity()===group?'page':undefined} onClick={()=>{
          if(group==='设置'){props.onNavigate('settings');return;}
          setActivity(group);setFocused(false);
          const target=settingsMode()?navigationPages().find(item=>item.group===group)?.id:null;
          setSelectedFeature(target || null);if(target)props.onNavigate(target);
          setLayout(previous=>({...previous,hidden:{...previous.hidden,tree:false}}));save();
        }}><ActivityIcon name={group}/><span class="studio-rail-label" role="tooltip">{group}</span></button>
        <Show when={isDesktop() && group==='扩展'}><button aria-label="浏览器" onClick={() => window.dispatchEvent(new CustomEvent('tcode:open-browser'))}><ActivityIcon name="浏览器"/><span class="studio-rail-label" role="tooltip">浏览器</span></button></Show>
      </>}</For>
    </nav>
    <aside id="studio-tree" class="studio-explorer" hidden={hidden('tree')} aria-label="资源与会话" style={isDesktop()?{'--studio-navigation-share':`${layout().navigationShare*100}%`}:undefined}>
      <header><b>{activity()==='任务'?'任务与研究空间':activity()}</b><button aria-label="收起资源与会话" onClick={()=>toggle('tree')}>×</button></header>
      <nav class="studio-resource-nav" aria-label="功能导航"><details open><summary>功能与工作区</summary>
        <For each={[activity()]}>{group=><div class="studio-nav-group"><For each={navigationPages().filter(item=>item.group===group)}>{item=>{
          const count=()=>props.noticeCounts[item.id as keyof StudioShellProps['noticeCounts']] || 0;
          return <button aria-current={selectedFeature()===item.id?'page':undefined} aria-label={`${item.label}${count()?`，${count()} 项提醒`:''}`} onClick={()=>{setSelectedFeature(item.id);props.onNavigate(item.id);}}><span>{item.mark}</span>{item.label}<Show when={count()>0}><strong class="studio-nav-count">{count()}</strong></Show></button>;
        }}</For><Show when={group==='扩展'}><button data-testid="skill-center-trigger" aria-label={`Skill Center，${props.skillCount} 个 Skill`} onClick={props.onOpenSkillCenter}><span>技</span>Skill Center<strong class="studio-nav-count">{props.skillCount}</strong></button></Show></div>}</For>
      </details></nav>
      <Show when={isDesktop()}><div class="studio-sidebar-split" role="separator" tabindex="0" aria-label="调整功能导航与会话列表高度" aria-orientation="horizontal" aria-valuemin={10} aria-valuemax={55} aria-valuenow={Math.round(layout().navigationShare*100)} title="上下拖动；方向键调整；双击恢复"
        onPointerDown={event=>{if(event.button!==0)return;event.preventDefault();event.currentTarget.focus();sidebarSplit={start:event.clientY,share:layout().navigationShare,height:event.currentTarget.parentElement!.clientHeight,pointerId:event.pointerId};event.currentTarget.setPointerCapture(event.pointerId);}}
        onPointerMove={event=>{if(!sidebarSplit||sidebarSplit.pointerId!==event.pointerId)return;const share=sidebarSplit.share+(event.clientY-sidebarSplit.start)/sidebarSplit.height;setLayout(previous=>({...previous,navigationShare:Math.max(.10,Math.min(.55,share))}));}}
        onPointerUp={event=>{if(!sidebarSplit)return;sidebarSplit=undefined;if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId);save();}}
        onPointerCancel={()=>{if(sidebarSplit)setLayout(previous=>({...previous,navigationShare:sidebarSplit!.share}));sidebarSplit=undefined;}}
        onLostPointerCapture={()=>{sidebarSplit=undefined;}}
        onDblClick={()=>{setLayout(previous=>({...previous,navigationShare:.22}));save();}}
        onKeyDown={event=>{if(!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;event.preventDefault();setLayout(previous=>({...previous,navigationShare:event.key==='Home'?.10:event.key==='End'?.55:Math.max(.10,Math.min(.55,previous.navigationShare+(event.key==='ArrowUp'?-.02:.02)))}));save();}}
      /></Show>
      <div class="studio-sessions">{props.sidebar}</div>
    </aside>
    <section class="studio-work">
      <nav class="studio-tabs" aria-label="工作标签" hidden={hidden('tabs')}><For each={opened()}>{id=>{
        const active=()=>props.page===id && (id!=='console' || !props.contextTabs?.length || props.activeContextTab==='task');
        return <div data-active={active()}>
        <button aria-current={active()?'page':undefined} onClick={()=>{props.onNavigate(id);if(id==='console')props.onContextTabSelect?.('task');}}>{studioPages.find(p=>p.id===id)?.label}</button>
        <Show when={id!=='console'}><button aria-label={`关闭${studioPages.find(p=>p.id===id)?.label}标签`} onClick={()=>{setOpened(previous=>previous.filter(value=>value!==id));if(props.page===id)props.onNavigate('console');}}>×</button></Show>
      </div>}}</For><Show when={props.page==='console'}><For each={props.contextTabs || []}>{tab=><div data-active={props.activeContextTab===tab.id}>
        <button aria-current={props.activeContextTab===tab.id?'page':undefined} onClick={()=>props.onContextTabSelect?.(tab.id)}>{tab.label}</button>
      </div>}</For></Show></nav>
      <main id="studio-main" tabindex="-1" aria-label={activeTitle()}>{props.children}</main>
    </section>
    <aside id="studio-inspector" class="studio-context" data-browser={browserOpen()} data-collaboration={!browserOpen() && (props.inspectorTitle === '协作' || props.inspectorTitle === '伴随查看')} hidden={hidden('inspector')} aria-label={props.inspectorTitle || '会话上下文'}><header><b>{props.inspectorTitle || '会话上下文'}</b><button aria-label="收起右侧面板" onClick={()=>toggle('inspector')}>×</button></header><div>{props.inspector}</div></aside>
    <section id="studio-dock" class="studio-execution" hidden={hidden('dock')} aria-label="底部执行区"><header><nav><For each={studioPages.filter(p=>['jobs','review','workflow','subagents'].includes(p.id))}>{item=><button onClick={()=>props.onNavigate(item.id)}>{item.label}</button>}</For></nav><button aria-label="收起执行区" onClick={()=>toggle('dock')}>×</button></header><div>{props.dock}</div></section>
    {divider('tree')}{divider('inspector')}{divider('dock')}
    <footer class="studio-status" hidden={focused() || settingsMode()}><span>{props.sessionTitle}</span><span>{props.status}</span><span>本地后端 · 当前账号工作区</span></footer>
  </div>;
}
