import StudioShell, { type StudioShellProps } from '../components/StudioShell';

export default function WebStudioShell(props: Omit<StudioShellProps, 'surface'>) {
  return <StudioShell {...props} surface="web" />;
}
