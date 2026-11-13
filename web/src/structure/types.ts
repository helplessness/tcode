export type StructureSelector = {
  target: 'all' | 'selection' | 'residues' | 'chain' | 'ligand';
  chain?: string; residues?: number[]; insertion_code?: string; model_index?: number;
  ligand_name?: string; radius?: number;
};
export type ViewCommand = {
  id?: string; expires_at?: number;
  action: 'inspect' | 'open' | 'select' | 'focus' | 'representation' | 'color' | 'rotate' | 'zoom' | 'reset' | 'clear' | 'undo' | 'measure' | 'clear_measurements';
  expected_sha256?: string; path?: string; selector?: StructureSelector;
  representation?: 'cartoon' | 'ball-and-stick' | 'molecular-surface' | 'spacefill' | 'line';
  color?: string; axis?: 'x' | 'y' | 'z'; degrees?: number; factor?: number;
  atoms?: MeasurementAtom[];
};
export type MeasurementAtom = { chain: string; residue_number: number; atom_name?: string; model_index?: number; insertion_code?: string; alt_id?: string; operator?: string };
export type DistanceMeasurement = { id: string; distance_angstrom: number; atoms: MeasurementAtom[]; positions: number[][] };
export type Residue = { model_index: number; chain: string; residue_number: number; insertion_code: string; name: string; operator: string };
export type ViewState = {
  path?: string; sha256?: string; ready: boolean; atoms?: number;
  chains?: { model_index: number; chain: string; first: number; last: number }[];
  selection?: Residue[]; selected_atoms?: number; selection_truncated?: boolean;
  measurements?: DistanceMeasurement[];
  camera?: { position: number[]; target: number[]; up: number[]; radius: number };
  error?: string;
};
export type ViewerApi = {
  load: (text: string, format: string, path: string, sha256: string) => Promise<void>;
  state: () => ViewState;
  run: (command: ViewCommand) => Promise<void>;
  removeResidue: (residue: Residue) => void;
  controls: () => void;
  resize: () => void;
  save: () => unknown;
  restore: (saved: unknown) => void;
  dispose: () => void;
};
export type ViewerWindow = Window & { structureViewer?: Promise<ViewerApi> };
