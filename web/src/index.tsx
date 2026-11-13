import { render } from 'solid-js/web';
import WebApp from './web/WebApp';
import 'katex/dist/katex.min.css';
import './index.css';
import './paper-theme.css';

const root = document.getElementById('root');
if (root) {
  render(() => <WebApp />, root);
}
