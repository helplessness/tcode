// Start Vite on 5178, then feed this file to tabbit-cli nodejs.
// Isolated responses: never changes real completion records or MCP jobs.
const fixtureUrl = 'http://127.0.0.1:5178/tests/acceptance/async-notifications.html';
const events = [{id: 'notice-read', kind: 'async_completion', session_id: 'fixture-session', identifier: 'LOCAL-FIXTURE', status: 'completed', result_summary: '测试结果', read_at: ''}];
const writes = [];
let fail = false;
let gets = 0;
await page.route('**/api/async-completions**', async route => {
  if (route.request().method() === 'GET') {gets++; await route.fulfill({json: {events: events.filter(event => !event.read_at), unread_count: events.filter(event => !event.read_at).length}}); return;}
  writes.push(route.request().url());
  if (fail) {fail = false; await route.fulfill({status: 500, json: {detail: '测试保存失败'}}); return;}
  const url = route.request().url();
  if (url.endsWith('/read-all')) for (const event of events) event.read_at = new Date().toISOString();
  else events[0].read_at = new Date().toISOString();
  await route.fulfill({json: {status: 'ok', changed: 1}});
});
await page.goto(fixtureUrl, {waitUntil: 'domcontentloaded'});
const original = await page.evaluate(() => localStorage.getItem('tcode.settings.inAppNotify'));
try {
  await page.evaluate(() => {localStorage.setItem('tcode.settings.inAppNotify', '1'); window.dispatchEvent(new Event('tcode:settings-changed'));});
  const notices = page.locator('.tcode-result-notice');
  await expect(notices).toHaveCount(1);
  await page.getByRole('button', {name: '稍后处理'}).click();
  await expect(notices).toHaveCount(0);
  assert(writes.length === 0);
  await page.reload({waitUntil: 'domcontentloaded'});
  await expect(notices).toHaveCount(1);
  fail = true;
  await page.getByRole('button', {name: '已读', exact: true}).click();
  await expect(page.getByRole('alert')).toContainText('标记已读失败');
  await expect(notices).toHaveCount(1);
  await page.getByRole('button', {name: '已读', exact: true}).click();
  await expect(notices).toHaveCount(0);
  const beforeReload = gets;
  await page.reload({waitUntil: 'domcontentloaded'});
  await expect.poll(() => gets).toBeGreaterThan(beforeReload);
  await expect(notices).toHaveCount(0);
  assert(writes.length === 2 && writes.every(url => url.endsWith('/notice-read/read')));
  assert(events[0].read_at);
  for (let i = 0; i < 5; i++) events.push({...events[0], id: `bulk-${i}`, read_at: ''});
  await page.reload({waitUntil: 'domcontentloaded'});
  await expect(notices).toHaveCount(3);
  await expect(page.getByText('异步任务通知 · 5 条未读')).toBeVisible();
  await page.getByRole('button', {name: '稍后处理'}).first().click();
  fail = true;
  await page.getByRole('button', {name: '全部已读', exact: true}).click();
  await expect(page.getByRole('alert')).toContainText('全部标记已读失败');
  await expect(notices).toHaveCount(3);
  await page.getByRole('button', {name: '全部已读', exact: true}).click();
  await expect(notices).toHaveCount(0);
  const beforeBulkReload = gets;
  await page.reload({waitUntil: 'domcontentloaded'});
  await expect.poll(() => gets).toBeGreaterThan(beforeBulkReload);
  await expect(notices).toHaveCount(0);
  assert(events.every(event => event.read_at) && events.length === 6);
  assert(writes.filter(url => url.endsWith('/read-all')).length === 2);
  events.push({...events[0], id: 'new-after-bulk', read_at: ''});
  await page.reload({waitUntil: 'domcontentloaded'});
  await expect(notices).toHaveCount(1);
  return {laterReappears: true, readPersistsAcrossReload: true, saveFailureCanRetry: true, readAllIncludesHiddenAndDeferred: true, newEventsStillNotify: true, resultsPreserved: events.length === 7};
} finally {
  await page.evaluate(original => {if (original === null) localStorage.removeItem('tcode.settings.inAppNotify'); else localStorage.setItem('tcode.settings.inAppNotify', original);}, original);
}
