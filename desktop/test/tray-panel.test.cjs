const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createTrayPanel, trayActivity } = require('../tray-panel.cjs');

test('tray reflects actual running state and approved app instead of the enabled switch', () => {
  assert.equal(trayActivity({ enabled: true, status: 'paused' }).running, false);
  assert.equal(trayActivity({ enabled: true, status: 'running', active_scope: { display_name: 'NeteaseMusic' } }).app, '网易云音乐');
  assert.equal(trayActivity({ enabled: false, status: 'running' }).running, false);
  const activity = trayActivity({ enabled: true, status: 'running', active_scope: { display_name: '网易云音乐' } }, { image: 'data:image/png;base64,x', app: '网易云音乐', observed_at: 123 });
  assert.deepEqual(activity, { running: true, label: '正在操作电脑', app: '网易云音乐', image: 'data:image/png;base64,x', observed_at: 123 });
});

test('tray dismisses on native blur, rejects foreign IPC, and clears preview state on hide', () => {
  let panel;
  class Window extends EventEmitter {
    constructor() { super(); panel = this; this.visible = false; this.bounds = {height:240}; this.webContents = new EventEmitter(); this.webContents.mainFrame = {}; this.sent=[]; this.webContents.send=(...args)=>this.sent.push(args); this.webContents.setWindowOpenHandler=()=>{}; }
    setAlwaysOnTop(){} getBounds(){return this.bounds;} setBounds(b){this.bounds=b;}
    isVisible(){return this.visible;} show(){this.visible=true;} focus(){}
    hide(){if(this.visible){this.visible=false;this.emit('hide');}} destroy(){} async loadFile(){}
  }
  const handlers = new Map(); const tray = new EventEmitter();
  tray.setImage=()=>{}; tray.setTitle=()=>{}; tray.setToolTip=()=>{};
  tray.getBounds=()=>({x:1900,y:0,width:70,height:25});
  const controller = createTrayPanel({ BrowserWindow: Window, tray,
    ipcMain: { handle:(name,fn)=>handlers.set(name,fn), removeHandler:name=>handlers.delete(name) },
    screen: { getDisplayMatching:()=>({workArea:{x:0,y:25,width:1920,height:1000}}) },
    nativeImage: { createFromPath:()=>({ resize(){return this;}, setTemplateImage(){} }) },
    getState:()=>({enabled:true,status:'running'}), getPreview:()=>null, showWindow(){}, quit(){} });
  tray.emit('click'); assert.equal(panel.visible,true); assert.equal(panel.bounds.x,1620);
  assert.throws(()=>handlers.get('tray:state')({sender:{},senderFrame:{url:'file:///untrusted'}}), /Untrusted/);
  panel.emit('blur'); assert.equal(panel.visible,false); assert.ok(panel.sent.some(([name])=>name==='tray:hidden'));
  tray.emit('click'); assert.equal(panel.visible,false, 'same tray click that blurs the popup must not reopen it');
  controller.destroy(); assert.equal(handlers.size,0);
});
