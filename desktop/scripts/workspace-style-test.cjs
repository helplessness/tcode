// Offline visual regression. No visible window, backend, profile or real sessions.
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 760, height: 600 });
  try {
    const css = fs.readFileSync(path.join(__dirname, '../../web/src/interface.css'), 'utf8');
    const groups = [210, 280, 150, 30].map((hue, i) => `<section class="session-workspace-group" style="--workspace-hue:${hue}" ${i === 1 ? 'data-current="true"' : ''}><div class="session-workspace-heading" role="button" tabindex="0" aria-expanded="false"><svg class="session-workspace-chevron" width="14" height="14" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7" fill="none" stroke="currentColor"/></svg><span class="session-workspace-icon">◇</span><span class="session-workspace-name">${['蛋白结构与功能研究','很长的项目名称测试折叠后仍能正常展示','文献与实验记录','分子动力学'][i]}</span><span class="session-workspace-count"><svg width="12" height="12" viewBox="0 0 24 24"><path d="M4 4h16v12H8l-4 4Z" fill="none" stroke="currentColor"/></svg><span>${[12, 9999, 3, 86][i]}</span></span><details class="session-workspace-menu"><summary>⋯</summary><div><button>重命名</button></div></details></div></section>`).join('');
    await win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<style>${css}*{box-sizing:border-box}body{font:14px system-ui;margin:24px;background:var(--tcode-bg);--tcode-bg:#f5f7f3;--tcode-surface:#fff;--tcode-surface-soft:#f8faf7;--tcode-border:#dce3d8;--tcode-text:#28332c;--tcode-primary:#586641}main{width:300px}</style><main>${groups}</main>`)}`);
    for (const dark of [false, true]) {
      if (dark) await win.webContents.executeJavaScript(`document.body.style.cssText='--tcode-bg:#181c20;--tcode-surface:#252a30;--tcode-surface-soft:#20252a;--tcode-border:#414950;--tcode-text:#edf0f2'`);
      for (const width of [220, 300, 400]) {
        await win.webContents.executeJavaScript(`document.querySelector('main').style.width='${width}px'`);
        assert(await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('.session-workspace-heading')).every(h=>{const r=h.getBoundingClientRect(),b=h.querySelector('.session-workspace-count').getBoundingClientRect(),n=h.querySelector('.session-workspace-name').getBoundingClientRect();return b.right<=r.right&&n.right<=b.left&&h.scrollWidth<=h.clientWidth})`), `${width}px ${dark ? 'dark' : 'light'} overflow`);
        assert(await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('.session-workspace-name')).every(n=>n.clientWidth>=${width === 220 ? 90 : 160}&&n.clientHeight<=40)`), 'workspace names need readable width and at most two lines');
      }
    }
    console.log('PASS: project groups and four-digit count badges fit 220/300/400px in light and dark surfaces');
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { win.destroy(); app.exit(process.exitCode || 0); }
});
