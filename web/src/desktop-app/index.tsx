import { render } from 'solid-js/web';
import { ErrorBoundary } from 'solid-js';
import 'katex/dist/katex.min.css';
import '../index.css';
import '../paper-theme.css';
import DesktopApp from './DesktopApp';

const root = document.getElementById('root');
if (root) render(() => <ErrorBoundary fallback={error => {
  console.error('Desktop UI failed to render:', error);
  return <main role="alert" style={{ padding: '40px', 'max-width': '640px', margin: 'auto' }}>
    <h1>界面加载失败</h1>
    <p>界面资源可能已更新，请重新加载。已保存的会话与交互界面仍保留在本地。</p>
    <button type="button" onClick={() => window.location.reload()}>重新加载界面</button>
  </main>;
}}><DesktopApp /></ErrorBoundary>, root);
