const { contextBridge, ipcRenderer } = require('electron');
if (process.isMainFrame) contextBridge.exposeInMainWorld('tcodeTray', Object.freeze({
  state: () => ipcRenderer.invoke('tray:state'),
  action: action => ipcRenderer.invoke('tray:action', action),
  height: height => ipcRenderer.invoke('tray:height', height),
  onState: callback => ipcRenderer.on('tray:state', (_event, state) => callback(state)),
  onHide: callback => ipcRenderer.on('tray:hidden', () => callback()),
}));
