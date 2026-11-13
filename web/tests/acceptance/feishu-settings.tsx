import { render } from 'solid-js/web';
import SettingsWorkspace from '../../src/components/SettingsWorkspace';
import '../../src/index.css';
import '../../src/paper-theme.css';
import '../../src/interface.css';
import '../../src/studio.css';
document.documentElement.classList.add('studio-theme');
render(() => <SettingsWorkspace initialSection="feishu" workspaceId="ws1" />, document.getElementById('root')!);
