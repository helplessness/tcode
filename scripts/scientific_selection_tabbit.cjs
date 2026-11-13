// Run through tabbit-cli nodejs; page/context/assert/expect are provided by Tabbit.
// Both servers are loopback fixture servers. No remote computation or live model is used.
for (const pattern of ['http://127.0.0.1:5556/acceptance/**', '**/api/mcp-apps/**']) {
  await page.route(pattern, async route => {
    const url = new URL(route.request().url());
    const response = await context.request.fetch('http://127.0.0.1:5557' + url.pathname + url.search, {
      method: route.request().method(), data: route.request().postData() || undefined,
    });
    await route.fulfill({response});
  });
}
const report = [];
for (const [tool, label, button, content] of [
  ['gnina', 'Pose 2', '选用此 Pose', 'pose-best'],
  ['boltz', 'target_model_0', '选用此结果', 'data_model_0'],
]) {
  await page.goto('http://127.0.0.1:5556/tests/acceptance/scientific-selection.html?tool=' + tool);
  const frame = page.frameLocator('iframe');
  const state = async () => (await context.request.get('http://127.0.0.1:5557/acceptance/state?tool=' + tool)).json();
  const before = await state();
  await frame.getByRole('combobox', {name:'预览结果'}).selectOption({label});
  assert.equal((await state()).message_count, before.message_count, 'preview must not write an event');
  await frame.getByRole('button', {name:button}).click();
  await page.getByRole('button', {name:'拒绝', exact:true}).click();
  await expect(frame.locator('#selection-status')).toContainText('拒绝');
  assert.equal((await state()).message_count, before.message_count, 'denial must not write an event');
  await frame.getByRole('button', {name:button}).click();
  await page.getByRole('button', {name:'允许一次', exact:true}).click();
  await expect(frame.locator('#selection-status')).toHaveText('已正式选用：' + label);
  const committed = await state();
  await frame.getByRole('button', {name:button}).click();
  await page.getByRole('button', {name:'允许一次', exact:true}).click();
  await expect(frame.locator('#selection-status')).toHaveText('已正式选用：' + label);
  assert.equal((await state()).message_count, committed.message_count, 'repeat selection must be idempotent');
  await page.reload();
  await expect(page.frameLocator('iframe').locator('#selection-status')).toHaveText('已正式选用：' + label);
  await page.frameLocator('iframe').getByRole('button', {name:'查看所选数据'}).click();
  await page.getByRole('button', {name:'允许一次', exact:true}).click();
  await expect(page.frameLocator('iframe').locator('#selected-data')).toContainText(content);
  report.push({tool, passed:['preview','deny','commit','idempotency','reload','exact artifact read']});
}
return report;
