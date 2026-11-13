import { expect, it } from 'vitest';
import { desktopNotificationLedger } from './desktopNotifications';
import { planFolderImport } from './folderImport';

it('persists banner dedupe across reloads and isolates accounts', () => {
  const values = new Map<string,string>();
  const storage = {getItem:(key:string)=>values.get(key) || null,setItem:(key:string,value:string)=>{values.set(key,value);}};
  desktopNotificationLedger('alice', storage).remember('job-1');
  expect(desktopNotificationLedger('alice', storage).has('job-1')).toBe(true);
  expect(desktopNotificationLedger('bob', storage).has('job-1')).toBe(false);
  const broken = {getItem:()=>'{',setItem:()=>{throw new Error('quota');}};
  const ledger=desktopNotificationLedger('alice',broken);
  ledger.remember('job-2');
  expect(ledger.has('job-2')).toBe(true);
});

it('plans a bounded folder snapshot preserving paths and skipping hidden data', () => {
  const file=(path:string,size=10)=>({webkitRelativePath:path,size});
  const plan=planFolderImport([file('project/input/a.pdb'),file('project/.env'),file('project/.git/config'),file('project/node_modules/x')]);
  expect(plan.files.map(f=>f.webkitRelativePath)).toEqual(['project/input/a.pdb']);
  expect(plan.skipped).toBe(3);
  expect(plan.bytes).toBe(10);
  expect(()=>planFolderImport([file('project/../outside')])).toThrow();
  expect(()=>planFolderImport([file('project/large',101*1024**2)])).toThrow();
  expect(()=>planFolderImport(Array.from({length:201},(_,i)=>file(`project/${i}`)))).toThrow();
});
