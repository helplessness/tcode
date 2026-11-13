export const preferenceKeys = {
  compact: 'tcode.settings.compact',
  reduceMotion: 'tcode.settings.reduceMotion',
  notify: 'tcode.settings.notify',
  inAppNotify: 'tcode.settings.inAppNotify',
  mcpMode: 'tcode.settings.mcpMode',
  // v2 resets values accidentally inherited from the active conversation by the old new-session flow.
  agent: 'tcode.settings.defaultAgent.v2',
  reasoning: 'tcode.settings.reasoning',
  scheduler: 'tcode.settings.scheduler',
  officeAnimations: 'tcode.settings.officeAnimations',
  officePauseInactive: 'tcode.settings.officePauseInactive',
  officeRememberZoom: 'tcode.settings.officeRememberZoom',
  structureAgent: 'tcode.settings.structureAgent',
  structureAgentStatus: 'tcode.settings.structureAgentStatus',
  structureRepresentation: 'tcode.settings.structureRepresentation',
  structureRememberScene: 'tcode.settings.structureRememberScene',
  structureMeasurementPrecision: 'tcode.settings.structureMeasurementPrecision',
} as const;

export const readBooleanPreference = (key: string, fallback: boolean) => {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value === '1';
  } catch { return fallback; }
};

export const readPreference = <T extends string>(key: string, fallback: T): T => {
  try { return (localStorage.getItem(key) as T) || fallback; }
  catch { return fallback; }
};

export const savePreference = (key: string, value: string | boolean | number) => {
  localStorage.setItem(key, typeof value === 'boolean' ? value ? '1' : '0' : String(value));
};

export const announcePreferenceChange = () => window.dispatchEvent(new CustomEvent('tcode:settings-changed'));
