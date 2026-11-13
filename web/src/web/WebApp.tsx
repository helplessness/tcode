import App from '../App';
import WebStudioShell from './WebStudioShell';

export default function WebApp() {
  return <App surface="web" Shell={WebStudioShell} />;
}
