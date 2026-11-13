import { expect, it } from 'vitest';
import { nearUploadTarget } from './uploadFeedback';
it('opens near the upload target without catching distant drags', () => {
  const rect = { left: 100, right: 300, top: 200, bottom: 400 };
  expect(nearUploadTarget(rect, 80, 250)).toBe(true);
  expect(nearUploadTarget(rect, 200, 432)).toBe(true);
  expect(nearUploadTarget(rect, 200, 433)).toBe(false);
  expect(nearUploadTarget(rect, 20, 250)).toBe(false);
});
