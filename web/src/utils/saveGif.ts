// A native save picker may outlive the renderer's click handler by minutes.
// Retain its Blob until Electron reports completion/cancellation, not a timer.
export function saveGif(blob: Blob, title: string): Promise<'saved' | 'cancelled' | 'handed-off'> {
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url;
  link.download = `${title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0,80) || '动画'}.gif`;
  return new Promise((resolve, reject) => {
    let unsubscribe: (() => void) | undefined;
    const release = () => { unsubscribe?.(); URL.revokeObjectURL(url); };
    try {
      const subscribe = window.tcodeDesktop?.onDownloadResult;
      if (subscribe) unsubscribe = subscribe(result => {
        if (result.url !== url) return;
        release();
        if (result.state === 'completed') resolve('saved');
        else if (result.state === 'cancelled') resolve('cancelled');
        else reject(Error('GIF未能保存，请检查目标目录后重试。'));
      });
      document.body.append(link); link.click();
      if (!subscribe) {
        // Browsers expose no download-completion event. Keep a bounded lease;
        // report hand-off honestly rather than claiming a file was saved.
        setTimeout(release, 300000);
        resolve('handed-off');
      }
    } catch (error) { release(); reject(error); }
    finally { link.remove(); }
  });
}
