import { describe, expect, it } from 'vitest';
import { deriveTaskLifecyclePhase } from './taskLifecycle';

const session = (outcome = '', messages: any[] = []) => ({
  messages,
  task_runtime: outcome
    ? { session_id: 's1', session_outcome_status: outcome, active_total: 0, counts: {}, active_counts: {}, updated_at: 1 }
    : undefined,
});

describe('deriveTaskLifecyclePhase', () => {
  it('prioritizes live user and execution signals over stale outcomes', () => {
    expect(deriveTaskLifecyclePhase({ session: session('completed'), hasQuestion: true })).toBe('clarifying');
    expect(deriveTaskLifecyclePhase({ session: session('completed'), hasPermissionRequest: true })).toBe('awaiting_authorization');
    expect(deriveTaskLifecyclePhase({ session: session('completed'), isRunning: true })).toBe('executing');
  });
  it('keeps accepted delivery complete without hiding or cancelling background work', () => {
    const accepted = session('partial', [{id:'answer',role:'assistant',verification:{advisory:true,delivery_state:'accepted'}},
      {role:'assistant',presentation:'async_completion',content:'后台进度'}]);
    accepted.task_runtime!.active_total = 1;
    expect(deriveTaskLifecyclePhase({session:{...accepted,task_runtime:{...accepted.task_runtime,active_counts:{waiting_external:1}}} as any})).toBe('completed');
    expect(deriveTaskLifecyclePhase({session:accepted,isRunning:true})).toBe('executing');
  });

  it('maps review, revision and terminal outcomes', () => {
    expect(deriveTaskLifecyclePhase({ session: session('partial') })).toBe('awaiting_review');
    expect(deriveTaskLifecyclePhase({ session: session('failed') })).toBe('failed');
    expect(deriveTaskLifecyclePhase({ session: session('completed') })).toBe('completed');
    expect(deriveTaskLifecyclePhase({ session: session('blocked') })).toBe('blocked');
    expect(deriveTaskLifecyclePhase({ session: session('', [{ role: 'assistant', verification: { verification_status: 'PARTIAL', confidence: .5, confirmation_state: 'modification_requested' } }]) })).toBe('revision_requested');
  });

  it('treats explicit acceptance as complete even when the cached outcome is partial', () => {
    expect(deriveTaskLifecyclePhase({ session: session('partial', [{ role: 'assistant', verification: { verification_status: 'VERIFIED', confidence: .95, confirmation_state: 'accepted', user_confirmed: true } }]) })).toBe('completed');
  });
  it('accepts advisory delivery without claiming verification passed and preserves live gates', () => {
    const accepted = session('partial', [{role:'assistant', verification:{advisory:true, delivery_state:'accepted', verification_status:'PARTIAL', confidence:.2}}]);
    expect(deriveTaskLifecyclePhase({session:accepted})).toBe('completed');
    expect(deriveTaskLifecyclePhase({session:accepted, hasPermissionRequest:true})).toBe('awaiting_authorization');
    expect(deriveTaskLifecyclePhase({session:{...accepted, task_runtime:{...accepted.task_runtime, active_counts:{waiting_review:1}}} as any})).toBe('awaiting_review');
    expect(deriveTaskLifecyclePhase({session:session('partial', [...accepted.messages, {role:'user',content:'新要求'}])})).not.toBe('completed');
  });
});
