import { onCleanup, onMount, type JSX } from 'solid-js';
import { Portal } from 'solid-js/web';
import './ResearchDialog.css';

export default function ResearchDialog(props: { title: string; onClose: () => void; busy?: boolean; children: JSX.Element; portal?: boolean }) {
  let dialog!: HTMLDialogElement;
  // Open after the portal has attached; moving an open dialog can lose top-layer state.
  onMount(() => queueMicrotask(() => { if(dialog.isConnected && !dialog.open)dialog.showModal(); }));
  onCleanup(() => dialog.close());
  const content = <dialog ref={dialog} class="research-dialog" aria-label={props.title}
    onCancel={event => { event.preventDefault(); if (!props.busy) props.onClose(); }}>
    <header class="research-dialog-header"><h2>{props.title}</h2>
      <button type="button" disabled={props.busy} onClick={props.onClose}>返回</button>
    </header>
    <div class="research-dialog-content">{props.children}</div>
  </dialog>;
  return props.portal===false ? content : <Portal>{content}</Portal>;
}
