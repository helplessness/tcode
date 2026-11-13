const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { inputEvidence } = require('./input-evidence.cjs');

test('opt-in test evidence pins one task/app, bounds private images and omits text/AX/secrets', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-test-evidence-'));
  const record = inputEvidence({ directory, bundleId: 'fixture.app', driver: { call: async () => ({ apps: [{ active: true, pid: 99 }] }) } });
  const png = Buffer.alloc(24); png.writeUInt32BE(100, 16); png.writeUInt32BE(100, 20);
  const event = { operation: 'observe', payload: { app: { bundle_id: 'fixture.app', pid: 42 }, task_id: 'task', text: 'SECRET-BODY' },
    result: { _image: { data: png.toString('base64') }, window_bounds: { x: 0, y: 0, width: 50, height: 50 },
      tree_markdown: 'SECRET-AX', secret: 'SECRET-TOKEN', window_title: 'SECRET-TITLE' } };
  await record({ ...event, payload: { ...event.payload, app: { bundle_id: 'unrelated.app' } } });
  assert.equal(fs.readdirSync(directory).length, 0);
  for (let i = 0; i < 15; i++) await record(event);
  await record({ ...event, payload: { ...event.payload, task_id: 'other-task' } });
  await record({ operation: 'press_key', stage: 'before', payload: { ...event.payload, key: 'return', x: 20, y: 20 },
    frontmostPid: 88, capture: { delivered_width: 100, delivered_height: 100 } });
  const lines = fs.readFileSync(path.join(directory, 'evidence.jsonl'), 'utf8');
  assert.doesNotMatch(lines, /SECRET|unrelated|other-task/);
  const rows = lines.trim().split('\n').map(JSON.parse);
  assert.equal(rows.length, 16); assert.equal(rows.at(-1).frontmost_pid, 88);
  assert.equal(rows.at(-1).coordinates.derived_screen_point.x, 10);
  assert.equal(fs.readdirSync(directory).filter(x => x.endsWith('.png')).length, 12);
  assert.equal(fs.statSync(directory).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(directory, '01.png')).mode & 0o777, 0o600);
});
