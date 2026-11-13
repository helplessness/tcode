/** TCode adapter. Upstream geometry remains unmodified; see ../LICENSE. */
import { writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { slabOf, depthOf, iso, axisVector } from '../kit/iso-kit.mjs';
import { frameAlong, lathe, ringBand, sphereOf } from '../kit/lathe.mjs';

const fail = message => { throw new Error(message); };
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const fields = (value, allowed) => {
  if (!object(value) || Object.keys(value).some(key => !allowed.includes(key))) fail('不支持的字段');
};
const number = (value, min, max) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) fail('几何数值超出范围');
  return value;
};
const vector = (value, bound = 200) => {
  if (!Array.isArray(value) || value.length !== 3) fail('需要三个坐标');
  return value.map(n => number(n, -bound, bound));
};
const text = (value, limit, required = false) => {
  if (typeof value !== 'string' || value.length > limit || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value) || (required && !value.trim())) fail('名称或说明无效');
  return value;
};
const xml = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[ch]));
const identifiers = new Set(['anatomy_svg']);
const identifier = value => {
  if (typeof value !== 'string' || !/^[A-Za-z_][A-Za-z0-9_-]{0,95}$/.test(value) || identifiers.has(value)) fail('部件标识无效或重复');
  identifiers.add(value); return value;
};
const palettes = {
  alloy: ['#eef0f2','#cdd3d9','#98a4b0','#637282'],
  polymer: ['#f1eee7','#d9d4c9','#b2afa5','#797d7b'],
  rubber: ['#646974','#484f5b','#353e4b','#202a38'],
  accent: ['#dce6d9','#a8bda4','#7e997a','#4e6c52'],
};
function paint(paths, colors, style) {
  const [top, light, mid, dark] = colors;
  const path = (d, fill, stroke = 'none', width = .55) => d ? `<path d="${xml(d)}" fill="${fill}" stroke="${stroke}" stroke-width="${width}" stroke-linejoin="round"/>` : '';
  return path(paths.fill, light) + paths.shades.map((d, i) => path(d, [dark, mid, light, top][i], paths.seamless ? [dark,mid,light,top][i] : 'none', .3)).join('')
    + path(paths.top, top, paths.seamless ? top : 'none', .3)
    + path(paths.crease, 'none', mid, .4) + path(paths.bevel, 'none', top, .65)
    + path(paths.outline, 'none', style === 'manual' ? '#45544e' : dark, style === 'manual' ? .8 : .5);
}

export function render(scene, style = 'clay') {
  identifiers.clear(); identifiers.add('anatomy_svg');
  if (!['clay','manual'].includes(style)) fail('未知风格，请使用 canvas.style 返回的风格');
  fields(scene, ['id','title','description','assemblies','animation']);
  const id = identifier(scene.id || 'anatomy_device');
  const title = text(scene.title, 200, true);
  const description = text(scene.description || '教学示意，不代表工程验证。', 2000);
  if (!Array.isArray(scene.assemblies) || scene.assemblies.length < 1 || scene.assemblies.length > 12) fail('需要 1–12 个组件');
  const projection = { origin:[0,0], scale:1, azimuth:45, elevation:30 };
  const parts = [{element_id:id,label:title,description,illustration:'isometric'}];
  const bounds = [];
  let count = 0;
  const groups = scene.assemblies.map(assembly => {
    fields(assembly, ['id','label','description','explode','order','pieces']);
    const aid = identifier(assembly.id);
    const label = text(assembly.label,120,true);
    const explain = text(assembly.description || '',1000);
    const order = number(assembly.order ?? 0,0,11);
    if (!Number.isInteger(order)) fail('拆解顺序必须是整数');
    const world = vector(assembly.explode || [0,0,0],160);
    const [dx,dy] = axisVector(projection,world);
    if (Math.hypot(...world) > .01 && Math.hypot(dx,dy) < .01) fail('拆解方向在当前视角不可见');
    parts.push({element_id:aid,label,description:explain,explode:{x:dx,y:dy,order}});
    if (!Array.isArray(assembly.pieces) || assembly.pieces.length < 1 || assembly.pieces.length > 12) fail('每组需要 1–12 个零件');
    const bodies = assembly.pieces.map(piece => {
      if (++count > 48) fail('总零件数不得超过48');
      fields(piece,['id','label','description','kind','position','size','radius','inner_radius','length','axis','profile','material','color']);
      const pid = identifier(piece.id);
      parts.push({element_id:pid,label:text(piece.label,120,true),description:text(piece.description || '',1000)});
      const position = vector(piece.position || [0,0,0]);
      const radius = number(piece.radius ?? 10, .5,80);
      const length = number(piece.length ?? 20, .5,160);
      const axis = piece.axis || 'z';
      if (!['x','y','z'].includes(axis)) fail('轴向必须是 x、y 或 z');
      const unit = {x:[1,0,0],y:[0,1,0],z:[0,0,1]}[axis];
      const frame = frameAlong(position,unit);
      let paths, low, high;
      if (piece.kind === 'slab') {
        const size = vector(piece.size || [30,20,6]);
        size.forEach(n => number(n,.5,160));
        paths = slabOf({x:position[0],y:position[1],w:size[0],d:size[1],r:Math.min(2,size[0]/5,size[1]/5)},position[2],size[2],projection,4,.6);
        low = position; high = position.map((n,i) => n+size[i]);
      } else if (piece.kind === 'sphere') {
        paths = sphereOf(position,radius,projection,{steps:24});
        low = position.map(n => n-radius); high = position.map(n => n+radius);
      } else if (piece.kind === 'ring') {
        const inner = number(piece.inner_radius ?? radius*.55,.1,radius-.1);
        paths = ringBand(0,length,inner,radius,frame,projection,{steps:24});
        low = position.map((n,i) => n-(unit[i] ? 0 : radius));
        high = position.map((n,i) => n+(unit[i] ? length : radius));
      } else if (piece.kind === 'lathe') {
        const profile = piece.profile || [[0,0],[0,radius*.85],[1,radius],[length-1,radius],[length,radius*.85],[length,0]];
        if (!Array.isArray(profile) || profile.length < 2 || profile.length > 16) fail('回转轮廓需要2–16个点');
        let previous = -Infinity;
        profile.forEach(pair => {
          if (!Array.isArray(pair) || pair.length !== 2) fail('轮廓点需要[轴向距离,半径]');
          number(pair[0],0,160); number(pair[1],0,80);
          if (pair[0] < previous) fail('轮廓轴向距离必须递增');
          previous = pair[0];
        });
        const r = Math.max(...profile.map(p => p[1]));
        if (r <= 0 || previous <= profile[0][0]) fail('回转轮廓需要可见的半径和长度');
        paths = lathe(profile,frame,projection,{steps:24,bevel:.4});
        low = position.map((n,i) => n+(unit[i] ? profile[0][0] : -r));
        high = position.map((n,i) => n+(unit[i] ? previous : r));
      } else fail('支持 slab、lathe、ring、sphere；真实三维请用 canvas.compose');
      const material = piece.material || 'alloy';
      if (!Object.hasOwn(palettes, material)) fail('未知材质');
      let colors = palettes[material];
      if (piece.color !== undefined) {
        if (typeof piece.color !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(piece.color)) fail('颜色需要六位十六进制值');
        const rgb = [1,3,5].map(i => parseInt(piece.color.slice(i,i+2),16));
        colors = [.8,1,.78,.55].map((k,i) => '#' + rgb.map(n => Math.round(i === 0 ? n+(255-n)*.45 : n*k).toString(16).padStart(2,'0')).join(''));
      }
      for (const x of [low[0],high[0]]) for (const y of [low[1],high[1]]) for (const z of [low[2],high[2]]) bounds.push(iso([x,y,z],projection));
      const centre = low.map((n,i) => (n+high[i])/2);
      return {depth:depthOf(centre,projection),source:`<g id="${pid}">${paint(paths,colors,style)}</g>`};
    });
    bodies.sort((a,b) => a.depth-b.depth);
    return {depth:bodies.reduce((n,b) => n+b.depth,0)/bodies.length,source:`<g id="${aid}">${bodies.map(b => b.source).join('')}</g>`};
  });
  if (scene.assemblies.length >= 2) {
    const animation = scene.animation || {duration_seconds:6,mode:'roundtrip'};
    fields(animation,['duration_seconds','mode']);
    const duration = number(animation.duration_seconds ?? 6,1,60);
    const mode = animation.mode || 'roundtrip';
    if (!['explode','assemble','roundtrip'].includes(mode)) fail('未知播放模式');
    parts[0].animation = {duration_seconds:duration,mode};
  } else if (scene.animation) fail('时间轴至少需要两个组件');
  groups.sort((a,b) => a.depth-b.depth);
  const x = Math.min(...bounds.map(p => p[0]))-12, y = Math.min(...bounds.map(p => p[1]))-12;
  const w = Math.max(...bounds.map(p => p[0]))-x+12, h = Math.max(...bounds.map(p => p[1]))-y+12;
  const box = [x,y,w,h].map(n => Number(n.toFixed(2))).join(' ');
  const source = `<svg xmlns="http://www.w3.org/2000/svg" id="anatomy_svg" viewBox="${box}"><title>${xml(title)}</title><g id="${id}">${groups.map(g => g.source).join('')}</g></svg>`;
  if (Buffer.byteLength(source)>160000) fail('图形过大，请减少轮廓点或零件并重试');
  // Preserve the shared world geometry, not just independently painted SVG
  // groups. The platform canonicalizes this scene and both views use depth.
  const spatial = {id,title,description,assemblies:scene.assemblies.map(group => ({
    id:group.id,label:group.label,description:group.description || '',
    explode:group.explode || [0,0,0],order:group.order ?? 0,
    pieces:group.pieces.map(piece => {
      const radius=piece.radius ?? 10,length=piece.length ?? 20;
      return {...piece,kind:piece.kind === 'slab' ? 'block' : piece.kind,
        description:piece.description || '',position:piece.position || [0,0,0],
        radius,length,axis:piece.axis || 'z',material:piece.material || 'alloy',
        ...(piece.kind === 'slab' ? {size:piece.size || [30,20,6]} : {}),
        ...(piece.kind === 'ring' ? {inner_radius:piece.inner_radius ?? radius*.55} : {}),
        ...(piece.kind === 'lathe' ? {profile:piece.profile || [[0,0],[0,radius*.85],[1,radius],[length-1,radius],[length,radius*.85],[length,0]]} : {})};
    }),
  })),...(parts[0].animation ? {animation:parts[0].animation} : {})};
  return {title,source,parts,style,scene:spatial};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [major, minor] = process.versions.node.split('.').map(Number);
    if (major < 20 || (major === 20 && minor < 10)) fail('此绘图动作需要 Node.js 20.10 或更高版本');
    const args = process.argv.slice(2);
    if (args.length%2 || args.some((v,i) => i%2===0 && !['--scene','--style','--output'].includes(v))) fail('参数无效');
    const options = Object.fromEntries(Array.from({length:args.length/2},(_,i) => [args[i*2],args[i*2+1]]));
    if (!options['--scene'] || options['--scene'].length>24000) fail('需要小于24000字符的场景JSON');
    const result = render(JSON.parse(options['--scene']),options['--style'] || 'clay');
    if (!options['--output']) fail('需要输出JSON路径');
    const document = JSON.stringify(result);
    writeFileSync(options['--output'],document);
    const digest = createHash('sha256').update(document).digest('hex');
    // Large SVG remains in the declared artifact, avoiding a giant tool reply.
    console.log(JSON.stringify({title:result.title,parts:result.parts.length,source_bytes:Buffer.byteLength(result.source),artifact_digest:digest,next:'使用平台回执的output_path作为artifact_path，先canvas.check再canvas.create，并传artifact_digest；不要使用沙箱内部路径，无需读取或转写SVG。'}));
  } catch (error) { console.error(error.message); process.exitCode=1; }
}
