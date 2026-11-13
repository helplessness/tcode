const { isLocalNavigation } = require('./backend.cjs');

function reloadDesktopPage(contents, origin) {
  // Loading the base URL can only remove a hash instead of reloading the document.
  if (isLocalNavigation(contents.getURL(), origin)) return contents.reloadIgnoringCache();
  return contents.loadURL(`${origin}/desktop.html`);
}

function assertSender(event, window, origin) {
  if (!window || event.sender !== window.webContents ||
      event.senderFrame !== window.webContents.mainFrame ||
      !isLocalNavigation(event.senderFrame.url, origin) ||
      new URL(event.senderFrame.url).pathname !== '/desktop.html') {
    throw new Error('此页面不能调用桌面功能');
  }
}

function downloadRoute(resource) {
  if (!resource || typeof resource.path !== 'string' || !resource.path || resource.path.length > 4096) {
    throw new Error('请选择一个文件');
  }
  const params = new URLSearchParams({ path: resource.path });
  if (resource.jobId) {
    if (typeof resource.jobId !== 'string' || typeof resource.root !== 'string') throw new Error('无效的任务产物');
    params.set('root', resource.root);
    return `/api/jobs/${encodeURIComponent(resource.jobId)}/artifacts/download?${params}`;
  }
  if (typeof resource.workspaceId !== 'string' || !resource.workspaceId.trim()) throw new Error('请选择工作区');
  params.set('workspace_id', resource.workspaceId);
  return `/api/files/download?${params}`;
}

function isResearchMapPopup({ url, frameName, referrer }, origin) {
  if (url !== 'about:blank' || frameName !== 'tcode-research-map' || !origin || !isLocalNavigation(referrer?.url || '', origin)) return false;
  return new URL(referrer.url).pathname === '/desktop.html';
}

module.exports = { assertSender, downloadRoute, reloadDesktopPage, isResearchMapPopup };
