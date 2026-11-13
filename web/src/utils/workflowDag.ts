export type WorkflowDagStage = {
  stage_id: string;
  label?: string;
  owner?: string;
  depends_on?: string[];
  review_gate?: boolean;
  status?: string;
  description?: string;
  input_contract?: Record<string, unknown>;
  output_contract?: Record<string, unknown>;
  expected_artifacts?: string[];
  success_checks?: string[];
  retry_policy?: Record<string, unknown>;
  timeout_policy?: Record<string, unknown>;
};

export type WorkflowDagNode = WorkflowDagStage & { layer: number; x: number; y: number };

export function layoutWorkflowDag(stages: WorkflowDagStage[]) {
  const known = new Set(stages.map(stage => stage.stage_id));
  const layerById = new Map<string, number>();
  const visit = (stage: WorkflowDagStage, trail = new Set<string>()): number => {
    if (layerById.has(stage.stage_id)) return layerById.get(stage.stage_id)!;
    if (trail.has(stage.stage_id)) return 0;
    const nextTrail = new Set(trail).add(stage.stage_id);
    const dependencies = (stage.depends_on || []).filter(id => known.has(id));
    const layer = dependencies.length
      ? 1 + Math.max(...dependencies.map(id => visit(stages.find(item => item.stage_id === id)!, nextTrail)))
      : 0;
    layerById.set(stage.stage_id, layer);
    return layer;
  };
  stages.forEach(stage => visit(stage));
  const rows = new Map<number, number>();
  const nodes: WorkflowDagNode[] = stages.map(stage => {
    const layer = layerById.get(stage.stage_id) || 0;
    const row = rows.get(layer) || 0;
    rows.set(layer, row + 1);
    return { ...stage, layer, x: 34 + layer * 284, y: 38 + row * 138 };
  });
  const edges = stages.flatMap(stage => (stage.depends_on || [])
    .filter(from => known.has(from))
    .map(from => ({ from, to: stage.stage_id })));
  return {
    nodes,
    edges,
    width: Math.max(280, 68 + ((Math.max(0, ...nodes.map(node => node.layer)) + 1) * 284)),
    height: Math.max(156, 76 + Math.max(1, ...rows.values()) * 138),
  };
}
