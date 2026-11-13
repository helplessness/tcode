const { contextBridge, ipcRenderer } = require('electron');

// Previews and MCP frames never receive native capabilities.
if (process.isMainFrame) {
  contextBridge.exposeInMainWorld('tcodeDesktop', Object.freeze({
    modelSettings: payload => ipcRenderer.invoke('desktop:model-settings', payload),
    workspace: (workspaceId, action) => ipcRenderer.invoke('desktop:workspace', { workspaceId, action }),
    revealFile: (workspaceId, path) => ipcRenderer.invoke('desktop:reveal-file', { workspaceId, path }),
    resolveFile: (workspaceId, path) => ipcRenderer.invoke('desktop:resolve-file', { workspaceId, path }),
    copyFilePath: (workspaceId, path) => ipcRenderer.invoke('desktop:copy-file-path', { workspaceId, path }),
    copyText: text => ipcRenderer.invoke('desktop:copy-text', text),
    saveFile: resource => ipcRenderer.invoke('desktop:save-file', resource),
    onDownloadResult: callback => {
      const listener = (_event, result) => callback(result);
      ipcRenderer.on('desktop:download-result', listener);
      return () => ipcRenderer.removeListener('desktop:download-result', listener);
    },
    notify: event => ipcRenderer.invoke('desktop:notify', event),
    browserControl: (session, action) => ipcRenderer.invoke('desktop:browser-control', { session, action }),
    browserInput: (session, input) => ipcRenderer.invoke('desktop:browser-input', { session, input }),
    browserPanel: (session, command) => ipcRenderer.invoke('desktop:browser-panel', { session, command }),
    onBrowserNewTab: callback => {
      const listener = (_event, url) => callback(url);
      ipcRenderer.on('desktop:browser-new-tab', listener);
      return () => ipcRenderer.removeListener('desktop:browser-new-tab', listener);
    },
    computerPreview: () => ipcRenderer.invoke('desktop:computer-preview'),
    computerStatus: () => ipcRenderer.invoke('desktop:computer-status'),
    computerOpenPermission: permission => ipcRenderer.invoke('desktop:computer-permission', permission),
    computerRefreshPermissions: () => ipcRenderer.invoke('desktop:computer-permission-refresh'),
    computerControl: action => ipcRenderer.invoke('desktop:computer-control', action),
    computerSetEnabled: enabled => ipcRenderer.invoke('desktop:computer-enabled', enabled),
    computerGrant: grant => ipcRenderer.invoke('desktop:computer-grant', grant),
    computerReject: approval => ipcRenderer.invoke('desktop:computer-reject', approval),
    onComputerStatus: callback => {
      const listener = (_event, payload) => callback(payload);
      ipcRenderer.on('desktop:computer-status', listener);
      return () => ipcRenderer.removeListener('desktop:computer-status', listener);
    },
    computerPanelRequest: () => ipcRenderer.invoke('desktop:computer-panel-request'),
    computerPanelAck: requestId => ipcRenderer.invoke('desktop:computer-panel-ack', requestId),
    onComputerPanelOpen: callback => {
      const listener = (_event, payload) => callback(payload);
      ipcRenderer.on('desktop:computer-panel-open', listener);
      return () => ipcRenderer.removeListener('desktop:computer-panel-open', listener);
    },
    onNotification: callback => {
      const listener = (_event, id) => callback(id);
      ipcRenderer.on('desktop:notification-click', listener);
      return () => ipcRenderer.removeListener('desktop:notification-click', listener);
    },
    onRecovery: callback => {
      const listener = (_event, payload) => callback(payload);
      ipcRenderer.on('desktop:recovery', listener);
      return () => ipcRenderer.removeListener('desktop:recovery', listener);
    },
  }));
}
