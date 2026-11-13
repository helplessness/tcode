import StudioShell, { type StudioShellProps } from '../components/StudioShell';

export default function DesktopStudioShell(props: Omit<StudioShellProps, 'surface'>) {
  return <StudioShell {...props} surface="desktop" />;
}
