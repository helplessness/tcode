const test = require('node:test');
const assert = require('node:assert/strict');
const { createRecoveryController } = require('../recovery.cjs');

test('healthy resume does not restart the backend', async () => {
  let restarts = 0;
  const controller = createRecoveryController({
    healthCheck: async () => true,
    restart: async () => { restarts += 1; },
  });
  assert.equal(await controller.recover('system-resume'), 'healthy');
  assert.equal(restarts, 0);
});

test('unhealthy resume restarts once and coalesces concurrent checks', async () => {
  let restarts = 0;
  const controller = createRecoveryController({
    healthCheck: async () => false,
    restart: async () => { restarts += 1; },
  });
  const [first, second] = await Promise.all([
    controller.recover('system-resume'),
    controller.recover('system-resume'),
  ]);
  assert.equal(first, 'restarted');
  assert.equal(second, 'restarted');
  assert.equal(restarts, 1);
});
