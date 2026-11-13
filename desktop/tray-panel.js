const bridge = window.tcodeTray;
const row = document.getElementById('software');
const preview = document.getElementById('preview');
const frame = document.getElementById('frame');
let hasImage = false;
let previewRequested = false;
function render(state) {
  document.getElementById('heading').classList.toggle('running', state.running);
  document.getElementById('status').textContent = state.label;
  document.getElementById('app').textContent = state.app || '暂无正在操作的软件';
  hasImage = Boolean(state.image);
  frame.hidden = !hasImage;
  showPreview(previewRequested);
  row.lastElementChild.hidden = !hasImage;
  if (state.image) frame.src = state.image; else frame.removeAttribute('src');
}
function showPreview(show) { previewRequested = show; const visible = show && hasImage; preview.hidden = !visible; row.setAttribute('aria-expanded', String(visible)); }
document.getElementById('preview-area').addEventListener('mouseenter', () => showPreview(true));
document.getElementById('preview-area').addEventListener('mouseleave', () => showPreview(false));
row.addEventListener('focus', () => showPreview(true));
row.addEventListener('blur', () => showPreview(false));
row.addEventListener('click', () => showPreview(preview.hidden));
document.getElementById('open').addEventListener('click', () => bridge.action('open'));
document.getElementById('quit').addEventListener('click', () => bridge.action('quit'));
document.addEventListener('keydown', event => { if (event.key === 'Escape') bridge.action('hide'); });
bridge.onState(render);
bridge.onHide(() => { showPreview(false); hasImage = false; frame.removeAttribute('src'); });
bridge.state().then(render);
new ResizeObserver(() => bridge.height(Math.ceil(document.getElementById('content').getBoundingClientRect().height + 16))).observe(document.getElementById('content'));
