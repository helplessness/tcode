const path = require('node:path');
const { pathToFileURL } = require('node:url');

function trayActivity(state, preview) {
  const running = state?.enabled === true && state.status === 'running';
  const app = preview?.app || state?.active_scope?.display_name || '';
  return { running, label: running ? '正在操作电脑' : state?.pending_approval ? '等待确认' : '暂无电脑操作',
    app: app === 'NeteaseMusic' ? '网易云音乐' : app,
    image: preview?.image || '', observed_at: preview?.observed_at || 0 };
}

function createTrayPanel({ BrowserWindow, ipcMain, screen, tray, nativeImage, getState, getPreview, showWindow, quit }) {
  const url = pathToFileURL(path.join(__dirname, 'tray-panel.html')).href;
  const panel = new BrowserWindow({ width: 300, height: 210, show: false, frame: false, resizable: false,
    minimizable: false, maximizable: false, fullscreenable: false, skipTaskbar: true,
    title: 'TCode 电脑控制', backgroundColor: '#00000000', transparent: true,
    vibrancy: 'menu', visualEffectState: 'active', roundedCorners: true,
    webPreferences: { preload: path.join(__dirname, 'tray-preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true } });
  panel.setAlwaysOnTop(true, 'pop-up-menu');
  panel.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  panel.webContents.on('will-navigate', event => event.preventDefault());
  panel.on('blur', () => panel.hide());
  panel.on('hide', () => panel.webContents.send('tray:hidden'));
  let timer;
  let lastIcon;
  const icons = [false, true].map(running => {
    const icon = nativeImage.createFromPath(path.join(__dirname, 'assets', running ? 'computerTemplate.png' : 'tcodeTemplate.png')).resize({ width: 18, height: 18 });
    icon.setTemplateImage(true);
    return icon;
  });
  function update() {
    const state = getState();
    const running = state?.enabled === true && state.status === 'running';
    if (running !== lastIcon) {
      tray.setImage(icons[Number(running)]); tray.setTitle('TCode');
      tray.setToolTip(running ? 'TCode 正在操作电脑' : 'TCode · 关闭窗口后继续运行'); lastIcon = running;
    }
    if (panel.isVisible()) panel.webContents.send('tray:state', trayActivity(state, getPreview()));
  }
  function position(height = panel.getBounds().height) {
    const bounds = tray.getBounds();
    const area = screen.getDisplayMatching(bounds).workArea;
    const width = 300;
    panel.setBounds({ x: Math.round(Math.max(area.x, Math.min(bounds.x + bounds.width / 2 - width / 2, area.x + area.width - width))),
      y: Math.round(Math.max(area.y, bounds.y + bounds.height)), width,
      height: Math.round(Math.min(height, area.height - 8)) });
  }
  let hiddenAt = 0;
  panel.on('hide', () => { hiddenAt = Date.now(); });
  function toggle() {
    // Clicking the tray first blurs the popup; do not reopen it on that same click.
    if (panel.isVisible() || Date.now() - hiddenAt < 200) { panel.hide(); return; }
    position(); panel.show(); panel.focus(); update();
  }
  function assertSender(event) {
    if (event.sender !== panel.webContents || event.senderFrame !== panel.webContents.mainFrame
      || event.senderFrame.url !== url) throw new Error('Untrusted tray sender');
  }
  ipcMain.handle('tray:state', event => { assertSender(event); return trayActivity(getState(), getPreview()); });
  ipcMain.handle('tray:action', (event, action) => {
    assertSender(event);
    if (action === 'hide') panel.hide();
    else if (action === 'open') { panel.hide(); showWindow(); }
    else if (action === 'quit') { panel.hide(); quit(); }
    else throw new Error('Unknown tray action');
  });
  ipcMain.handle('tray:height', (event, height) => {
    assertSender(event);
    if (!Number.isFinite(height)) return;
    position(Math.max(170, Math.min(520, height)));
  });
  tray.on('click', toggle); tray.on('right-click', toggle);
  void panel.loadFile(path.join(__dirname, 'tray-panel.html'));
  timer = setInterval(update, 1000); timer.unref(); update();
  return { update, toggle, isVisible: () => panel.isVisible(), destroy() {
    clearInterval(timer); tray.removeListener('click', toggle); tray.removeListener('right-click', toggle);
    for (const channel of ['tray:state', 'tray:action', 'tray:height']) ipcMain.removeHandler(channel);
    panel.destroy();
  } };
}
module.exports = { createTrayPanel, trayActivity };
