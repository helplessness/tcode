import { render } from 'solid-js/web';
import AsyncCompletionNotifications from '../../src/components/AsyncCompletionNotifications';
import '../../src/index.css';
import '../../src/paper-theme.css';

document.documentElement.classList.add('studio-theme');
render(() => <main style={{padding: '24px'}}>异步通知去重验收<AsyncCompletionNotifications /></main>, document.getElementById('root')!);
