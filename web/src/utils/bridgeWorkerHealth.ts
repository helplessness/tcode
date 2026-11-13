type BridgeTaskCard = {
  source_type?: string;
  status?: string;
  execution_channel?: string;
  claim_owner?: string;
  created_at?: string;
};

export const bridgeWorkerWarning = (
  card?: BridgeTaskCard | null,
  nowMs = Date.now(),
): string => {
  if (!card || String(card.source_type || '') !== 'async') return '';
  if (String(card.status || '').toLowerCase() !== 'todo') return '';
  if (String(card.execution_channel || '').toLowerCase() !== 'bridge') return '';
  if (String(card.claim_owner || '').trim()) return '';

  const createdAt = new Date(String(card.created_at || '')).getTime();
  if (!Number.isFinite(createdAt) || nowMs - createdAt < 30_000) return '';
  return '任务已排队超过 30 秒但尚未被 bridge worker 认领。请确认服务是通过 scripts/start_web.sh 启动的。';
};
