import type { JobWorkspace } from '../api/jobs';

export function localJobResultRoot(workspace: JobWorkspace): string {
  const absolute = String(workspace.output_abs_path || '').trim();
  if (absolute.startsWith('/') && !absolute.startsWith('//')) return absolute;
  if (workspace.transfer_state === 'ready') {
    const root = String(workspace.output_root || '').trim();
    if (root && !root.includes('://')) return root;
  }
  return '';
}
