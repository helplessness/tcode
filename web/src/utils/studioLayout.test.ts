import { describe,it,expect } from 'vitest';
import { defaultStudioLayout,isPageAvailableOnSurface,parseStudioLayout,studioDimensions,inspectorAnchors,snapInspector,rubberWidth,drawerSpringStep,studioNavigationPages,studioNavigationPagesForSurface,studioPageFromHash,studioPages } from './studioLayout';
describe('complete studio shell',()=>{
  it('resizes freely through 75 percent and snaps to full only above the threshold',()=>{
    const anchors=inspectorAnchors(1440,64);
    expect(anchors).toEqual([280,1376]);
    for(const width of [280,350,500,688,700,1000,1032])for(const velocity of [-2,0,2])expect(snapInspector(width,velocity,anchors)).toBe(width);
    expect(snapInspector(200,0,anchors)).toBe(280);
    for(const velocity of [-2,0,2])expect(snapInspector(1032.01,velocity,anchors)).toBe(1376);
    expect(snapInspector(1100,0,anchors)).toBe(1376);
    expect(snapInspector(1250,0,anchors)).toBe(1376);
    expect(snapInspector(1350,-.1,anchors)).toBe(1376);
    expect(inspectorAnchors(1440,64+280).slice(-1)).toEqual([1096]);
    expect(inspectorAnchors(1440,344)).toEqual([280,1096]);
    expect(snapInspector(548,2,inspectorAnchors(1440,344))).toBe(548);
    expect(snapInspector(822,2,inspectorAnchors(1440,344))).toBe(822);
    expect(snapInspector(823,-2,inspectorAnchors(1440,344))).toBe(1096);
    const merged=defaultStudioLayout();merged.inspectorMerged=true;
    expect(parseStudioLayout(JSON.stringify(merged)).inspectorMerged).toBe(true);
    expect(parseStudioLayout('{}').inspectorMerged).toBe(false);
  });
  it('damps overflow and settles the spring exactly at its target',()=>{
    expect(rubberWidth(200,280,1376)).toBeGreaterThan(216);
    expect(rubberWidth(200,280,1376)).toBeLessThan(280);
    expect(rubberWidth(1700,280,1376)).toBeLessThan(1440);
    let state={width:1430,velocity:1};
    for(let i=0;i<180;i++)state=drawerSpringStep(state.width,state.velocity,1376,16);
    expect(state.width).toBeCloseTo(1376,2);
    expect(Math.abs(state.velocity)).toBeLessThan(.015);
    let rebound={width:1376,velocity:-.55},minimum=rebound.width;
    for(let i=0;i<60;i++){rebound=drawerSpringStep(rebound.width,rebound.velocity,1376,16);minimum=Math.min(minimum,rebound.width);}
    expect(minimum).toBeLessThan(1372);
    expect(minimum).toBeGreaterThan(1360);
    expect(rebound.width).toBeCloseTo(1376,1);
  });
  it('reserves a readable desktop chat canvas when both sidebars are open',()=>{
    const layout=defaultStudioLayout();layout.sizes.inspector=.6;
    const d=studioDimensions(layout,1440,900,false,64,560);
    expect(1440-d.rail-d.tree-d.inspector).toBeGreaterThanOrEqual(560);
  });
  it('persists the sidebar vertical split and safely restores old preferences',()=>{
    expect(parseStudioLayout('{}').navigationShare).toBe(.22);
    expect(parseStudioLayout(JSON.stringify({navigationShare:.15})).navigationShare).toBe(.15);
    expect(parseStudioLayout(JSON.stringify({navigationShare:9})).navigationShare).toBe(.55);
    expect(parseStudioLayout(JSON.stringify({navigationShare:-1})).navigationShare).toBe(.10);
  });
  it('allows narrow desktop navigation without changing web dimensions',()=>{
    const layout=defaultStudioLayout();layout.sizes.tree=.08;layout.hidden.inspector=true;
    expect(studioDimensions(layout,1440,900,false,64).tree).toBe(180);
    expect(studioDimensions(layout,1440,900,false,52).tree).toBe(200);
  });
  it('gives desktop navigation room without reducing the minimum main workspace',()=>{
    const layout=defaultStudioLayout();layout.sizes.tree=.5;layout.sizes.inspector=.5;
    for(const width of [800,1024,1440]){
      const d=studioDimensions(layout,width,900,false,64);
      expect(d.rail).toBe(64);
      expect(width-d.rail-d.tree-d.inspector).toBeGreaterThanOrEqual(360);
    }
    expect(studioDimensions(layout,1440,900).rail).toBe(52);
    expect(studioDimensions(layout,1440,900,true,64).rail).toBe(0);
  });
  it('keeps all prior workspaces and exposes previously nested modules',()=>{
    const ids=studioPages.map(p=>p.id);
    expect(new Set(ids).size).toBe(ids.length);
    for(const id of ['console','sidechat','kanban','scheduled','research','wiki','ontology','protein-runs','assets','changes','subagents','team','symphony','mcp','plugins','jobs','review','workflow','structure'])expect(ids).toContain(id);
    expect(studioPageFromHash('#/workflow')).toBe('workflow');expect(studioPageFromHash('#/unknown')).toBe('console');
  });
  it('keeps task context pages out of fixed navigation without breaking task links',()=>{
    expect(studioNavigationPages.map(page=>page.id)).not.toContain('console');
    expect(studioNavigationPages.map(page=>page.id)).not.toContain('protein-runs');
    expect(studioPageFromHash('#/protein-runs')).toBe('protein-runs');
    expect(studioPages.find(page=>page.id==='protein-runs')?.label).toBe('实验运行');
  });
  it('shows MCP status and configuration on web while keeping desktop settings local',()=>{
    expect(studioNavigationPagesForSurface('web').map(page=>page.id)).toContain('mcp');
    expect(studioNavigationPagesForSurface('web').map(page=>page.id)).not.toContain('settings');
    expect(studioNavigationPagesForSurface('desktop').map(page=>page.id)).toContain('mcp');
    expect(isPageAvailableOnSurface('mcp','web')).toBe(true);
    expect(isPageAvailableOnSurface('plugins','web')).toBe(true);
  });
  it('rejects corrupt saved preferences',()=>{
    expect(parseStudioLayout('{')).toEqual(defaultStudioLayout());
    const parsed=parseStudioLayout(JSON.stringify({sizes:{tree:900,inspector:'200'},hidden:{tree:true,dock:'true'}}));
    expect(parsed.sizes.tree).toBe(.5);expect(parsed.sizes.inspector).toBe(.19);expect(parsed.hidden.tree).toBe(true);expect(parsed.hidden.dock).toBe(false);
  });
  it('protects main area and releases every pixel in focus mode',()=>{
    const layout=defaultStudioLayout();layout.sizes.tree=.5;layout.sizes.inspector=.5;
    for(const width of [800,1024,1440]){const d=studioDimensions(layout,width,900);expect(width-d.rail-d.tree-d.inspector).toBeGreaterThanOrEqual(360);}
    expect(studioDimensions(layout,1440,900,true)).toEqual({rail:0,tree:0,inspector:0,dock:0});
    expect(layout.hidden.tree).toBe(false);
  });
  it('allows the companion pane to reach 60% and preserves it across reloads',()=>{
    const layout=defaultStudioLayout();layout.sizes.inspector=.6;
    const restored=parseStudioLayout(JSON.stringify(layout));
    expect(restored.sizes.inspector).toBe(.6);
    for(const width of [1740,1920,2560]){
      const d=studioDimensions(restored,width,1000);
      expect(d.inspector).toBeCloseTo(width*.6);
      expect(width-d.rail-d.tree-d.inspector).toBeGreaterThanOrEqual(360);
    }
    restored.sizes.inspector=1;
    expect(parseStudioLayout(JSON.stringify(restored)).sizes.inspector).toBe(1);
    const narrow=studioDimensions(restored,1024,900);
    expect(narrow.inspector).toBeLessThanOrEqual(1024*.6);
    expect(1024-narrow.rail-narrow.tree-narrow.inspector).toBeGreaterThanOrEqual(360);
  });
});
