export type WorkspaceListPreferences = { grouped:boolean; order:string[]; pinned:string[]; collapsed:string[]; names:Record<string,string> };
export const workspaceListDefaults = ():WorkspaceListPreferences => ({grouped:false,order:[],pinned:[],collapsed:[],names:{}});
export function parseWorkspaceList(raw:string|null):WorkspaceListPreferences {
  const result=workspaceListDefaults();
  try {
    const value=JSON.parse(raw||'{}');result.grouped=value?.grouped===true;
    for(const key of ['order','pinned','collapsed'] as const)if(Array.isArray(value?.[key]))result[key]=[...new Set<string>(value[key].filter((id:unknown)=>typeof id==='string'))];
    if(value?.names&&typeof value.names==='object'&&!Array.isArray(value.names))result.names=Object.fromEntries(Object.entries(value.names).filter((entry):entry is [string,string]=>typeof entry[1]==='string'&&!!entry[1].trim()));
  }catch{/* Corrupt display preferences must never hide sessions. */}
  return result;
}
export function sessionWorkspaceKey(session:{workspace_id?:string;workdir?:string;workdir_name?:string}) {
  return session.workspace_id?`id:${session.workspace_id}`:session.workdir?`path:${session.workdir}`:session.workdir_name?`name:${session.workdir_name}`:'unassigned';
}

/** Identity-based accent stays stable across aliases, filtering and reordering. */
export function workspaceAccentHue(id: string): number {
  let hash = 2166136261;
  for (const character of id) hash = Math.imul(hash ^ character.codePointAt(0)!, 16777619);
  return (hash >>> 0) % 360;
}
