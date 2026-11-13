// Only type imports: runtime is the pinned, lazy, local official Mol* Viewer bundle.
import type { Viewer } from 'molstar/lib/apps/viewer/app';
import type { lib } from 'molstar/lib/apps/viewer/lib';
import type { Structure, StructureElement } from 'molstar/lib/mol-model/structure';
import type { Camera } from 'molstar/lib/mol-canvas3d/camera';
import type { Color } from 'molstar/lib/mol-util/color';
import type { StructureSelectionQuery } from 'molstar/lib/mol-plugin-state/helpers/structure-selection-query';
import type { ViewerWindow, ViewerApi, ViewState, Residue, StructureSelector, ViewCommand, MeasurementAtom, DistanceMeasurement } from './types';

declare const molstar: { Viewer: typeof Viewer; lib: typeof lib };

async function initialize(): Promise<ViewerApi> {
  const viewer = await molstar.Viewer.create('viewer', {
    layoutIsExpanded: false, layoutShowControls: true, layoutShowSequence: true,
    layoutShowLeftPanel: true, collapseLeftPanel: true, collapseRightPanel: true,
    layoutShowLog: false, layoutShowRemoteState: false, viewportShowExpand: false,
    viewportShowToggleFullscreen: false, viewportShowSelectionMode: true,
    viewportShowScreenshotControls: true, viewportBackgroundColor: '#f5f3ea',
    volumeStreamingDisabled: true,
    // Keep local scientific tools/export; no implicit external annotation requests.
    extensions: ['model-export', 'geo-export', 'ma-quality-assessment'],
  });
  const plugin = viewer.plugin;
  const { StructureElement: E, StructureProperties: P, Unit, Queries, QueryContext, StructureSelection } = molstar.lib.structure;
  const { Vec3, Quat } = molstar.lib.math.LinearAlgebra;
  let path = '', sha256 = '', sourceData: unknown;
  let inventoryStructure: Structure | undefined, chains: NonNullable<ViewState['chains']> = [];
  let previous: Camera.Snapshot[] = [];
  let measurements: DistanceMeasurement[] = [];
  const current = () => {
    const roots = plugin.managers.structure.hierarchy.current.structures;
    if (roots.length !== 1 || !roots[0].cell.obj) throw new Error('请通过工作区加载一个结构，再执行 Agent 操作');
    const structure = roots[0].cell.obj.data;
    if (!sourceData || structure.models.some(model => model.sourceData !== sourceData)) throw new Error('结构来源已变化，请从工作区重新加载以建立文件关联');
    return { root: roots[0], structure };
  };
  const residue = (loc: StructureElement.Location): Residue => ({
    model_index: loc.structure.models.indexOf(loc.unit.model), chain: P.chain.auth_asym_id(loc),
    residue_number: P.residue.auth_seq_id(loc), insertion_code: P.residue.pdbx_PDB_ins_code(loc),
    name: P.residue.label_comp_id(loc), operator: P.unit.operator_name(loc),
  });
  const residues = (loci: StructureElement.Loci, limit = 200) => {
    const result = new Map<string, Residue>(); let truncated = false;
    E.Loci.forEachLocation(loci, loc => {
      if (!Unit.isAtomic(loc.unit)) return;
      const r = residue(loc), key = `${r.model_index}:${r.operator}:${r.chain}:${r.residue_number}:${r.insertion_code}`;
      if (result.has(key)) return;
      if (result.size >= limit) { truncated = true; return; }
      result.set(key, r);
    });
    return { values: [...result.values()], truncated };
  };
  function state(): ViewState {
    try {
      const { structure } = current();
      if (inventoryStructure !== structure) {
        const map = new Map<string, NonNullable<ViewState['chains']>[number]>();
        E.Loci.forEachLocation(E.Loci.all(structure), loc => {
          if (!Unit.isAtomic(loc.unit)) return;
          const r = residue(loc), key = `${r.model_index}:${r.chain}`, old = map.get(key);
          if (old) { old.first = Math.min(old.first, r.residue_number); old.last = Math.max(old.last, r.residue_number); }
          else if (map.size < 200) map.set(key, { model_index: r.model_index, chain: r.chain, first: r.residue_number, last: r.residue_number });
        });
        chains = [...map.values()]; inventoryStructure = structure;
      }
      const loci = plugin.managers.structure.selection.getLoci(structure);
      const selected = E.Loci.is(loci) ? residues(loci) : { values: [], truncated: false };
      const camera = plugin.canvas3d!.camera.getSnapshot();
      return { ready: true, path, sha256, atoms: structure.elementCount, chains,
        measurements: measurements.filter(m => plugin.state.data.cells.has(m.id)),
        selection: selected.values, selection_truncated: selected.truncated,
        selected_atoms: E.Loci.is(loci) ? E.Loci.size(loci) : 0,
        camera: { position: [...camera.position], target: [...camera.target], up: [...camera.up], radius: camera.radius } };
    } catch (error) { return { ready: false, error: String(error) }; }
  }
  function resolve(selector: StructureSelector): StructureElement.Loci {
    const { structure } = current();
    let loci: StructureElement.Loci;
    if (selector.target === 'selection') {
      const selected = plugin.managers.structure.selection.getLoci(structure);
      if (!E.Loci.is(selected)) throw new Error('当前没有选区');
      loci = selected;
    } else {
      const numbers = new Set(selector.residues || []);
      const query = Queries.generators.atoms({ atomTest: ctx => {
        const loc = ctx.element;
        if (!Unit.isAtomic(loc.unit)) return false;
        if (selector.model_index !== undefined && structure.models.indexOf(loc.unit.model) !== selector.model_index) return false;
        if (selector.chain !== undefined && P.chain.auth_asym_id(loc) !== selector.chain) return false;
        if (selector.insertion_code !== undefined && P.residue.pdbx_PDB_ins_code(loc) !== selector.insertion_code) return false;
        if (selector.target === 'residues') return numbers.has(P.residue.auth_seq_id(loc));
        if (selector.target === 'ligand') return P.entity.type(loc) === 'non-polymer' && (!selector.ligand_name || P.residue.label_comp_id(loc) === selector.ligand_name);
        return true;
      } });
      loci = StructureSelection.toLociWithSourceUnits(query(new QueryContext(structure)));
      const matches = residues(loci, 10000).values;
      if (selector.target !== 'all' && selector.model_index === undefined && new Set(matches.map(r => r.model_index)).size > 1) throw new Error('多个模型匹配，请指定 model_index');
      if (selector.target === 'residues') {
        if ([...numbers].some(n => !matches.some(r => r.residue_number === n))) throw new Error('部分残基不存在，未执行；请核对作者编号和链');
        if (selector.insertion_code === undefined && [...numbers].some(n => new Set(matches.filter(r => r.residue_number === n).map(r => r.insertion_code)).size > 1)) throw new Error('残基插入码有歧义，请指定 insertion_code');
      }
    }
    if (!E.Loci.size(loci)) throw new Error('未匹配任何原子，未改变展示');
    if (selector.radius !== undefined) loci = E.Loci.extendToWholeResidues(E.Loci.extendToRadius(loci, selector.radius));
    return loci;
  }
  const remember = () => { previous.push(plugin.canvas3d!.camera.getSnapshot()); previous = previous.slice(-10); };
  function measurementAtom(atom: MeasurementAtom) {
    const { structure } = current();
    const query = Queries.generators.atoms({ atomTest: ctx => {
      const loc = ctx.element;
      return Unit.isAtomic(loc.unit) && structure.models.indexOf(loc.unit.model) === (atom.model_index ?? 0)
        && P.chain.auth_asym_id(loc) === atom.chain && P.residue.auth_seq_id(loc) === atom.residue_number
        && P.atom.auth_atom_id(loc) === (atom.atom_name || 'CA')
        && (atom.insertion_code === undefined || P.residue.pdbx_PDB_ins_code(loc) === atom.insertion_code)
        && (atom.alt_id === undefined || P.atom.label_alt_id(loc) === atom.alt_id)
        && (atom.operator === undefined || P.unit.operator_name(loc) === atom.operator);
    } });
    const loci = StructureSelection.toLociWithSourceUnits(query(new QueryContext(structure)));
    if (E.Loci.size(loci) !== 1) throw new Error(`测距端点 ${atom.chain}:${atom.residue_number}:${atom.atom_name || 'CA'} 匹配 ${E.Loci.size(loci)} 个原子；需唯一原子，请核对编号、插入码、alt_id 或 operator`);
    const position = Vec3(); let identity: MeasurementAtom = atom;
    E.Loci.forEachLocation(loci, loc => {
      loc.unit.conformation.position(loc.element, position);
      identity = { chain: P.chain.auth_asym_id(loc), residue_number: P.residue.auth_seq_id(loc),
        atom_name: P.atom.auth_atom_id(loc), model_index: structure.models.indexOf(loc.unit.model),
        insertion_code: P.residue.pdbx_PDB_ins_code(loc), alt_id: P.atom.label_alt_id(loc), operator: P.unit.operator_name(loc) };
    });
    return { loci, position, identity };
  }
  async function run(command: ViewCommand) {
    if (command.expires_at && Date.now() >= command.expires_at) throw new Error('指令已过期');
    const { root } = current();
    if (command.expected_sha256 !== sha256) throw new Error('结构文件已变化，请重新 inspect');
    const camera = plugin.canvas3d!.camera;
    if (['focus', 'select', 'representation', 'color'].includes(command.action)) {
      if (!command.selector) throw new Error('缺少选区');
      const loci = resolve(command.selector);
      if (command.action === 'focus' || command.action === 'select') {
        plugin.managers.structure.selection.fromLoci('set', loci, false);
        if (command.action === 'focus') { remember(); plugin.managers.camera.focusLoci(loci, { durationMs: 0 }); }
      } else if (command.action === 'representation') {
        if (!command.representation) throw new Error('缺少表示方式');
        if (command.selector.target === 'all') await plugin.managers.structure.component.clear([root]);
        const component = await plugin.builders.structure.tryCreateComponentFromExpression(root.cell, E.Loci.toExpression(loci), 'tcode-agent-selection', { label: 'Agent 选区' });
        if (!component) throw new Error('无法创建选区表示');
        // Replace the previous Agent representation rather than accumulate duplicates.
        const children = plugin.state.data.tree.children.get(component.ref);
        if (children?.size) { const update = plugin.state.data.build(); children.forEach(ref => { if (ref) update.delete(ref); }); await update.commit(); }
        await plugin.builders.structure.representation.addRepresentation(component, { type: command.representation });
      } else {
        if (!/^#[0-9a-fA-F]{6}$/.test(command.color || '')) throw new Error('无效颜色');
        const selection = StructureSelection.Singletons(loci.structure, E.Loci.toStructure(loci));
        const query: StructureSelectionQuery = { label: 'Agent 选区', description: '', category: 'Agent', isHidden: true,
          priority: 0, referencesCurrent: false, expression: E.Loci.toExpression(loci), query: () => selection, getSelection: async () => selection };
        await plugin.managers.structure.component.applyTheme({ selection: query, representations: [], action: { name: 'color', params: { color: parseInt(command.color!.slice(1), 16) as Color } } }, [root]);
      }
    } else if (command.action === 'measure') {
      if (command.atoms?.length !== 2) throw new Error('测距需要两个明确的原子端点');
      measurements = measurements.filter(m => plugin.state.data.cells.has(m.id));
      const [a, b] = command.atoms.map(measurementAtom);
      if (E.Loci.areEqual(a.loci, b.loci)) throw new Error('测距端点不能是同一个原子');
      const existing = measurements.find(m => JSON.stringify(m.atoms) === JSON.stringify([a.identity, b.identity]) || JSON.stringify(m.atoms) === JSON.stringify([b.identity, a.identity]));
      if (!existing) {
        if (measurements.length >= 20) throw new Error('最多保留 20 条 Agent 测距，请先 clear_measurements');
        const distance = Vec3.distance(a.position, b.position);
        const result = await plugin.managers.structure.measurement.addDistance(a.loci, b.loci, { customText: `${distance.toFixed(3)} Å` });
        if (!result) throw new Error('Mol* 无法创建距离标注');
        measurements.push({ id: result.selection.ref, distance_angstrom: distance,
          atoms: [a.identity, b.identity], positions: [[...a.position], [...b.position]] });
      }
    } else if (command.action === 'clear_measurements') {
      const update = plugin.state.data.build();
      for (const m of measurements) if (plugin.state.data.cells.has(m.id)) update.delete(m.id);
      await update.commit(); measurements = [];
    } else if (command.action === 'clear') plugin.managers.interactivity.lociSelects.deselectAll();
    else if (command.action === 'undo') {
      const snapshot = previous.pop(); if (!snapshot) throw new Error('没有可恢复的视角');
      plugin.managers.camera.setSnapshot(snapshot, 0);
    } else if (command.action === 'reset') { remember(); plugin.managers.camera.reset(undefined, 0); }
    else if (command.action === 'rotate' || command.action === 'zoom') {
      remember(); const snapshot = camera.getSnapshot();
      const offset = Vec3.sub(Vec3(), snapshot.position, snapshot.target);
      if (command.action === 'rotate') {
        if (!command.axis || !Number.isFinite(command.degrees) || Math.abs(command.degrees!) > 360) throw new Error('无效旋转参数');
        const axis = Vec3.create(command.axis === 'x' ? 1 : 0, command.axis === 'y' ? 1 : 0, command.axis === 'z' ? 1 : 0);
        const q = Quat.setAxisAngle(Quat(), axis, command.degrees! * Math.PI / 180);
        Vec3.transformQuat(offset, offset, q); Vec3.transformQuat(snapshot.up, snapshot.up, q);
      } else {
        if (!Number.isFinite(command.factor) || command.factor! < .2 || command.factor! > 5) throw new Error('无效缩放参数');
        Vec3.scale(offset, offset, 1 / command.factor!);
      }
      Vec3.add(snapshot.position, snapshot.target, offset); plugin.managers.camera.setSnapshot(snapshot, 0);
    } else throw new Error('不支持的视图操作');
    // Yield a paint before the browser receipt is sent.
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
  }
  viewer.subscribe(plugin.managers.structure.selection.events.changed, () => window.dispatchEvent(new Event('structure-change')));
  return {
    async load(text, format, nextPath, digest) {
      sha256 = ''; sourceData = undefined; previous = []; measurements = []; inventoryStructure = undefined;
      await plugin.clear();
      await viewer.loadStructureFromData(text, (format === 'cif' ? 'mmcif' : format) as Parameters<Viewer['loadStructureFromData']>[1], { dataLabel: nextPath });
      const structures = plugin.managers.structure.hierarchy.current.structures;
      const structure = structures[0]?.cell.obj?.data;
      if (structures.length !== 1 || !structure?.elementCount) throw new Error('没有可解析的结构原子');
      path = nextPath; sha256 = digest; sourceData = structure.models[0].sourceData;
      viewer.handleResize();
    },
    state, run,
    removeResidue(target) {
      const { structure } = current();
      const query = Queries.generators.atoms({ atomTest: ctx => {
        const loc = ctx.element;
        return Unit.isAtomic(loc.unit)
          && structure.models.indexOf(loc.unit.model) === target.model_index
          && P.chain.auth_asym_id(loc) === target.chain
          && P.residue.auth_seq_id(loc) === target.residue_number
          && P.residue.pdbx_PDB_ins_code(loc) === target.insertion_code
          && P.unit.operator_name(loc) === target.operator;
      } });
      const loci = StructureSelection.toLociWithSourceUnits(query(new QueryContext(structure)));
      plugin.managers.structure.selection.fromLoci('remove', loci, false);
    },
    controls() {
      plugin.layout.setProps({ regionState: { ...plugin.layout.state.regionState, right: plugin.layout.state.regionState.right === 'hidden' ? 'full' : 'hidden' } });
      viewer.handleResize();
    },
    resize() { viewer.handleResize(); },
    save() { return { sha256, camera: plugin.canvas3d!.camera.getSnapshot(), selection: plugin.managers.structure.selection.getSnapshot() }; },
    restore(saved) {
      const value = saved as { sha256: string; camera: Camera.Snapshot; selection: ReturnType<typeof plugin.managers.structure.selection.getSnapshot> };
      if (value.sha256 !== sha256 || !value.camera || !['position', 'target', 'up'].every(key => {
        const vector = value.camera[key as 'position']; return Array.isArray(vector) && vector.length === 3 && vector.every(Number.isFinite);
      })) throw new Error('保存的视角不适用于当前结构');
      remember(); plugin.managers.camera.setSnapshot(value.camera, 0); plugin.managers.structure.selection.setSnapshot(value.selection);
    },
    dispose() { viewer.dispose(); },
  };
}
(window as ViewerWindow).structureViewer = initialize();
