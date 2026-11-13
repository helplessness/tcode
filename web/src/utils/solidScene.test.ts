import {expect,it} from 'vitest';
import * as THREE from 'three';
import {pieceGeometry,solidMotion,projectedSpan} from './solidScene';
import type {SolidPiece,SolidScene} from '../api/canvas';
import {assemblyDirection,assemblyStepIndex,assemblySteps} from './assemblySteps';
import {timelineShare} from './canvasAnimation';
import guidedBearing from '../../../docs/IntelligentUI/examples/guided-bearing.json';
const piece:SolidPiece={id:'p',label:'轴',description:'',kind:'cylinder',position:[0,0,0],size:[8,12,4],radius:6,inner_radius:3,length:10,axis:'z',material:'alloy',order:0};
it('keeps guided steps, focus targets and continuous motion on the same boundaries',()=>{
  // Motion consumes the raw fixture's vectors; geometry defaults are applied by the server.
  const scene=guidedBearing as unknown as SolidScene,steps=assemblySteps(scene,'assemble');
  expect(steps.map(s=>s.plans.map(p=>p.id))).toEqual([['inner_group'],['shaft_group']]);
  expect(assemblyDirection(steps[1].plans[0].explode!,steps[1].sign)).toBe('Z 正向 48');
  const pose=(progress:number)=>solidMotion(scene,timelineShare(progress,'assemble',true));
  expect(pose(.25).get('inner_group')).toEqual([0,0,15]);
  expect(pose(.25).get('shaft_group')).toEqual([0,0,-48]);
  expect(assemblyStepIndex(.5,2)).toBe(0);
  expect(pose(.5).get('inner_group')).toEqual([0,0,0]);
  expect(assemblyStepIndex(.75,2)).toBe(1);
  expect(pose(.75).get('shaft_group')).toEqual([0,0,-24]);
  expect(Math.hypot(...pose(1).get('shaft_group')!)).toBe(0);
  expect(pose(0).has('outer_group')).toBe(false);
  expect(assemblySteps(scene,'roundtrip').map(s=>s.sign)).toEqual([1,1,-1,-1]);
});
it('uses finite solid geometry with world-space axes and actual hollow rings',()=>{
  for(const kind of ['block','plate','rail','cylinder','ring','bolt','knob'] as const)for(const axis of ['x','y','z'] as const){
    const geometry=pieceGeometry({...piece,kind,axis});geometry.computeBoundingBox();
    const bounds=geometry.boundingBox!;expect(bounds.isEmpty()).toBe(false);expect([...bounds.min.toArray(),...bounds.max.toArray()].every(Number.isFinite)).toBe(true);
    if(!['block','plate','rail'].includes(kind))expect(bounds.max.getComponent('xyz'.indexOf(axis))).toBeCloseTo(10,4);
    geometry.dispose();
  }
  const geometry=pieceGeometry({...piece,kind:'ring'}),mesh=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({side:THREE.DoubleSide}));mesh.updateMatrixWorld();
  expect(new THREE.Raycaster(new THREE.Vector3(0,0,30),new THREE.Vector3(0,0,-1)).intersectObject(mesh)).toHaveLength(0);
  expect(new THREE.Raycaster(new THREE.Vector3(4,0,30),new THREE.Vector3(0,0,-1)).intersectObject(mesh).length).toBeGreaterThan(0);
  geometry.dispose();
});
it('uses the same staggered source vectors for assembly and local piece motion',()=>{
  const scene:SolidScene={id:'s',title:'装配',description:'',assemblies:[{id:'base',label:'底座',description:'',explode:[0,0,0],order:0,pieces:[piece]},{id:'cover',label:'外罩',description:'',explode:[0,0,80],order:1,pieces:[{...piece,id:'lid',explode:[20,0,0]}]}]};
  expect(solidMotion(scene,0).get('cover')).toEqual([0,0,0]);
  expect(solidMotion(scene,1).get('cover')).toEqual([0,0,80]);
  expect(solidMotion(scene,.1).get('cover')).toEqual([0,0,0]);
  expect(solidMotion(scene,1,'cover').get('lid')).toEqual([20,0,0]);
});

it('occludes the lower shaft with the bearing while its upper end remains in front, independent of mesh order',()=>{
  const ring=new THREE.Mesh(pieceGeometry({...piece,kind:'ring',radius:14,inner_radius:8,length:9}),new THREE.MeshBasicMaterial());
  const shaft=new THREE.Mesh(pieceGeometry({...piece,kind:'lathe',profile:[[0,0],[0,7.8],[13,7.8],[14,7],[32,7],[33,0]]}),new THREE.MeshBasicMaterial());
  shaft.position.z=-14;
  ring.updateMatrixWorld();shaft.updateMatrixWorld();
  const direction=new THREE.Vector3(1,1,.85).normalize();
  for(const meshes of [[shaft,ring],[ring,shaft]]){
    for(const [z,expected] of [[-8,ring],[16,shaft]] as const){
      const point=new THREE.Vector3(0,0,z),ray=new THREE.Raycaster(point.clone().addScaledVector(direction,100),direction.clone().negate());
      expect(ray.intersectObjects(meshes,false)[0]?.object).toBe(expected);
    }
  }
  const ball=pieceGeometry({...piece,kind:'sphere',radius:3.5});ball.computeBoundingBox();
  expect(ball.boundingBox!.min.z).toBeCloseTo(-3.5);expect(ball.boundingBox!.max.z).toBeCloseTo(3.5);
  ring.geometry.dispose();shaft.geometry.dispose();ball.dispose();
});

it('fits projected solid bounds at narrow and wide aspect ratios after camera rotation',()=>{
  const box=new THREE.Box3(new THREE.Vector3(-50,-30,0),new THREE.Vector3(100,40,120));
  for(const aspect of [.4,1,1.8])for(const angle of [0,.7,1.4]){
    const camera=new THREE.OrthographicCamera();camera.up.set(0,0,1);const center=box.getCenter(new THREE.Vector3());camera.position.copy(center).add(new THREE.Vector3(Math.cos(angle)*100,Math.sin(angle)*100,80));camera.lookAt(center);
    const span=projectedSpan(box,camera.quaternion,aspect),inverse=camera.quaternion.clone().invert();
    for(const x of [box.min.x,box.max.x])for(const y of [box.min.y,box.max.y])for(const z of [box.min.z,box.max.z]){
      const point=new THREE.Vector3(x,y,z).sub(center).applyQuaternion(inverse);expect(Math.abs(point.x)).toBeLessThan(span*aspect);expect(Math.abs(point.y)).toBeLessThan(span);
    }
  }
});

it('moves nested pieces on the whole timeline even when every assembly stays fixed',()=>{
  const scene:SolidScene={id:'s',title:'装配',description:'',assemblies:[{id:'base',label:'底座',description:'',explode:[0,0,0],order:0,pieces:[piece,{...piece,id:'shaft',explode:[0,0,140],order:1}]},{id:'bearings',label:'轴承组',description:'',explode:[0,0,0],order:0,pieces:[{...piece,id:'bearing',explode:[40,0,75],order:2}]}]};
  const offsets=solidMotion(scene,1);
  expect(offsets.get('base')).toEqual([0,0,0]);
  expect(offsets.get('shaft')).toEqual([0,0,140]);
  expect(offsets.get('bearing')).toEqual([40,0,75]);
  expect(solidMotion(scene,0).get('shaft')).toEqual([0,0,0]);
  expect(solidMotion(scene,.2).get('shaft')![2]).toBeGreaterThan(0);
  expect(solidMotion(scene,.2).get('bearing')).toEqual([0,0,0]);
  scene.assemblies[0].explode=[10,0,0];
  expect(solidMotion(scene,1).get('base')).toEqual([10,0,0]);
});
