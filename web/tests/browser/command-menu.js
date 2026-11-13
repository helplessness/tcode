// Start Vite on port 5178, then run via tabbit-cli nodejs with this file on stdin.
// Production component, isolated fixture: no backend writes or model calls.
await page.goto('http://127.0.0.1:5178/tests/acceptance/command-menu.html', {waitUntil: 'domcontentloaded'});
await page.emulateMedia({reducedMotion: 'reduce'});
const results = [];
for (const theme of ['light', 'dark']) {
  await page.evaluate(value => {
    document.documentElement.classList.toggle('studio-theme', value === 'light');
    document.documentElement.dataset.desktop = String(value === 'light');
  }, theme);
  for (const width of [1280, 375]) {
    await page.setViewportSize({width, height: 812});
    const metrics = await page.getByTestId('slash-command-menu').evaluate(menu => {
      const option = menu.querySelector('[role="option"][aria-selected="true"]');
      const label = getComputedStyle(option.querySelector('.command-menu-label'));
      const description = getComputedStyle(option.querySelector('.command-menu-description'));
      const surface = getComputedStyle(menu);
      const selected = getComputedStyle(option);
      const luminance = rgb => {
        const channels = rgb.match(/[\d.]+/g).slice(0,3).map(Number).map(value => {
          value /= 255;
          return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
        });
        return channels[0]*.2126 + channels[1]*.7152 + channels[2]*.0722;
      };
      const contrast = (a,b) => {
        const x = luminance(a), y = luminance(b);
        return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);
      };
      const bounds = menu.getBoundingClientRect();
      return {labelColor: label.color, background: surface.backgroundColor,
        textContrast: contrast(label.color, surface.backgroundColor),
        descriptionContrast: contrast(description.color, selected.backgroundColor),
        selectedBorderContrast: contrast(selected.borderColor, selected.backgroundColor),
        labelPx: parseFloat(label.fontSize), width: bounds.width, height: bounds.height,
        fits: bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0};
    });
    assert(metrics.textContrast >= 4.5 && metrics.descriptionContrast >= 4.5);
    assert(metrics.selectedBorderContrast >= 3 && metrics.fits && metrics.width <= 480);
    assert(metrics.height < 400 && metrics.labelPx >= 15);
    assert(metrics.labelColor === 'rgb(0, 0, 0)' && metrics.background === 'rgb(255, 255, 255)');
    results.push({theme, viewport: width, ...metrics});
  }
}
await page.evaluate(() => {
  document.documentElement.classList.add('studio-theme');
  document.documentElement.dataset.desktop = 'true';
});
await page.getByRole('textbox', {name: '指令输入'}).press('ArrowDown');
await expect(page.getByRole('option').nth(1)).toHaveAttribute('aria-selected', 'true');
await page.getByRole('textbox', {name: '指令输入'}).press('Escape');
await page.getByRole('button', {name: '选择 Skill', exact: true}).click();
await page.getByRole('textbox', {name: '搜索选择 Skill'}).fill('multi-model');
await expect(page.getByRole('option')).toHaveCount(1);
await page.getByRole('textbox', {name: '搜索选择 Skill'}).press('Enter');
await expect(page.getByTestId('selected-skill')).toContainText('multi-model');
await page.getByRole('button', {name: '移除技能'}).click();
await expect(page.getByTestId('selected-skill')).toHaveCount(0);
return {results, keyboardAndSkillSelection: true};
