import { confirmAction } from '../components/FeedbackDialog';

// Stop the original click synchronously; replay only that click after approval.
export function installWikiNavigationGuard(dirty:()=>boolean,save:()=>Promise<unknown>){
  let pending=false,disposed=false;
  let approved:MouseEvent|undefined;
  const navigation=(event:MouseEvent)=>{
    if(event===approved||!dirty())return;
    const target=event.target as HTMLElement;
    const control=target.closest<HTMLElement>('button,a,select');
    if(target.closest('.wiki-notes-editor,.feedback-dialog')||!control)return;
    event.preventDefault();event.stopImmediatePropagation();
    if(pending)return;
    pending=true;
    void confirmAction('笔记尚未发布。离开后可恢复已保存草稿，确定离开吗？').then(confirmed=>{
      if(!confirmed||disposed||!target.isConnected||control.matches(':disabled'))return;
      void save().catch(()=>{});
      approved=new MouseEvent('click',{bubbles:true,cancelable:true,composed:true,button:event.button,ctrlKey:event.ctrlKey,metaKey:event.metaKey,shiftKey:event.shiftKey,altKey:event.altKey});
      try{
        target.dispatchEvent(approved);
        if(control instanceof HTMLSelectElement){control.focus();try{control.showPicker?.();}catch{/* Keyboard selection remains available without transient activation. */}}
      }finally{approved=undefined;}
    }).finally(()=>{pending=false;});
  };
  document.addEventListener('click',navigation,true);
  return ()=>{disposed=true;document.removeEventListener('click',navigation,true);};
}
