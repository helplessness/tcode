const { app, BrowserWindow, WebContentsView, Menu, Tray, nativeImage, dialog, shell, session, ipcMain, Notification, globalShortcut, powerMonitor, clipboard, systemPreferences, safeStorage, screen } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { startBackend, isLocalNavigation, isExternalLink } = require('./backend.cjs');
const { assertSender, downloadRoute, reloadDesktopPage, isResearchMapPopup } = require('./native.cjs');
const { createRecoveryController } = require('./recovery.cjs');
const { ComputerBroker, McpDriverAdapter, approvalPresentation, resolvedApprovalPanelRequest } = require('./computer.cjs');
const { createTrayPanel } = require('./tray-panel.cjs');
const { DesktopBrowser } = require('./browser.cjs');
const { createTestInstance, readTestLogin } = require('./test-instance.cjs');
const { modelSecrets } = require('./model-secrets.cjs');

app.setName('TCode Desktop');
// Keep desktop Chromium state separate from Electron and the Web runtime.
app.setPath('userData', path.join(app.getPath('appData'), 'TCode Desktop'));
const projectRoot = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..');
const smoke = process.argv.includes('--smoke-test');
const testInstance = process.argv.includes('--test-instance') ? createTestInstance(app.getPath('temp'), process.env,
  readTestLogin(process.env.TCODE_DESKTOP_TEST_LOGIN_CONFIG || path.join(projectRoot, '.tcode-local/symphony_route.env'))) : undefined;
if (testInstance) {
  app.setName('TCode Desktop Test');
  app.setPath('userData', testInstance.root);
  app.setPath('sessionData', testInstance.root);
  console.log(`独立测试实例目录：${testInstance.root}（使用原配置账号，数据与登录状态独立）`);
}
if (smoke) app.setPath('userData', app.isPackaged
  ? path.join(app.getPath('temp'), 'tcode-desktop-smoke')
  : path.join(projectRoot, 'tmp/desktop-smoke'));
const dataRoot = app.getPath('userData');
const secrets = modelSecrets(dataRoot, safeStorage);
let modelConfigPending = false;
const runtimeRoot = testInstance ? path.join(dataRoot, 'runtime') : process.env.TCODE_DESKTOP_RUNTIME || path.join(dataRoot, 'runtime');
const logPath = path.join(dataRoot, 'logs/backend.log');
const python = process.env.TCODE_DESKTOP_PYTHON || path.join(projectRoot, '.desktop-venv/bin/python');
const backendExecutable = app.isPackaged
  ? path.join(process.resourcesPath, 'backend', 'tcode-backend')
  : undefined;
let window;
let tray;
let trayPanel;
let backend;
let origin;
let quitting = false;
let starting = false;
const notified = new Set();
const notifications = new Set();
let shortcutEnabled = false;
let recovery;
let computer;
let browser;
let computerPanelRequest = 0;
let pendingComputerPanelRequest = 0;
let pendingComputerPanelApproval = '';
let presentedComputerApproval = '';
let computerStart;
const managedE2E = !testInstance && process.env.TCODE_DESKTOP_MANAGED_E2E === '1';
const lifecycleLog = managedE2E ? process.env.TCODE_DESKTOP_LIFECYCLE_LOG : '';
let quitReason = '';

function recordLifecycle(type, fields = {}) {
  if (!lifecycleLog) return;
  fs.mkdirSync(path.dirname(lifecycleLog), { recursive: true });
  fs.appendFileSync(lifecycleLog, `${JSON.stringify({ at: new Date().toISOString(), source: 'desktop', type, pid: process.pid, ...fields })}\n`, { mode: 0o600 });
}

function requestQuit(reason) {
  quitReason ||= reason;
  recordLifecycle('quit_requested', { reason: quitReason });
  app.quit();
}

function computerPreview() {
  const preview = computer?.preview();
  if (!preview) return null;
  const image = nativeImage.createFromBuffer(preview.bytes);
  if (image.isEmpty()) return null;
  return { image: image.resize({ width: 640 }).toDataURL(), app: preview.app, observed_at: preview.observed_at };
}

function emitComputerState() {
  const state = computer?.publicState() || { available: false, status: 'unavailable' };
  if (window && !window.isDestroyed()) window.webContents.send('desktop:computer-status', state);
  trayPanel?.update();
  const presentation = approvalPresentation(state, presentedComputerApproval);
  if (presentation.should_present) {
    presentedComputerApproval = presentation.key;
    requestComputerPanelOpen(presentation.key);
  } else if (!presentation.key) {
    const resolved = resolvedApprovalPanelRequest(
      pendingComputerPanelRequest, pendingComputerPanelApproval, presentedComputerApproval,
    );
    pendingComputerPanelRequest = resolved.request_id;
    pendingComputerPanelApproval = resolved.approval_key;
    presentedComputerApproval = '';
  }
}

function requestComputerPanelOpen(approvalKey = '') {
  showWindow();
  pendingComputerPanelRequest = ++computerPanelRequest;
  pendingComputerPanelApproval = String(approvalKey || '');
  if (window && !window.isDestroyed()) {
    window.webContents.send('desktop:computer-panel-open', { request_id: pendingComputerPanelRequest,
      reason: pendingComputerPanelApproval === 'permissions' ? 'permissions' : 'approval' });
  }
}

let accessibilityPromptRequested = false;
function requestComputerPermissionGuidance() {
  const state = computer?.publicState();
  if (state?.enabled && state.permissions?.status === 'known' && state.permissions.identity_verified
      && state.permissions.accessibility && state.permissions.screen_recording) return;
  requestComputerPanelOpen('permissions');
  // Request a native prompt once per app launch, never on status polling.
  // The user's decision remains authoritative; this does not grant permission.
  if (process.platform === 'darwin' && state?.enabled && state.permissions?.identity_verified
      && state.permissions.accessibility === false && !accessibilityPromptRequested) {
    accessibilityPromptRequested = true;
    try { systemPreferences.isTrustedAccessibilityClient(true); }
    catch { /* The panel retains the explicit system-settings buttons. */ }
  }
}

async function backendHealthy() {
  if (!origin || !backend?.token) return false;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4_000);
  try {
    const response = await fetch(`${origin}/api/health`, {
      headers: { Cookie: `tcode_desktop_gate=${backend.token}` },
      signal: controller.signal,
    });
    await response.arrayBuffer();
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

function emitRecoveryState(payload) {
  if (!window || window.isDestroyed()) return;
  window.webContents.send('desktop:recovery', payload);
}

function registerShortcut() {
  globalShortcut.unregisterAll();
  shortcutEnabled = globalShortcut.register('CommandOrControl+Shift+Space', () => {
    if (window?.isVisible() && window.isFocused()) window.hide(); else showWindow();
  });
  if (!shortcutEnabled) void dialog.showMessageBox(window, { message: '全局快捷键未能注册', detail: '⌘⇧Space 可能已被占用。仍可通过菜单栏或 Dock 打开 TCode。' });
}

async function resolveWorkspaceFile(payload) {
  if (!payload || typeof payload.workspaceId !== 'string' || !payload.workspaceId.trim() || typeof payload.path !== 'string' || !payload.path.trim()) throw new Error('请选择工作区文件');
  const cookies = await window.webContents.session.cookies.get({ url: origin });
  const response = await fetch(`${origin}/api/desktop/file?${new URLSearchParams({ workspace_id: payload.workspaceId, path: payload.path })}`, {
    headers: { Cookie: cookies.map(cookie => `${cookie.name}=${cookie.value}`).join('; '), 'X-TCode-Desktop-Native': backend.token },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '无法定位文件');
  return data.path;
}

function installNativeHandlers() {
  ipcMain.handle('desktop:model-settings', async (event, payload) => {
    assertSender(event, window, origin);
    if (modelConfigPending) throw new Error('正在处理模型配置，请稍候');
    if (!payload || !['list','save','test','default'].includes(payload.action)) throw new Error('无效的模型配置操作');
    modelConfigPending = true;
    let before;
    let changed = false;
    try {
      if (payload.action === 'save' && payload.api_key !== undefined) {
        if (!/^[a-z0-9-]{1,64}$/.test(payload.id) || typeof payload.api_key !== 'string' || payload.api_key.length > 8192) throw new Error('密钥或服务标识无效');
        before = secrets.read();
        const key = `TCODE_DESKTOP_MODEL_${payload.id.replaceAll('-', '_').toUpperCase()}_KEY`;
        secrets.write({...before, [key]:payload.api_key}); changed = true;
      }
      const cookies = await window.webContents.session.cookies.get({ url: origin });
      const response = await fetch(`${origin}/api/desktop/model-settings`, {
        method:'POST', headers:{'Content-Type':'application/json', Cookie:cookies.map(c=>`${c.name}=${c.value}`).join('; '), 'X-TCode-Desktop-Native':backend.token},
        body:JSON.stringify(payload), signal:AbortSignal.timeout(30000),
      });
      const value = await response.json();
      if (!response.ok) {
        if(changed){secrets.write(before);changed=false;}
        return {ok:false,message:typeof value.detail==='string'?value.detail:'模型配置未保存，请重试'};
      }
      return {...value, secure_storage:secrets.available()};
    } catch {
      // Unknown network commit outcomes retain the encrypted credential.
      return {ok:false,message:'无法完成模型配置，请检查本地服务和系统密钥存储后重试'};
    } finally { modelConfigPending=false; }
  });
  ipcMain.handle('desktop:workspace', async (event, payload) => {
    assertSender(event, window, origin);
    if (!payload || !['finder', 'terminal'].includes(payload.action) || typeof payload.workspaceId !== 'string' || !payload.workspaceId.trim()) throw new Error('请选择工作区');
    const cookies = await window.webContents.session.cookies.get({ url: origin });
    const response = await fetch(`${origin}/api/desktop/workspace?${new URLSearchParams({ workspace_id: payload.workspaceId })}`, {
      headers: {
        Cookie: cookies.map(cookie => `${cookie.name}=${cookie.value}`).join('; '),
        'X-TCode-Desktop-Native': backend.token,
      },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '无法打开工作区');
    if (payload.action === 'finder') {
      const error = await shell.openPath(data.path);
      if (error) throw new Error(error);
    } else {
      // Explicit user action only. Never pass a model command to a host shell.
      await new Promise((resolve, reject) => {
        const child = spawn('/usr/bin/open', ['-a', 'Terminal', data.path], { stdio: 'ignore' });
        child.once('error', reject);
        child.once('exit', code => code === 0 ? resolve() : reject(new Error('无法打开系统终端')));
      });
    }
    return { success: true };
  });
  ipcMain.handle('desktop:reveal-file', async (event, payload) => {
    assertSender(event, window, origin);
    shell.showItemInFolder(await resolveWorkspaceFile(payload));
    return { success: true };
  });
  ipcMain.handle('desktop:resolve-file', async (event, payload) => {
    assertSender(event, window, origin);
    return { path: await resolveWorkspaceFile(payload) };
  });
  ipcMain.handle('desktop:copy-file-path', async (event, payload) => {
    assertSender(event, window, origin);
    const resolvedPath = await resolveWorkspaceFile(payload);
    clipboard.writeText(resolvedPath);
    return { path: resolvedPath };
  });
  ipcMain.handle('desktop:copy-text', (event, text) => {
    assertSender(event, window, origin);
    if (typeof text !== 'string' || text.length > 5_000_000) throw new Error('无效复制内容');
    clipboard.writeText(text);
    return { success: true };
  });
  ipcMain.handle('desktop:save-file', (event, resource) => {
    assertSender(event, window, origin);
    window.webContents.downloadURL(origin + downloadRoute(resource));
    return { success: true };
  });
  ipcMain.handle('desktop:notify', (event, value) => {
    assertSender(event, window, origin);
    if (!value || typeof value.id !== 'string' || value.id.length > 256 || typeof value.title !== 'string' || typeof value.body !== 'string') throw new Error('无效通知');
    if (!Notification.isSupported() || notified.has(value.id)) return false;
    notified.add(value.id);
    if (notified.size > 1000) notified.delete(notified.values().next().value);
    const notification = new Notification({ title: value.title.slice(0, 100), body: value.body.slice(0, 300) });
    notifications.add(notification);
    notification.on('click', () => { showWindow(); window.webContents.send('desktop:notification-click', value.id); });
    notification.on('close', () => notifications.delete(notification));
    notification.show();
    return true;
  });
  ipcMain.handle('desktop:computer-preview', event => {
    assertSender(event, window, origin);
    return computerPreview();
  });
  ipcMain.handle('desktop:computer-status', async event => {
    assertSender(event, window, origin);
    return computer ? computer.refreshPermissions() : { available: false, status: 'unavailable' };
  });
  ipcMain.handle('desktop:computer-permission', async (event, permission) => {
    assertSender(event, window, origin);
    const urls = {
      accessibility: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility',
      screen_recording: 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
    };
    if (!computer || process.platform !== 'darwin' || typeof permission !== 'string' || !Object.hasOwn(urls, permission)) throw new Error('无效的权限设置操作');
    if (permission === 'accessibility') systemPreferences.isTrustedAccessibilityClient(true);
    await shell.openExternal(urls[permission]);
    return { success: true };
  });
  ipcMain.handle('desktop:computer-permission-refresh', async event => {
    assertSender(event, window, origin);
    if (!computer) throw new Error('电脑控制不可用');
    return computer.refreshPermissions(true);
  });
  ipcMain.handle('desktop:browser-control', async (event, payload) => {
    assertSender(event, window, origin);
    if (!browser || !payload || typeof payload.session !== 'string' || !['takeover', 'release'].includes(payload.action)) throw new Error('无效的浏览器控制操作');
    if (payload.action === 'takeover' && /^manual-browser-[\w-]+$/.test(payload.session) && ![...browser.pages.values()].some(page => page.session === payload.session && !page.closed)) {
      await browser.openBlank(payload.session);
    }
    return browser.control(payload.session, payload.action);
  });
  ipcMain.handle('desktop:browser-input', (event, payload) => {
    assertSender(event, window, origin);
    if (!browser || !payload || typeof payload.session !== 'string') throw new Error('无效的浏览器输入');
    return browser.input(payload.session, payload.input);
  });
  ipcMain.handle('desktop:browser-panel', (event, payload) => {
    assertSender(event, window, origin);
    if (!browser || !payload || typeof payload.session !== 'string' || !payload.command) throw new Error('无效的浏览器操作');
    return browser.panel(payload.session, window, payload.command);
  });
  window.webContents.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => {
    if (!isMainFrame || isInPlace || !browser) return;
    for (const page of browser.pages.values()) if (page.host === window) {
      browser.detach(page);
      void browser.control(page.session, 'release').catch(() => {});
    }
  });
  ipcMain.handle('desktop:computer-panel-request', event => {
    assertSender(event, window, origin);
    return { request_id: pendingComputerPanelRequest || null,
      reason: pendingComputerPanelApproval === 'permissions' ? 'permissions' : 'approval' };
  });
  ipcMain.handle('desktop:computer-panel-ack', (event, requestId) => {
    assertSender(event, window, origin);
    if (Number.isSafeInteger(requestId) && requestId === pendingComputerPanelRequest) {
      pendingComputerPanelRequest = 0;
      pendingComputerPanelApproval = '';
    }
    return { success: true };
  });
  ipcMain.handle('desktop:computer-control', (event, action) => {
    assertSender(event, window, origin);
    if (!computer || !['pause', 'takeover', 'emergency_stop'].includes(action)) throw new Error('无效的电脑控制操作');
    const state = computer.revoke(action === 'takeover' ? 'user_takeover' : action);
    emitComputerState();
    return state;
  });
  ipcMain.handle('desktop:computer-enabled', async (event, enabled) => {
    assertSender(event, window, origin);
    if (!computer || typeof enabled !== 'boolean') throw new Error('无效的电脑控制开关');
    computer.setEnabled(enabled);
    return enabled ? computer.refreshPermissions(true) : computer.publicState();
  });
  ipcMain.handle('desktop:computer-grant', (event, grant) => {
    assertSender(event, window, origin);
    if (!computer) throw new Error('电脑控制不可用');
    const grantId = computer.grantPending(grant);
    emitComputerState();
    return { success: true, grant_id: grantId, state: computer.publicState() };
  });
  ipcMain.handle('desktop:computer-reject', (event, approval) => {
    assertSender(event, window, origin);
    if (!computer) throw new Error('电脑控制不可用');
    const state = computer.rejectPending(approval);
    emitComputerState();
    return state;
  });
}

function showWindow() {
  if (!window || window.isDestroyed()) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function showStatus(title, detail) {
  const escape = value => value.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const startupCss = fs.readFileSync(path.join(__dirname, 'startup.css'), 'utf8');
  const orb = title.startsWith('正在') ? '<div class="startup-orb-wrapper" aria-hidden="true"><span>T</span><span>c</span><span>o</span><span>d</span><span>e</span><div class="startup-orb"></div></div>' : '';
  const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>TCode Desktop</title><style>${startupCss}</style><main role="status">${orb}<h1>${escape(title)}</h1><p>${escape(detail)}</p></main></html>`;
  return window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
}

async function openExternal(url) {
  if (!isExternalLink(url)) return;
  const { response } = await dialog.showMessageBox(window, {
    type: 'question', message: '在系统应用中打开链接？', detail: url,
    buttons: ['打开', '取消'], defaultId: 0, cancelId: 1,
  });
  if (response === 0) await shell.openExternal(url);
}

async function start() {
  if (starting || quitting) return false;
  starting = true;
  try {
    if (backend) { computer?.rotateSession('backend_restart'); await backend.stop(); await browser?.reset(); }
    origin = undefined;
    await showStatus('正在启动 TCode', '正在加载本地运行环境与工作台。首次启动可能需要一些时间。');
    const runtimeEntry = backendExecutable || python;
    if (!fs.existsSync(runtimeEntry)) throw new Error(app.isPackaged
      ? '安装包中的 TCode 后端缺失，请重新安装应用。'
      : '未找到桌面 Python 环境。请先运行 scripts/start_desktop.sh，或设置 TCODE_DESKTOP_PYTHON。');
    if (!browser) { browser = new DesktopBrowser({ BrowserWindow, WebContentsView, session, dialog, shell, Menu }); await browser.start(); }
    backend = startBackend({ projectRoot, runtimeRoot, logPath, python, backendExecutable, environment: {...(testInstance?.environment || process.env), ...secrets.read()}, computer: computer?.startupPayload(), browser: browser.startupPayload(), onExit: (code, signal) => {
      recordLifecycle('backend_exit', { expected: quitting, code, signal });
      if (!starting && !quitting) {
        origin = undefined;
        void showStatus('正在恢复 TCode', '本地服务意外停止，正在自动恢复任务与工作台。\n远程 MCP 任务会沿用原任务编号，不会重复提交。');
        recovery?.schedule('backend-exit');
      }
    } });
    origin = await backend.ready;
    await window.webContents.session.cookies.set({
      url: origin, name: 'tcode_desktop_gate', value: backend.token,
      httpOnly: true, sameSite: 'strict', path: '/', secure: false,
    });
    await window.loadURL(`${origin}/desktop.html`);
    // Startup may finish while the window is occluded by another app. Present
    // the loaded workbench again, but respect a window the user explicitly hid.
    if (window.isVisible()) showWindow();
    if (managedE2E) {
      await computerStart;
      const healthy = await backendHealthy();
      const payload = {
        type: 'desktop_ready', protocol: 1, pid: process.pid, project_root: projectRoot,
        renderer_url: window.webContents.getURL(), backend_healthy: healthy,
        host_generation: computer?.publicState()?.driver_generation,
      };
      console.log(`TCODE_DESKTOP_READY ${JSON.stringify(payload)}`);
      recordLifecycle('ready', { backend_pid: backend.child.pid, renderer: 'loopback_desktop', host_generation: payload.host_generation });
    }
    if (smoke) {
      const health = await fetch(`${origin}/api/health`, {
        headers: { Cookie: `tcode_desktop_gate=${backend.token}` },
      });
      if (!health.ok) throw new Error(`Health check failed: ${health.status}`);
      const denied = await fetch(`${origin}/api/health`);
      if (denied.status !== 403) throw new Error('Unauthenticated loopback access was not blocked');
      const crossOrigin = await fetch(`${origin}/api/health`, {
        headers: { Cookie: `tcode_desktop_gate=${backend.token}`, Origin: 'https://untrusted.example' },
      });
      if (crossOrigin.status !== 403) throw new Error('Cross-origin access was not blocked');
      if (process.env.TCODE_DESKTOP_SMOKE_EMAIL && process.env.TCODE_DESKTOP_SMOKE_PASSWORD) {
        // Optional integration run against a disposable runtime and test account.
        // Uses the normal login and file APIs; no authentication bypass is installed.
        const credentials = JSON.stringify({ email: process.env.TCODE_DESKTOP_SMOKE_EMAIL, password: process.env.TCODE_DESKTOP_SMOKE_PASSWORD });
        const loginStatus = await window.webContents.executeJavaScript(`fetch('/api/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(${credentials})}).then(r=>r.status)`);
        if (loginStatus !== 200) throw new Error(`Smoke test account login failed: ${loginStatus}`);
        const services = await window.webContents.executeJavaScript(`fetch('/api/desktop/services').then(async r=>{if(!r.ok) throw new Error('Desktop service status failed');return r.json()})`);
        if (services.local_runner_required !== false || !services.config_file || !['unconfigured', 'invalid', 'connected', 'disconnected'].includes(services.remote)) throw new Error('Desktop remote service configuration boundary failed');
        if (services.local_execution !== 'offline-single-process') throw new Error('Native Seatbelt startup probe did not pass');
        const uploadPath = await window.webContents.executeJavaScript(`(async()=>{
          const created=await fetch('/api/workdirs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'desktop-smoke'})});
          if(!created.ok && created.status!==409) throw new Error('Smoke workspace creation failed');
          const form=new FormData();form.append('file',new File(['desktop-file-check'],'sample.txt'));
          const response=await fetch('/api/files/upload?workspace_id=desktop-smoke&relative_path=folder/sample.txt',{method:'POST',body:form});
          if(!response.ok) throw new Error('Smoke file import failed');
          const result=await response.json();
          const read=await fetch('/api/files/content?workspace_id=desktop-smoke&path='+encodeURIComponent(result.relative_path));
          if(!read.ok || (await read.json()).content!=='desktop-file-check') throw new Error('Smoke file roundtrip failed');
          return result.relative_path;
        })()`);
        if (!uploadPath.startsWith('upload/folder/')) throw new Error('Folder structure was not preserved');
        const cookies = await window.webContents.session.cookies.get({ url: origin });
        const cookie = cookies.map(item => `${item.name}=${item.value}`).join('; ');
        const nativeUrl = `${origin}/api/desktop/workspace?workspace_id=desktop-smoke`;
        const forbidden = await fetch(nativeUrl, { headers: { Cookie: cookie } });
        if (forbidden.status !== 403) throw new Error('Renderer could resolve native paths without the main-process credential');
        const resolved = await fetch(nativeUrl, { headers: { Cookie: cookie, 'X-TCode-Desktop-Native': backend.token } });
        if (!resolved.ok) throw new Error(`Native workspace resolution failed: ${resolved.status}`);
        const directory = (await resolved.json()).path;
        const relative = path.relative(runtimeRoot, directory);
        if (relative.startsWith('..') || path.isAbsolute(relative) || !fs.statSync(directory).isDirectory()) throw new Error('Native workspace escaped the disposable runtime');
        await window.loadURL(`${origin}/desktop.html`);
      }
      // Allow the existing SPA to finish its initial auth render.
      await new Promise(resolve => setTimeout(resolve, 2500));
      const text = await window.webContents.executeJavaScript('document.body.innerText');
      if (!/登录|工作区/.test(text) || text.includes('Desktop transport authentication required')) {
        throw new Error('Frontend did not render the login page or workspace');
      }
      if (process.env.TCODE_DESKTOP_SMOKE_EMAIL && !await window.webContents.executeJavaScript("Boolean(document.querySelector('.studio-shell'))")) throw new Error('Authenticated desktop workspace did not render');
      const capture = await window.webContents.capturePage();
      fs.mkdirSync(dataRoot, { recursive: true });
      fs.writeFileSync(path.join(dataRoot, 'desktop.png'), capture.toPNG());
      window.close();
      if (window.isDestroyed() || window.isVisible()) throw new Error('Close did not hide the window');
      showWindow();
      console.log(JSON.stringify({ smoke: 'passed', backendPid: backend.child.pid, screenshot: path.join(dataRoot, 'desktop.png'), text: text.slice(0, 400) }));
      app.quit();
    }
    return true;
  } catch (error) {
    if (backend) await backend.stop();
    origin = undefined;
    await showStatus('TCode 启动失败', `${error.message}\n\n日志：${logPath}\n可从应用菜单查看日志或重试。`);
    if (smoke) { console.error(error.message); process.exitCode = 1; app.quit(); }
    else if (!quitting) recovery?.schedule('startup-failure');
    return false;
  } finally { starting = false; }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);
  app.on('activate', () => {
    // A tray popup already owns focus; raising the main window would dismiss it.
    if (!trayPanel?.isVisible()) showWindow();
  });
  app.on('window-all-closed', () => {});
  app.on('before-quit', event => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    recordLifecycle('before_quit', { reason: quitReason || 'app_event' });
    void (async () => {
      await backend?.stop();
      await computer?.stop();
      await browser?.stop();
      recovery?.cancel();
      globalShortcut.unregisterAll();
      trayPanel?.destroy();
      tray?.destroy();
      recordLifecycle('cleanup_complete', { reason: quitReason || 'app_event', code: process.exitCode || 0 });
      app.exit(process.exitCode || 0);
    })();
  });
  process.on('SIGINT', () => requestQuit('signal_sigint'));
  process.on('SIGTERM', () => requestQuit('signal_sigterm'));
  if (managedE2E) {
    process.stdin.once('end', () => requestQuit('parent_eof'));
    process.stdin.resume();
  }
  app.whenReady().then(() => {
    const desktopSession = session.fromPartition('persist:tcode-desktop');
    desktopSession.webRequest.onBeforeSendHeaders((details, callback) => {
      // Cookies have no port boundary. Never leak our launch credential to a
      // different loopback service loaded by a preview or remote resource.
      if (!origin || !isLocalNavigation(details.url.replace(/^ws/, 'http'), origin)) {
        for (const key of Object.keys(details.requestHeaders)) {
          if (key.toLowerCase() === 'cookie') {
            details.requestHeaders[key] = details.requestHeaders[key].split(';').filter(
              item => !/^(tcode_desktop_gate|tcode_auth_session)=/.test(item.trim()),
            ).join(';');
          }
        }
      }
      callback({ requestHeaders: details.requestHeaders });
    });
    desktopSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    desktopSession.setPermissionCheckHandler(() => false);
    const packagedDriver = path.join(process.resourcesPath, 'computer', 'cua-driver');
    const developmentDriver = process.env.TCODE_COMPUTER_DRIVER || path.join(projectRoot, 'desktop', 'vendor', 'cua-driver');
    const computerDriverPath = app.isPackaged ? packagedDriver : developmentDriver;
    computer = new ComputerBroker({
      runtimeRoot,
      enabled: false,
      driver: fs.existsSync(computerDriverPath) ? new McpDriverAdapter({ executable: computerDriverPath, hostBundleId: app.isPackaged ? 'com.tcode.desktop' : 'com.github.Electron' }) : null,
      onStateChange: emitComputerState,
      onPermissionRequired: requestComputerPermissionGuidance,
      hostPermissions: () => ({ accessibility: systemPreferences.isTrustedAccessibilityClient(false),
        screen_recording: systemPreferences.getMediaAccessStatus('screen') === 'granted' }),
    });
    if (managedE2E && !app.isPackaged && process.env.TCODE_COMPUTER_TEST_EVIDENCE_APP) {
      const directory = path.join(projectRoot, '.tcode', 'computer-test-evidence', 'music-input');
      computer.onTestEvidence = require('./test/input-evidence.cjs').inputEvidence({
        directory, bundleId: process.env.TCODE_COMPUTER_TEST_EVIDENCE_APP, driver: computer.driver,
      });
    }
    computerStart = computer.start().then(() => emitComputerState()).catch(error => {
      console.error(`Computer broker unavailable: ${error.message}`);
      computer = undefined;
      throw error;
    });
    window = new BrowserWindow({
      title: testInstance ? 'TCode Desktop · 独立测试' : 'TCode Desktop', width: 1440, height: 960, minWidth: 980, minHeight: 680,
      backgroundColor: '#f7f8f7',
      webPreferences: { session: desktopSession, preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true, backgroundThrottling: false },
    });
    if (testInstance) window.on('page-title-updated', event => event.preventDefault());
    installNativeHandlers();
    desktopSession.on('will-download', (_event, item, contents) => {
      // Chromium owns the file picker and streaming write. Do not accept a
      // renderer-provided local destination or buffer entire artifacts in IPC.
      item.setSaveDialogOptions({ title: '保存到本地', buttonLabel: '保存' });
      const downloadUrl = item.getURL();
      const owner = contents === window.webContents ? contents : undefined;
      item.once('done', async (_done, state) => {
        if (owner && !owner.isDestroyed()) owner.send('desktop:download-result', { url: downloadUrl, state });
        if (state === 'cancelled') return;
        if (state !== 'completed') {
          await dialog.showMessageBox(window, { type: 'error', message: '文件未能保存', detail: '请检查网络和目标目录后重试。' });
          return;
        }
        const result = await dialog.showMessageBox(window, { message: '文件已保存', detail: item.getSavePath(), buttons: ['在 Finder 中显示', '完成'], defaultId: 1 });
        if (result.response === 0) shell.showItemInFolder(item.getSavePath());
      });
    });
    window.on('close', event => { if (!quitting) { event.preventDefault(); window.hide(); } });
    window.webContents.on('will-navigate', (event, url) => {
      if (!origin || !isLocalNavigation(url, origin)) { event.preventDefault(); void openExternal(url); }
    });
    window.webContents.on('will-redirect', (event, url) => {
      if (!origin || !isLocalNavigation(url, origin)) event.preventDefault();
    });
    window.webContents.setWindowOpenHandler(({ url, frameName, referrer }) => {
      // 仅允许主界面的只读研究地图空窗口；不继承原生能力桥。
      if (isResearchMapPopup({ url, frameName, referrer }, origin)) {
        return { action: 'allow', overrideBrowserWindowOptions: { title: '研究地图', width: 1280, height: 900,
          webPreferences: { preload: '', nodeIntegration: false, contextIsolation: true, sandbox: true } } };
      }
      void openExternal(url); return { action: 'deny' };
    });
    window.webContents.on('did-create-window', child => {
      child.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      child.webContents.on('will-navigate', event => event.preventDefault());
      child.webContents.on('will-redirect', event => event.preventDefault());
      child.webContents.on('will-attach-webview', event => event.preventDefault());
    });
    window.webContents.on('will-attach-webview', event => event.preventDefault());
    window.webContents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
      if (!isMainFrame || code === -3 || quitting) return; // Cancelled navigations are expected during reload.
      recordLifecycle('page_load_failed', { code, description });
      if (origin && isLocalNavigation(url, origin)) {
        void showStatus('界面加载失败', '请从“显示”菜单选择“重新加载”重试。已保存的会话与交互界面仍保留在本地。');
      }
    });
    window.webContents.on('render-process-gone', (_event, details) => {
      recordLifecycle('renderer_exit', { reason: String(details?.reason || 'unknown'), exit_code: Number(details?.exitCode || 0) });
      if (!quitting) recovery?.schedule('renderer-exit');
    });
    recovery = createRecoveryController({
      healthCheck: backendHealthy,
      restart: async () => {
        if (!await start()) throw new Error('TCode backend restart failed');
      },
      notify: payload => {
        emitRecoveryState(payload);
        if (payload.state === 'ready' && payload.reason === 'renderer-exit' && origin) {
          void window.loadURL(`${origin}/desktop.html`);
        }
      },
    });
    powerMonitor.on('resume', () => {
      computer?.revoke('system_resume');
      emitComputerState();
      // macOS may report resume before sockets and network routes are usable.
      setTimeout(() => recovery?.recover('system-resume').catch(() => {}), 750);
    });
    powerMonitor.on('suspend', () => { computer?.revoke('system_suspend'); emitComputerState(); });
    powerMonitor.on('lock-screen', () => { computer?.revoke('screen_locked'); emitComputerState(); });
    const icon = nativeImage.createFromPath(path.join(__dirname, 'assets/tcodeTemplate.png')).resize({ width: 18, height: 18 });
    icon.setTemplateImage(true);
    tray = new Tray(icon);
    trayPanel = createTrayPanel({ BrowserWindow, ipcMain, screen, tray, nativeImage,
      getState: () => computer?.publicState(), getPreview: computerPreview,
      showWindow, quit: () => requestQuit('tray_quit') });
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { label: 'TCode Desktop', submenu: [
        { label: '关于 TCode Desktop', click: () => dialog.showMessageBox(window, { message: 'TCode Desktop · 本地开发版', detail: '关闭窗口后任务继续运行。系统唤醒或本地服务异常退出后会自动恢复；远程任务沿用原任务编号继续跟踪。' }) },
        { label: '打开运行数据目录', click: () => shell.openPath(runtimeRoot) },
        { label: '查看启动日志', click: () => shell.showItemInFolder(logPath) },
        { label: '重启本地服务…', click: async () => {
          const result = await dialog.showMessageBox(window, { message: '重启本地服务？', detail: '本地执行会中断。此操作不发送远程任务取消请求。', buttons: ['重启', '取消'], defaultId: 1, cancelId: 1 });
          if (result.response === 0) void start();
        } },
        { type: 'separator' }, { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
        { type: 'separator' }, { label: '退出 TCode', accelerator: 'Command+Q', click: () => app.quit() },
      ] },
      { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
      { label: '显示', submenu: [
        { label: '显示主窗口', accelerator: 'Command+1', click: showWindow },
        { label: '菜单栏电脑控制状态', accelerator: 'Command+Option+U', click: () => trayPanel?.toggle() },
        { label: '电脑控制面板', accelerator: 'Control+Option+C', click: () => requestComputerPanelOpen() },
        { label: '重新加载', accelerator: 'Command+R', click: () => {
          if (!origin) { void start(); return; }
          void reloadDesktopPage(window.webContents, origin);
        } },
        { label: '启用全局快捷键 ⌘⇧Space', type: 'checkbox', checked: false, click: item => {
          if (item.checked) { registerShortcut(); item.checked = shortcutEnabled; }
          else { globalShortcut.unregisterAll(); shortcutEnabled = false; }
        } },
        { role: 'toggleDevTools' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' },
      ] },
      { role: 'windowMenu' },
    ]));
    void start();
  });
}
