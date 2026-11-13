import { createSignal } from 'solid-js';
import { render } from 'solid-js/web';
import DesktopServices from '../../src/components/DesktopServices';
import SettingsWorkspace from '../../src/components/SettingsWorkspace';
import '../../src/index.css';
import '../../src/paper-theme.css';
import '../../src/interface.css';
import '../../src/studio.css';

// Isolated fixture: never contacts or changes configured production services.
const services = Object.fromEntries(Array.from({ length: 24 }, (_, i) => [`store-${i + 1}`, {
  name: `Artifact 文件服务 ${i + 1}`, url: `https://artifacts-${i + 1}.example.test`, token_env: '',
}]));
let config = { artifact_services: services, artifact_default_service_id: 'store-1', artifact_target_services: {} };
window.fetch = async (input, init) => {
  const url = String(input);
  let body: unknown = {};
  if (url === '/api/artifact-services/config') {
    if (init?.method === 'PUT') config = JSON.parse(String(init.body));
    body = { ...config, saved: true, restart_required: true, can_edit: true };
  } else if (url === '/api/artifact-services') {
    body = { artifacts: Object.entries(config.artifact_services).map(([service_id, entry], i) => ({
      ...entry, service_id, state: i === 2 ? 'disconnected' : 'connected', default: service_id === config.artifact_default_service_id,
    })) };
  } else if (url.includes('models')) body = { models: [] };
  else if (url.includes('agents')) body = [];
  else if (url.includes('config')) body = { servers: [] };
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
};
document.documentElement.classList.add('studio-theme');
function Fixture() {
  const [section, setSection] = createSignal<'desktop' | 'mcp'>('desktop');
  return <><header class="studio-topbar"><div class="studio-tools"><DesktopServices onOpenSettings={setSection}/></div></header>
    <SettingsWorkspace initialSection={section()} /></>;
}
render(() => <Fixture />, document.getElementById('root')!);
