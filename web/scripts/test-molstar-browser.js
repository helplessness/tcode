// Run in an authenticated Tabbit task on the local app (no chat/scientific jobs):
// tabbit-cli nodejs --task "Molstar 联调" --request-id molstar-smoke < web/scripts/test-molstar-browser.js
const pdb = 'ATOM      1  N   ALA A  42       0.000   0.000   0.000  1.00 20.00           N\nATOM      2  CA  ALA A  42       1.458   0.000   0.000  1.00 20.00           C\nATOM      3  C   ALA A  42       2.000   1.400   0.000  1.00 20.00           C\nATOM      4  O   ALA A  42       1.500   2.400   0.000  1.00 20.00           O\nEND\n';
const bridge = { command: null, receipts: [], state: null };
let seq = 0;
await page.route('**/api/structure-view/**', async route => {
  if (route.request().method() === 'POST') {
    const body = route.request().postDataJSON(); bridge.state = body.state;
    if (body.receipt) {
      bridge.receipts.push({ ...body.receipt, state: body.state });
      if (bridge.command?.id === body.receipt.command_id) bridge.command = null;
    }
  }
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ command: bridge.command }) });
});
await page.route('**/api/files/inline?**', async route => {
  if (new URL(route.request().url()).searchParams.get('path') !== '__molstar_test__.pdb') return route.continue();
  await route.fulfill({ status: 200, contentType: 'text/plain', body: pdb });
});
async function command(action, extra = {}, ok = true) {
  const id = `smoke-${++seq}`;
  bridge.command = { id, action, expected_sha256: bridge.state?.sha256, ...extra, expires_at: Date.now() + 20000 };
  await expect.poll(() => bridge.receipts.find(r => r.command_id === id), { timeout: 15000 }).toBeTruthy();
  const receipt = bridge.receipts.find(r => r.command_id === id);
  assert.equal(receipt.ok, ok, receipt.error);
  return receipt.state;
}
try {
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(() => bridge.state, { timeout: 15000 }).toBeTruthy();
  assert.equal(await page.locator('iframe[title="Mol* 完整结构查看器"]').count(), 0, 'Must remain lazy');
  await command('inspect'); // No canvas needed; fresh browser state returned.
  assert.equal(bridge.state.ready, false);
  await command('open', { path: '__molstar_test__.pdb' }); // Reveals the closed pane.
  assert.equal(bridge.state.atoms, 4);
  const frame = page.frames().find(f => f.url().endsWith('/molstar.html')); assert(frame);
  const heightHandle = page.getByRole('separator', { name: '调整三维区域高度' });
  await heightHandle.press('Home');
  await expect(heightHandle).toHaveAttribute('aria-valuenow', '320');
  await heightHandle.press('ArrowDown');
  await expect(heightHandle).toHaveAttribute('aria-valuenow', '360');
  await heightHandle.dblclick();
  await expect(heightHandle).toHaveAttribute('aria-valuenow', '480');
  const atoms = [{ chain: 'A', residue_number: 42, atom_name: 'N' }, { chain: 'A', residue_number: 42, atom_name: 'CA' }];
  const measured = await command('measure', { atoms });
  // Mol* stores coordinates as Float32; tolerate sub-micro-Angstrom conversion error.
  assert(Math.abs(measured.measurements[0].distance_angstrom - 1.458) < 1e-6, JSON.stringify(measured.measurements));
  await command('measure', { atoms }); assert.equal(bridge.state.measurements.length, 1, 'Repeated measure must not duplicate');
  await command('measure', { atoms: [atoms[0], { ...atoms[1], residue_number: 999 }] }, false);
  assert.equal(bridge.state.measurements.length, 1, 'Invalid endpoint must preserve prior measurements');
  await command('measure', { atoms: [atoms[0], atoms[0]] }, false);
  await command('clear_measurements'); assert.equal(bridge.state.measurements.length, 0);
  await page.getByRole('button', { name: 'Structure Tools', exact: true }).click();
  await expect(frame.getByRole('button', { name: 'Measurements', exact: true })).toBeVisible();
  await frame.getByTitle('Toggle Selection Mode', { exact: true }).click();
  await frame.locator('.msp-sequence [data-seqid="0"]').click();
  await expect(page.getByText('选区与 Agent · 4 个原子', { exact: true })).toBeVisible();
  const selected = await command('inspect');
  assert.equal(selected.selection[0].residue_number, 42);
  const focused = await command('focus', { selector: { target: 'residues', chain: 'A', residues: [42] } });
  assert.equal(focused.selected_atoms, 4);
  const rotated = await command('rotate', { axis: 'y', degrees: 90 });
  assert.notEqual(JSON.stringify(rotated.camera.position), JSON.stringify(focused.camera.position));
  const zoomed = await command('zoom', { factor: 2 });
  const distance = c => Math.hypot(...c.position.map((x, i) => x - c.target[i]));
  assert(Math.abs(distance(zoomed.camera) / distance(rotated.camera) - .5) < .01);
  await command('color', { selector: { target: 'selection' }, color: '#cc3344' });
  await command('representation', { selector: { target: 'all' }, representation: 'ball-and-stick' });
  await command('focus', { selector: { target: 'selection', radius: 4 } });
  await command('focus', { selector: { target: 'residues', chain: 'A', residues: [999] } }, false);
  await command('rotate', { axis: 'x', degrees: 10, expected_sha256: 'b'.repeat(64) }, false);
  await command('clear'); assert.equal(bridge.state.selected_atoms, 0);
  await command('reset'); await command('undo');
  const draft = page.getByPlaceholder('下达新指令，输入 @ 选择 Workspace 文件，或输入 / 选择插件命令...');
  const original = await draft.inputValue();
  try {
    await page.getByRole('button', { name: '带着选区向 Agent 提问', exact: true }).click();
    assert((await draft.inputValue()).includes('tcode.structure-context.v2'));
  } finally { await draft.fill(original); }
  return { passed: true, operations: bridge.receipts.map(r => ({ id: r.command_id, ok: r.ok, error: r.error })) };
} finally {
  await page.unroute('**/api/structure-view/**');
  await page.unroute('**/api/files/inline?**');
  await page.reload({ waitUntil: 'domcontentloaded' });
}
