import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

import ComputerUsePanel, { ExactComputerAction, computerNavigationDefault, computerPanelApprovalKey, computerPreviewContext, computerPreviewNotice, computerPanelDisclosureTransition, computerPanelOpenForState, validComputerPanelRequest } from './ComputerUsePanel';

describe('ComputerUsePanel accessibility contract', () => {
  it('binds approval choices and preview context to revisions and control generations', () => {
    const state = {available:true,enabled:true,status:'paused',control_epoch:1,driver_generation:1,
      pending_approval:{approval_id:'a',revision:1,task_id:'t',operation:'observe',delivery_mode:'background',parameter_digest:'d'}};
    expect(computerPanelApprovalKey(state)).not.toBe(computerPanelApprovalKey({...state,pending_approval:{...state.pending_approval,revision:2}}));
    expect(computerPreviewContext(state)).not.toBe(computerPreviewContext({...state,control_epoch:2}));
    expect(computerPreviewNotice(undefined, 100000)).toContain('采集时间未知');
    expect(computerPreviewNotice(80000, 100000)).toContain('较早的观察');
    expect(computerPreviewNotice(99000, 100000)).toContain('不是实时画面');
    expect(computerPreviewNotice(99000, 100000)).toContain('不是任务完成证明');
  });
  it('defaults navigation only for a new task proposal and never resets explicit choices on status polling', () => {
    expect(computerNavigationDefault({ approval_kind: 'task', navigation_proposal: true })).toBe(true);
    expect(computerNavigationDefault({ approval_kind: 'action', navigation_proposal: true })).toBe(false);
    expect(computerNavigationDefault({ approval_kind: 'task' })).toBe(false);
    const source = readFileSync(new URL('./ComputerUsePanel.tsx', import.meta.url), 'utf8');
    expect(source).toContain('createEffect(on(approvalId,');
  });
  it('bounds long descriptions while keeping approval controls outside the scroller', () => {
    const css = readFileSync(new URL('../studio.css', import.meta.url), 'utf8');
    expect(css).toContain('max-height:min(640px,calc(100vh - 100px))');
    expect(css).toContain('.computer-panel-scroll{min-height:0;overflow-y:auto;overscroll-behavior:contain;overflow-wrap:anywhere}');
    const source = readFileSync(new URL('./ComputerUsePanel.tsx', import.meta.url), 'utf8');
    expect(source).toMatch(/<\/div>\s*<div class="computer-controls">/);
    expect(source).toMatch(/class="computer-controls">[\s\S]*?approve\(\)[\s\S]*?reject\(\)[\s\S]*?emergency_stop/);
  });
  it('renders full exact local action details as escaped, wrapping text', () => {
    const text = '控件：Send\n精确文本：<secret>&' + '长文本'.repeat(2000) + '\n按键：cmd + return\n坐标：(0.5, 0.2) normalized；截图 1040 × 904';
    const html = renderToString(() => <ExactComputerAction text={text} />);
    expect(html).toContain('aria-label="本次精确操作"');
    expect(html).toContain('&lt;secret>');
    expect(html).toContain('长文本'.repeat(2000));
    expect(html).toContain('1040 × 904');
    expect(html).not.toContain('<secret>');
  });
  it('uses a focusable controlled disclosure instead of native details/summary', () => {
    const html = renderToString(() => <ComputerUsePanel />);

    expect(html).toContain('class="computer-use-trigger"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('aria-controls="computer-use-controls"');
    expect(html).toContain('aria-keyshortcuts="Control+Alt+C"');
    expect(html).not.toContain('<details');
    expect(html).not.toContain('<summary');
  });

  it('opens, dismisses outside, and requests trigger focus on Escape', () => {
    expect(computerPanelDisclosureTransition(false, 'toggle')).toEqual({ open: true, focusTrigger: false });
    expect(computerPanelDisclosureTransition(true, 'outside', true)).toEqual({ open: true, focusTrigger: false });
    expect(computerPanelDisclosureTransition(true, 'outside', false)).toEqual({ open: false, focusTrigger: false });
    expect(computerPanelDisclosureTransition(true, 'escape')).toEqual({ open: false, focusTrigger: true });
    expect(computerPanelDisclosureTransition(false, 'escape')).toEqual({ open: false, focusTrigger: false });
  });

  it('accepts only a valid monotonic native open request shape', () => {
    expect(validComputerPanelRequest({ request_id: 3 })).toBe(3);
    expect(validComputerPanelRequest({ request_id: null })).toBeNull();
    expect(validComputerPanelRequest({ request_id: -1 })).toBeNull();
    expect(validComputerPanelRequest({ request_id: '3' })).toBeNull();
  });

  it('opens from a pulled pending snapshot even when the native event was missed', () => {
    expect(computerPanelOpenForState(false, {
      available: true,
      enabled: true,
      status: 'waiting_for_approval',
      pending_approval: {
        approval_id: 'approval-1', revision: 4, task_id: 'task-1', operation: 'observe',
        delivery_mode: 'foreground', parameter_digest: 'digest-1',
      },
    })).toBe(true);
    expect(computerPanelOpenForState(false, { available: true, status: 'paused', pending_approval: null })).toBe(false);
  });

  it('closes automatic permission guidance after verified grants but preserves manual panels and task approvals', () => {
    const ready = { available: true, enabled: true, status: 'paused', permissions: {
      status: 'known', accessibility: true, screen_recording: true, identity_verified: true,
      attribution: 'host', host_bundle_id: 'com.tcode.desktop',
    } };
    expect(computerPanelOpenForState(true, ready, true)).toBe(false);
    expect(computerPanelOpenForState(true, ready, false)).toBe(true);
    expect(computerPanelOpenForState(true, { ...ready, permissions: { ...ready.permissions, status: 'error' } }, true)).toBe(true);
    expect(computerPanelOpenForState(true, { ...ready, permissions: { ...ready.permissions, accessibility: false } }, true)).toBe(true);
    expect(computerPanelOpenForState(true, { ...ready, pending_approval: {
      approval_id: 'a', task_id: 'task', operation: 'observe', delivery_mode: 'foreground', parameter_digest: 'd',
    } }, true)).toBe(true);
  });
  it('keeps a dismissed approval closed across polling but opens a new revision', () => {
    const state = { available: true, status: 'waiting_for_approval', pending_approval: {
      approval_id: 'a', revision: 1, task_id: 't', operation: 'observe', delivery_mode: 'foreground', parameter_digest: 'd',
    } };
    expect(computerPanelOpenForState(false, state, false, 'a:1')).toBe(false);
    expect(computerPanelOpenForState(false, { ...state, pending_approval: { ...state.pending_approval, revision: 2 } }, false, 'a:1')).toBe(true);
    expect(computerPanelDisclosureTransition(true, 'blur')).toEqual({ open: false, focusTrigger: false });
  });

});
