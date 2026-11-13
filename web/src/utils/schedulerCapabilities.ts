export function restoredSchedulerMode(metadata?: Record<string, unknown>) {
  const mode = metadata?.scheduler_mode;
  return mode === 'chain' || mode === 'swarm' || mode === 'auto_swarm_builder' ? mode : 'supervisor';
}

export function autoSwarmAvailability(capabilities?: Record<string, unknown>) {
  const modes = capabilities?.schedulerModes as Record<string, unknown> | undefined;
  const auto = modes?.auto_swarm_builder as { available?: unknown; reason?: unknown } | undefined;
  return {
    available: auto?.available === true,
    reason: auto?.available === true ? '' : typeof auto?.reason === 'string' && auto.reason
      ? auto.reason : '尚未确认自动群体构建可用，请刷新执行引擎列表。',
  };
}
