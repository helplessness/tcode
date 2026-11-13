// Run with tabbit-cli nodejs --task question-card-ui < this-file.
// UI-only fixtures: intercepted status/answer requests never modify backend runs.
await page.setViewportSize({ width: 1440, height: 1000 });
await page.goto('http://127.0.0.1:5556/', { waitUntil: 'domcontentloaded' });
await expect(page.getByPlaceholder('下达新指令，输入 @ 选择 Workspace 文件，或输入 / 选择插件命令...')).toBeVisible();
const sessionId = '88e7b6b2-4888-48b9-9f0e-6b165b24e406';
const statusUrl = `http://127.0.0.1:5556/api/chat/status/${sessionId}`;
const live = await (await page.request.get(statusUrl)).json();
if (live.running) throw new Error('Refusing to replace an active session status with fixtures');
let fixture = null;
let failNext = false;
const answers = [];
const results = [];
const originalViewport = page.viewportSize();
const statusHandler = route => route.fulfill({ json: { ...live, running: Boolean(fixture), question_request: fixture } });
const answerHandler = async route => {
  const payload = route.request().postDataJSON();
  if (failNext) { failNext = false; await route.fulfill({ status: 500, json: { detail: '测试提交失败，请重试' } }); return; }
  answers.push(payload);
  fixture = null;
  await route.fulfill({ json: { status: 'ok' } });
};
await page.route(statusUrl, statusHandler);
await page.route('**/api/chat/question', answerHandler);
const longText = '这是界面回归测试的长说明，需要完整保留但不能撑大卡片。'.repeat(70);
const cases = [
  { name: 'single', question: longText, type: 'single', allow_custom: false, options: Array.from({ length: 8 }, (_, i) => `选项${i + 1}：${'完整选项说明'.repeat(25)}`) },
  { name: 'multiple', question: longText, type: 'multiple', allow_custom: false, options: ['包含逗号,的选项', '第二项', '第三项', '第四项', '第五项', '第六项', '第七项'] },
  { name: 'custom', question: longText, type: 'custom', allow_custom: true, options: [] },
  { name: 'workflow', question: longText, type: 'single', allow_custom: false, options: ['继续运行', '返回普通对话'], metadata: { kind: 'workflow_applicability_confirmation', workflow_applicability: { verdict: 'uncertain', reason: longText, workflow_summary: '测试用途' } } },
];
try {
  for (const item of cases) {
    fixture = { session_id: sessionId, request_id: `ui-fixed-${item.name}`, question: item };
    await page.reload({ waitUntil: 'domcontentloaded' });
    const card = page.getByRole('region', { name: '待回答问题' });
    await expect(card).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-chat-timeline] [data-question-card]')).toHaveCount(1);
    const compact = await card.boundingBox();
    if (Math.abs(compact.height - 360) > 1) throw new Error(`${item.name}: compact height ${compact.height}`);
    await card.getByRole('button', { name: '展开详情', exact: true }).click();
    const expanded = await card.boundingBox();
    if (Math.abs(expanded.height - compact.height) > 1) throw new Error('Expansion resized the card');
    await expect(card.locator('[data-question-body]')).toHaveCSS('overflow-y', 'auto');
    if (item.name === 'single') {
      await card.getByRole('button', { name: item.options[7], exact: true }).click();
      await card.getByRole('button', { name: '收起详情', exact: true }).click();
      await card.getByRole('button', { name: '提交答案', exact: true }).click();
    } else if (item.name === 'multiple') {
      await card.getByRole('button', { name: item.options[0], exact: true }).click();
      await card.getByRole('button', { name: item.options[6], exact: true }).click();
      await card.getByRole('button', { name: '提交答案', exact: true }).click();
    } else if (item.name === 'custom') {
      const hideSidebar = page.getByTitle('隐藏侧栏').first();
      if (await hideSidebar.count()) await hideSidebar.click();
      const hideDetails = page.getByRole('button', { name: '隐藏运行详情', exact: true });
      if (await hideDetails.count()) await hideDetails.click();
      await page.setViewportSize({ width: 390, height: 844 });
      const narrow = await card.boundingBox();
      if (narrow.width > 390 || Math.abs(narrow.height - 360) > 1) throw new Error('Narrow layout overflow');
      await card.getByRole('textbox', { name: '你的回答' }).fill('这是自由输入的回答');
      failNext = true;
      await card.getByRole('button', { name: '提交答案', exact: true }).click();
      await expect(card.getByRole('alert')).toContainText('测试提交失败');
      await card.getByRole('button', { name: '提交答案', exact: true }).click();
      if (originalViewport) await page.setViewportSize(originalViewport);
    } else {
      await expect(card.getByRole('button', { name: '提交答案', exact: true })).toHaveCount(0);
      await card.getByRole('button', { name: '返回普通对话', exact: true }).click();
    }
    await expect(card).toHaveCount(0);
    results.push({ type: item.name, compactHeight: compact.height, expandedHeight: expanded.height });
  }
  if (answers[0].answer !== cases[0].options[7] || answers[1].answer !== '1, 7' || answers[2].answer !== '这是自由输入的回答' || answers[3].answer !== '返回普通对话') throw new Error('Answer payload mismatch');
  // Capture a compact, long generic Question for visual verification.
  fixture = { session_id: sessionId, request_id: 'ui-fixed-preview', question: cases[1] };
  await page.reload({ waitUntil: 'domcontentloaded' });
  const preview = page.getByRole('region', { name: '待回答问题' });
  await expect(preview).toBeVisible({ timeout: 15000 });
  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/tmp/tcode-question-fixed.png' });
} finally {
  await page.unroute(statusUrl, statusHandler);
  await page.unroute('**/api/chat/question', answerHandler);
  if (originalViewport) await page.setViewportSize(originalViewport);
  await page.reload({ waitUntil: 'domcontentloaded' });
}
return { fixturesOnly: true, results, successfulAnswers: answers.length, backendMutations: 0 };
