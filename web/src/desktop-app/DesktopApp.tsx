import App from '../App';
import '../desktop';
import DesktopStudioShell from './DesktopStudioShell';

export default function DesktopApp() {
  return <App surface="desktop" Shell={DesktopStudioShell} />;
}
