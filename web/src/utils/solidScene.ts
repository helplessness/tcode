import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import type { CanvasAnimation, SolidPiece, SolidScene } from '../api/canvas';
import { explosionPortion, timelineShare, CANVAS_GIF_WIDTH, CANVAS_GIF_HEIGHT } from './canvasAnimation';

import { solidPlans } from './assemblySteps';

const colors={alloy:'#cdd4d5',polymer:'#dfddd4',rubber:'#394442',accent:'#93ac8e'};
export function pieceGeometry(piece:SolidPiece):THREE.BufferGeometry {
  if(piece.kind==='sphere')return new THREE.SphereGeometry(piece.radius,32,24);
  if(['plate','block','rail'].includes(piece.kind)){
    const geometry=new RoundedBoxGeometry(piece.size[0],piece.size[1],piece.size[2],2,Math.min(...piece.size)/12);
    geometry.translate(...piece.size.map(n=>n/2) as [number,number,number]);return geometry;
  }
  let geometry:THREE.BufferGeometry;
  if(piece.kind==='lathe'){
    const profile=piece.profile!;
    // LatheGeometry revolves around Y. Close open ends on the axis and rotate
    // into our axial-start Z convention before applying the world axis.
    const points=profile.map(([s,r])=>new THREE.Vector2(r,s));
    if(points[0].x!==0)points.unshift(new THREE.Vector2(0,points[0].y));
    if(points[points.length-1].x!==0)points.push(new THREE.Vector2(0,points[points.length-1].y));
    geometry=new THREE.LatheGeometry(points,64);geometry.rotateX(Math.PI/2);
  }else if(piece.kind==='ring'){
    const shape=new THREE.Shape();shape.absarc(0,0,piece.radius,0,Math.PI*2,false);
    const hole=new THREE.Path();hole.absarc(0,0,piece.inner_radius,0,Math.PI*2,true);shape.holes.push(hole);
    geometry=new THREE.ExtrudeGeometry(shape,{depth:piece.length,bevelEnabled:false,curveSegments:40,steps:1});
  }else{
    geometry=new THREE.CylinderGeometry(piece.radius,piece.radius,piece.length,piece.kind==='bolt'?6:48);
    geometry.rotateX(Math.PI/2);geometry.translate(0,0,piece.length/2);
  }
  if(piece.axis==='x')geometry.rotateY(Math.PI/2);
  if(piece.axis==='y')geometry.rotateX(-Math.PI/2);
  return geometry;
}
function pieceDetails(piece:SolidPiece){
  const points:THREE.Vector3[]=[];
  const segment=(a:number[],b:number[])=>points.push(new THREE.Vector3(...a as [number,number,number]),new THREE.Vector3(...b as [number,number,number]));
  if(piece.kind==='plate'&&Math.min(piece.size[0],piece.size[1])>=12){
    const [w,d,h]=piece.size;
    for(const [x,y] of [[3,3],[w-3,3],[w-3,d-3],[3,d-3]]){
      for(let i=0;i<12;i++){const a=i*Math.PI/6,b=(i+1)*Math.PI/6;segment([x+Math.cos(a)*1.3,y+Math.sin(a)*1.3,h+.02],[x+Math.cos(b)*1.3,y+Math.sin(b)*1.3,h+.02]);}
      segment([x-.8,y,h+.02],[x+.8,y,h+.02]);
    }
  }else if(piece.kind==='rail'){
    for(const y of [.45,.55])segment([0,piece.size[1]*y,piece.size[2]+.02],[piece.size[0],piece.size[1]*y,piece.size[2]+.02]);
  }else if(piece.kind==='knob'||piece.kind==='bolt'){
    segment([-piece.radius*.6,0,piece.length+.02],[piece.radius*.6,0,piece.length+.02]);
    if(piece.kind==='knob')for(let i=0;i<24;i++){const a=i*Math.PI/12;segment([Math.cos(a)*piece.radius,Math.sin(a)*piece.radius,piece.length*.15],[Math.cos(a)*piece.radius,Math.sin(a)*piece.radius,piece.length*.85]);}
  }
  const geometry=new THREE.BufferGeometry().setFromPoints(points);
  if(!['plate','rail','block'].includes(piece.kind)){if(piece.axis==='x')geometry.rotateY(Math.PI/2);if(piece.axis==='y')geometry.rotateX(-Math.PI/2);}
  return new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({color:'#506057',transparent:true,opacity:.55}));
}
export function solidMotion(scene:SolidScene,share:number,target=scene.id){
  const values=solidPlans(scene,target).filter(p=>!scene.bore_fits||p.explode?.some(Boolean));
  const orders=[...new Set(values.map(g=>g.order))].sort((a,b)=>a-b);
  return new Map(values.map(g=>[g.id,(g.explode||[0,0,0]).map(n=>n*(scene.bore_fits?Math.max(0,Math.min(1,share*orders.length-orders.indexOf(g.order))):explosionPortion(share,orders.indexOf(g.order)*.25,(orders.length-1)*.25)))]));
}
export function projectedSpan(box:THREE.Box3,orientation:THREE.Quaternion,aspect:number){
  const center=box.getCenter(new THREE.Vector3()),inverse=orientation.clone().invert();let x=0,y=0;
  for(const a of [box.min.x,box.max.x])for(const b of [box.min.y,box.max.y])for(const c of [box.min.z,box.max.z]){
    const point=new THREE.Vector3(a,b,c).sub(center).applyQuaternion(inverse);x=Math.max(x,Math.abs(point.x));y=Math.max(y,Math.abs(point.y));
  }
  return Math.max(8,y,x/Math.max(.01,aspect))*1.1;
}
export type SolidView=ReturnType<typeof createSolidView>;
export function createSolidView(canvas:HTMLCanvasElement,source:SolidScene,onSelect?:(id:string)=>void,interactive=true){
  const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:false,preserveDrawingBuffer:!interactive});
  renderer.setPixelRatio(interactive?Math.min(devicePixelRatio||1,2):1);
  renderer.setClearColor('#f6f7f4');renderer.outputColorSpace=THREE.SRGBColorSpace;
  const scene=new THREE.Scene(),world=new THREE.Group();scene.add(world);
  scene.add(new THREE.HemisphereLight('#ffffff','#8e9a93',2.5));
  const key=new THREE.DirectionalLight('#fff9ed',3.5);key.position.set(160,-120,240);scene.add(key);
  const fill=new THREE.DirectionalLight('#edf4ff',1.4);fill.position.set(-140,90,70);scene.add(fill);
  const groups=new Map<string,THREE.Group>(),meshes:THREE.Mesh[]=[];
  for(const group of source.assemblies){
    const assembly=new THREE.Group();assembly.name=group.id;groups.set(group.id,assembly);world.add(assembly);
    for(const piece of group.pieces){
      const node=new THREE.Group();node.position.fromArray(piece.position);groups.set(piece.id,node);assembly.add(node);
      const material=new THREE.MeshStandardMaterial({color:piece.color&&piece.color!=='none'&&piece.color!=='transparent'?piece.color:colors[piece.material],metalness:piece.material==='alloy'?.5:.1,roughness:piece.material==='rubber'?.8:.36});
      const mesh=new THREE.Mesh(pieceGeometry(piece),material);mesh.userData={id:piece.id,group:group.id};node.add(mesh);meshes.push(mesh);mesh.add(pieceDetails(piece));
      if(piece.color==='none'||piece.color==='transparent')mesh.visible=false;
      // Contours preserve legibility without pretending shadows are geometry.
      const edges=new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry,32),new THREE.LineBasicMaterial({color:'#506057',transparent:true,opacity:.25}));mesh.add(edges);
    }
  }
  const rest=new THREE.Box3().setFromObject(world),extent=rest.clone();
  // Include each independent plan's interval, including opposite nested
  // directions whose intermediate position can exceed both endpoint poses.
  for(const group of source.assemblies){
    for(const piece of group.pieces){
      const box=new THREE.Box3().setFromObject(groups.get(piece.id)!);
      box.min.add(new THREE.Vector3(...group.explode.map((v,i)=>Math.min(0,v)+Math.min(0,piece.explode?.[i]||0)) as [number,number,number]));
      box.max.add(new THREE.Vector3(...group.explode.map((v,i)=>Math.max(0,v)+Math.max(0,piece.explode?.[i]||0)) as [number,number,number]));
      extent.union(box);
    }
  }
  const center=rest.getCenter(new THREE.Vector3()),size=rest.getSize(new THREE.Vector3());
  let span=Math.max(size.length()*.62,30),width=1,height=1;
  const camera=new THREE.OrthographicCamera(-span,span,span,-span,.1,10000);camera.up.set(0,0,1);
  camera.position.copy(center).add(new THREE.Vector3(1,1,.85).normalize().multiplyScalar(1000));camera.lookAt(center);
  const controls=interactive?new OrbitControls(camera,canvas):undefined;
  if(controls){controls.target.copy(center);controls.enablePan=false;controls.minZoom=.3;controls.maxZoom=4;controls.update();}
  let fixedProjection=false;
  const draw=()=>renderer.render(scene,camera);
  let fitBox:THREE.Box3|undefined;
  const resize=(w:number,h:number)=>{const nextWidth=Math.max(1,w),nextHeight=Math.max(1,h);if(width!==nextWidth||height!==nextHeight)renderer.setSize(nextWidth,nextHeight,false);width=nextWidth;height=nextHeight;const ratio=width/height;span=projectedSpan(fitBox||new THREE.Box3().setFromObject(world),camera.quaternion,ratio);camera.left=-span*ratio;camera.right=span*ratio;camera.top=span;camera.bottom=-span;camera.updateProjectionMatrix();draw();};
  controls?.addEventListener('change',()=>resize(width,height));
  const motion=(value:number,identifier=source.id)=>{
    const offsets=solidMotion(source,value,identifier);
    for(const group of source.assemblies){
      groups.get(group.id)!.position.fromArray(offsets.get(group.id)||[0,0,0]);
      for(const piece of group.pieces){const offset=offsets.get(piece.id)||[0,0,0];groups.get(piece.id)!.position.fromArray(piece.position.map((v,i)=>v+offset[i]));}
    }
    // Keep the moving object in view without changing orientation or zoom.
    const box=new THREE.Box3().setFromObject(world);
    if(interactive&&!fixedProjection){const midpoint=box.getCenter(new THREE.Vector3());const direction=camera.position.clone().sub(controls!.target);controls!.target.copy(midpoint);camera.position.copy(midpoint).add(direction);resize(width,height);}
    else draw();
  };
  const select=(selection:string|string[])=>{const ids=Array.isArray(selection)?selection:selection?[selection]:[];for(const mesh of meshes){const relevant=!ids.length||ids.includes(source.id)||ids.includes(mesh.userData.id)||ids.includes(mesh.userData.group);const material=mesh.material as THREE.MeshStandardMaterial;material.transparent=!relevant;material.opacity=relevant?1:.18;material.depthWrite=relevant;}draw();};
  let down:{x:number;y:number}|undefined;
  const pointerDown=(event:PointerEvent)=>{down={x:event.clientX,y:event.clientY};};
  const pointerUp=(event:PointerEvent)=>{if(!down||Math.hypot(event.clientX-down.x,event.clientY-down.y)>5)return;down=undefined;const rect=canvas.getBoundingClientRect();const ray=new THREE.Raycaster();ray.setFromCamera(new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1),camera);const hit=ray.intersectObjects(meshes,false).find(hit=>hit.object.visible);if(hit)onSelect?.(hit.object.userData.id);};
  if(interactive){canvas.addEventListener('pointerdown',pointerDown);canvas.addEventListener('pointerup',pointerUp);}
  const reset=()=>{camera.zoom=1;const middle=(fixedProjection?extent:new THREE.Box3().setFromObject(world)).getCenter(new THREE.Vector3());camera.position.copy(middle).add(new THREE.Vector3(1,1,.85).normalize().multiplyScalar(1000));controls?.target.copy(middle);camera.lookAt(middle);controls?.update();resize(width,height);};
  const fixed=(value:boolean)=>{if(value===fixedProjection)return;fixedProjection=value;fitBox=value?extent:undefined;if(controls)controls.enableRotate=!value;if(value)reset();};
  const rotate=(horizontal:number,vertical=0)=>{const middle=controls?.target||center;const offset=camera.position.clone().sub(middle);const spherical=new THREE.Spherical().setFromVector3(offset.clone().applyAxisAngle(new THREE.Vector3(1,0,0),Math.PI/2));spherical.theta+=horizontal;spherical.phi=THREE.MathUtils.clamp(spherical.phi+vertical,.12,Math.PI-.12);offset.setFromSpherical(spherical).applyAxisAngle(new THREE.Vector3(1,0,0),-Math.PI/2);camera.position.copy(middle).add(offset);camera.lookAt(middle);controls?.update();draw();};
  const dispose=()=>{controls?.dispose();canvas.removeEventListener('pointerdown',pointerDown);canvas.removeEventListener('pointerup',pointerUp);scene.traverse(object=>{if(object instanceof THREE.Mesh||object instanceof THREE.LineSegments){object.geometry.dispose();const materials=Array.isArray(object.material)?object.material:[object.material];materials.forEach(m=>m.dispose());}});renderer.dispose();renderer.forceContextLoss();};
  const capture=(animation:CanvasAnimation)=>{
    const output=document.createElement('canvas'),offscreen=createSolidView(output,structuredClone(source),undefined,false);
    offscreen.setCamera(camera.position.clone().sub(controls?.target||center),1,extent);
    offscreen.resize(CANVAS_GIF_WIDTH,520);
    const frame=document.createElement('canvas');frame.width=CANVAS_GIF_WIDTH;frame.height=CANVAS_GIF_HEIGHT;const context=frame.getContext('2d')!;
    return {render:async(progress:number,signal:AbortSignal)=>{signal.throwIfAborted();offscreen.motion(timelineShare(progress,animation.mode,!!source.bore_fits));context.fillStyle='#f6f7f4';context.fillRect(0,0,frame.width,frame.height);context.drawImage(output,0,76);context.fillStyle='#26332e';context.font='22px system-ui';context.fillText([...source.title].slice(0,36).join(''),32,36);context.font='14px system-ui';context.fillText(`${(progress*100).toFixed(1)}% · ${(progress*animation.duration_seconds).toFixed(1)} / ${animation.duration_seconds} 秒`,32,60);source.assemblies.slice(0,6).forEach((group,i)=>context.fillText(group.label,32+(i%3)*300,624+Math.floor(i/3)*25));context.fillStyle='#68776f';context.font='12px system-ui';context.fillText('受控三维拆解示意 · 不代表工程装配验证',32,696);return context.getImageData(0,0,frame.width,frame.height).data;},dispose:offscreen.dispose};
  };
  const setCamera=(direction:THREE.Vector3,zoom:number,box:THREE.Box3)=>{const middle=box.getCenter(new THREE.Vector3());fitBox=box.clone();camera.position.copy(middle).add(direction.normalize().multiplyScalar(1000));camera.zoom=zoom;camera.lookAt(middle);camera.updateProjectionMatrix();};
  return {resize,motion,select,rotate,reset,dispose,capture,setCamera,fixed};
}
