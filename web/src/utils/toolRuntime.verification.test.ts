import { expect, it } from 'vitest';
import { getToolVerificationLabel } from './toolRuntime';

it('keeps pending effects distinct from failures and unknown delivery in live and restored tools', () => {
  const live = { metadata: { computer_verification_pending: true }, error: null };
  expect(getToolVerificationLabel(live)).toBe('已发送');
  expect(getToolVerificationLabel(JSON.parse(JSON.stringify(live)))).toBe('已发送');
  expect(getToolVerificationLabel({ metadata: { computer: { action_receipt: { verification: 'pending' } } } })).toBe('已发送');
  expect(getToolVerificationLabel({ ...live, error: 'delivery_failed' })).toBeNull();
  expect(getToolVerificationLabel({ metadata: { computer_audit: { status: 'outcome_unknown' }, computer_verification_pending: true } })).toBeNull();
  expect(getToolVerificationLabel({ metadata: { computer: { action_receipt: { verification: 'native_confirmed' } } } })).toBeNull();
});

it('renders only trusted legacy effect receipts as pending without rewriting history', () => {
  const old = { error: 'effect_unverifiable', metadata: {
    known_action_receipt: true, computer_audit: { status: 'failed_known', error_code: 'effect_unverifiable' },
  } };
  expect(getToolVerificationLabel(old)).toBe('已发送');
  expect(old.error).toBe('effect_unverifiable');
  expect(getToolVerificationLabel({ ...old, metadata: { computer_audit: old.metadata.computer_audit } })).toBeNull();
  expect(getToolVerificationLabel({ ...old, metadata: { ...old.metadata, computer_audit: { status: 'failed_known', error_code: 'delivery_failed' } } })).toBeNull();
});
