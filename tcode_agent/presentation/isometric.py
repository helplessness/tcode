"""Small deterministic isometric parts kit, inspired by Anatomy craft principles.

This module implements original geometry. The separately installed Anatomy skill
bundles upstream SVG geometry; it does not replace this controlled 3D scene model.
"""
from __future__ import annotations

import math
from xml.etree import ElementTree as ET
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canvas import ID, NS, Part, Explosion, CanvasAnimation, CanvasError, validate_svg

KINDS = ('plate', 'block', 'cylinder', 'ring', 'rail', 'bolt', 'knob', 'lathe', 'sphere')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Piece(Strict):
    id: str = Field(pattern=ID.pattern)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=1000)
    kind: Literal['plate', 'block', 'cylinder', 'ring', 'rail', 'bolt', 'knob', 'lathe', 'sphere']
    position: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0], min_length=3, max_length=3)
    size: list[float] = Field(default_factory=lambda: [24.0, 24.0, 8.0], min_length=3, max_length=3)
    radius: float = Field(default=10.0, ge=.5, le=80, allow_inf_nan=False)
    inner_radius: float = Field(default=5.0, gt=0, le=79, allow_inf_nan=False)
    length: float = Field(default=20.0, ge=.5, le=160, allow_inf_nan=False)
    profile: list[list[float]] | None = Field(default=None, min_length=2, max_length=16)
    axis: Literal['x', 'y', 'z'] = 'z'
    material: Literal['alloy', 'polymer', 'rubber', 'accent'] = 'alloy'
    color: str | None = Field(default=None, pattern=r'^(?:#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}|[a-zA-Z]{1,24})$')
    explode: list[float] | None = Field(default=None, min_length=3, max_length=3)
    order: int = Field(default=0, ge=0, le=11)

    @model_validator(mode='after')
    def bounds(self):
        if any(not math.isfinite(v) or abs(v) > 200 for v in self.position):
            raise ValueError('invalid_isometric_position')
        if any(not math.isfinite(v) or not .5 <= v <= 200 for v in self.size):
            raise ValueError('invalid_isometric_size')
        if self.kind == 'ring' and self.inner_radius >= self.radius:
            raise ValueError('ring_inner_radius_must_be_smaller')
        if self.kind == 'lathe':
            profile = self.profile
            if (not profile or any(len(p) != 2 or not all(math.isfinite(v) for v in p)
                or not 0 <= p[0] <= 160 or not 0 <= p[1] <= 80 for p in profile)
                or any(b[0] < a[0] for a,b in zip(profile,profile[1:]))
                or profile[-1][0] <= profile[0][0] or profile[-1][0] < .5
                or max(p[1] for p in profile) < .5):
                raise ValueError('invalid_lathe_profile')
            self.radius = max(p[1] for p in profile)
            self.length = profile[-1][0]
        elif self.profile is not None:
            raise ValueError('profile_requires_lathe')
        if self.explode is not None:
            explosion(self.explode, self.order)
        return self


class Assembly(Strict):
    id: str = Field(pattern=ID.pattern)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=1000)
    pieces: list[Piece] = Field(min_length=1, max_length=12)
    explode: list[float] = Field(min_length=3, max_length=3)
    order: int = Field(default=0, ge=0, le=11)

    @model_validator(mode='after')
    def plan(self):
        explosion(self.explode, self.order)
        if any(p.explode is not None for p in self.pieces) and any(p.explode is None for p in self.pieces):
            raise ValueError('incomplete_isometric_piece_plan')
        return self


class BoreFit(Strict):
    shaft_id: str = Field(pattern=ID.pattern)
    bore_id: str = Field(pattern=ID.pattern)


class IsometricScene(Strict):
    id: str = Field(default='device', pattern=ID.pattern)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)
    assemblies: list[Assembly] = Field(min_length=1, max_length=12)
    animation: CanvasAnimation | None = None
    bore_fits: list[BoreFit] | None = Field(default=None, min_length=1, max_length=12)

    @model_validator(mode='after')
    def identifiers(self):
        ids = [self.id]
        for group in self.assemblies:
            ids.append(group.id)
            ids.extend(p.id for p in group.pieces)
        if len(ids) != len(set(ids)) or sum(len(g.pieces) for g in self.assemblies) > 48:
            raise ValueError('isometric_id_or_piece_limit')
        if self.animation is not None and len(self.assemblies) < 2:
            raise ValueError('animation_requires_multiple_assemblies')
        if self.bore_fits:
            pieces = {p.id:p for g in self.assemblies for p in g.pieces}
            fits = {(f.shaft_id, f.bore_id) for f in self.bore_fits}
            if (len(fits) != len(self.bore_fits)
                or any(s not in pieces or b not in pieces or pieces[s].kind not in ('cylinder','lathe')
                       or pieces[b].kind != 'ring' for s,b in fits)):
                raise ValueError('invalid_bore_fit')
            if len(self.assemblies) < 2 or not any(any(v.explode or []) for g in self.assemblies for v in (g,*g.pieces)):
                raise ValueError('bore_fit_requires_motion_plan')
        return self


def project(point):
    x, y, z = point
    return ((x - y) * math.sqrt(3) / 2, (x + y) / 2 - z)


def explosion(vector, order):
    if any(not math.isfinite(v) or abs(v) > 240 for v in vector):
        raise ValueError('invalid_isometric_explosion')
    x, y = project(vector)
    if any(vector) and math.hypot(x, y) < 1e-6:
        raise ValueError('explosion_vector_parallel_to_camera')
    return Explosion(x=round(x, 4), y=round(y, 4), order=order)


def catalog():
    return {'projection': '固定30°等轴测，z轴向上；project(x,y,z)=((x-y)×0.866,(x+y)/2-z)',
            'coordinates': 'plate/block/rail的位置是包围盒最小角，不是中心。圆件位置是轴线起点，沿axis正向延伸；所有零件用同一世界坐标，组件无隐式原点偏移。例如居中96×64底板位置[-48,-32,10]，不是[0,0,10]；沿X从-50到50的轴应position[-50,0,38],length100。先计算接触面、同轴孔与支座，再加细节，不能用拆解姿态充当装配姿态。',
            'parts': list(KINDS), 'materials': ['alloy', 'polymer', 'rubber', 'accent'],
            'guide': '先确定有用的真实对象与装配关系；用板件/支座/轴/套环/紧固件构成结构。'
                     '统一相机和四档明暗，只突出少量accent；名称与长说明放在点击面板。'
                     '按实际结构给每组件explode三维向量和order，固定底座用[0,0,0]。'
                     '展开按order递增启动，合拢反向；支持保守路径检查与有限方向建议，不是工程装配验证。'}


def render_scene(scene: IsometricScene, style: str = 'clay'):
    palette = {
        'alloy': ('#eef0f2', '#cdd3d9', '#98a4b0', '#637282'),
        'polymer': ('#f1eee7', '#d9d4c9', '#b2afa5', '#797d7b'),
        'rubber': ('#646974', '#484f5b', '#353e4b', '#202a38'),
        'accent': ('#dce6d9', '#a8bda4', '#7e997a', '#4e6c52'),
    }
    ink = '#4d5b55'
    root = ET.Element('{' + NS + '}svg')
    ET.SubElement(root, '{' + NS + '}title').text = scene.title
    ET.SubElement(root, '{' + NS + '}desc').text = '固定等轴测相机、四档明暗、受控部件库；示意插图，不代表机械装配验证。'
    drawing = ET.Element('{' + NS + '}g', {'id': scene.id})
    parts = [Part(element_id=scene.id, label=scene.title, description=scene.description, illustration='isometric',
                  animation=scene.animation or (CanvasAnimation() if len(scene.assemblies) >= 2 else None))]
    extent = []

    def points3(points):
        projected = [project(p) for p in points]
        extent.extend(projected)
        return ' '.join(f'{x:.3f},{y:.3f}' for x, y in projected)

    def polygon(parent, points, color, outline=True):
        ET.SubElement(parent, '{' + NS + '}polygon', {'points': points3(points), 'fill': color,
                      'stroke': ink if outline else 'none', 'stroke-width': '.55', 'stroke-linejoin': 'round'})

    def line(parent, points, color=ink, width='.55'):
        ET.SubElement(parent, '{' + NS + '}polyline', {'points': points3(points), 'fill': 'none',
                      'stroke': color, 'stroke-width': width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})

    def box(parent, origin, size, colors):
        x, y, z = origin; w, d, h = size
        b = min(1.8, w / 8, d / 8, h / 4)
        top = [(x,y,z+h-b),(x+w,y,z+h-b),(x+w,y+d,z+h-b),(x,y+d,z+h-b)]
        inset = [(x+b,y+b,z+h),(x+w-b,y+b,z+h),(x+w-b,y+d-b,z+h),(x+b,y+d-b,z+h)]
        polygon(parent, [(x+w,y,z),(x+w,y+d,z),top[2],top[1]], colors[2])
        polygon(parent, [(x,y+d,z),(x+w,y+d,z),top[2],top[3]], colors[1])
        for i in range(4): polygon(parent, [top[i],top[(i+1)%4],inset[(i+1)%4],inset[i]], colors[1 if i < 2 else 0])
        polygon(parent, inset, colors[0])
        line(parent, [(x+w,y,z+h*.3),(x+w,y+d,z+h*.3),(x,y+d,z+h*.3)], colors[3], '.4')

    def cylinder(parent, origin, radius, length, axis, colors, segments=32, inner=0):
        index = {'x': 0, 'y': 1, 'z': 2}[axis]
        radial = [i for i in range(3) if i != index]
        def point(angle, along, r=radius):
            p = list(origin); p[index] += along
            p[radial[0]] += math.cos(angle)*r; p[radial[1]] += math.sin(angle)*r
            return p
        angles = [i*math.tau/segments for i in range(segments)]
        if segments == 6:
            for i in range(segments):
                a,b=angles[i],angles[i]+math.tau/segments
                if math.cos((a+b)/2)+math.sin((a+b)/2)>0:
                    polygon(parent,[point(a,0),point(b,0),point(b,length),point(a,length)],colors[1 if math.cos((a+b)/2)<0 else 2],False)
        else:
            def wall(start,end,color,r):
                steps=max(2,round((end-start)/math.tau*segments))
                arc=[start+(end-start)*i/steps for i in range(steps+1)]
                polygon(parent,[point(a,0,r) for a in arc]+[point(a,length,r) for a in reversed(arc)],color,False)
            if inner: wall(math.pi*.75,math.pi*1.75,colors[3],inner)
            # Continuous shaded strips avoid hairline seams between tiny facets.
            wall(-math.pi/4,math.pi/2,colors[2],radius)
            wall(math.pi/2,math.pi*.75,colors[1],radius)
        outer = [point(a,length) for a in angles]
        if inner:
            outer_text = points3(outer).split(' ')
            inside_text = points3([point(a,length,inner) for a in reversed(angles)]).split(' ')
            path = 'M ' + ' L '.join(outer_text) + ' Z M ' + ' L '.join(inside_text) + ' Z'
            ET.SubElement(parent, '{' + NS + '}path', {'d': path, 'fill': colors[0], 'fill-rule': 'evenodd', 'stroke': ink, 'stroke-width': '.55'})
        else:
            polygon(parent, outer, colors[0])
        return point

    def piece_art(piece):
        parent = ET.Element('{' + NS + '}g', {'id': piece.id})
        ET.SubElement(parent, '{' + NS + '}title').text = piece.label
        colors = (piece.color,)*4 if piece.color else palette[piece.material]; origin = piece.position
        if piece.kind in ('block', 'plate', 'rail'):
            box(parent, origin, piece.size, colors)
            x,y,z = origin; w,d,h = piece.size
            if piece.kind == 'rail':
                line(parent, [(x,y+d*.45,z+h),(x+w,y+d*.45,z+h)], colors[3], '.8')
                line(parent, [(x,y+d*.55,z+h),(x+w,y+d*.55,z+h)], colors[3], '.5')
            if piece.kind == 'plate' and min(w,d) >= 12:
                for u,v in ((3,3),(w-3,3),(w-3,d-3),(3,d-3)):
                    center = (x+u,y+v,z+h+.2)
                    polygon(parent, [(center[0]+math.cos(a)*1.3,center[1]+math.sin(a)*1.3,center[2]) for a in [i*math.tau/8 for i in range(8)]], colors[2])
                    line(parent, [(center[0]-.8,center[1],center[2]+.1),(center[0]+.8,center[1],center[2]+.1)], colors[3])
        elif piece.kind == 'sphere':
            cx,cy=project(origin); r=piece.radius
            ET.SubElement(parent, '{'+NS+'}circle', {'cx':str(cx),'cy':str(cy),'r':str(r),'fill':colors[1],'stroke':ink,'stroke-width':'.55'})
            extent.extend([(cx-r,cy-r),(cx+r,cy+r)])
        elif piece.kind == 'lathe':
            axis='xyz'.index(piece.axis)
            for start,end in zip(piece.profile,piece.profile[1:]):
                if end[0] <= start[0] or max(start[1],end[1]) <= 0: continue
                at=list(origin); at[axis]+=start[0]
                cylinder(parent,at,max(start[1],end[1]),end[0]-start[0],piece.axis,colors)
        else:
            point = cylinder(parent, origin, piece.radius, piece.length, piece.axis, colors,
                             segments=6 if piece.kind == 'bolt' else 32,
                             inner=piece.inner_radius if piece.kind == 'ring' else 0)
            if piece.kind in ('knob','bolt'):
                line(parent, [point(0,piece.length+.1,piece.radius*.7),point(math.pi,piece.length+.1,piece.radius*.7)], colors[3], '1')
                if piece.kind == 'knob':
                    for i in range(12):
                        a=i*math.tau/12
                        if math.cos(a)+math.sin(a)>0: line(parent,[point(a,piece.length*.15),point(a,piece.length*.85)], colors[3], '.4')
        return parent

    def depth(piece):
        center = list(piece.position)
        if piece.kind in ('block','plate','rail'): center = [a+b/2 for a,b in zip(center,piece.size)]
        else: center[{'x':0,'y':1,'z':2}[piece.axis]] += piece.length/2
        return sum(center)

    for group in sorted(scene.assemblies, key=lambda g: sum(depth(p) for p in g.pieces)/len(g.pieces)):
        node = ET.SubElement(drawing, '{' + NS + '}g', {'id': group.id})
        ET.SubElement(node, '{' + NS + '}title').text = group.label
        parts.append(Part(element_id=group.id,label=group.label,description=group.description,explode=explosion(group.explode,group.order)))
        for piece in sorted(group.pieces, key=depth):
            node.append(piece_art(piece))
            parts.append(Part(element_id=piece.id,label=piece.label,description=piece.description,
                              explode=explosion(piece.explode,piece.order) if piece.explode is not None else None))
    if not extent:
        raise CanvasError('empty_isometric_scene')
    xs,ys=zip(*extent); lo,hi=min(xs),max(xs); top,bottom=min(ys),max(ys)
    shadow = ET.SubElement(root,'{' + NS + '}ellipse', {'cx':f'{(lo+hi)/2:.3f}','cy':f'{bottom-2:.3f}',
        'rx':f'{max(1,(hi-lo)*.42):.3f}','ry':f'{max(2,(bottom-top)*.05):.3f}','fill':ink,'opacity':'.09'})
    root.append(drawing)
    padding=18
    bottom=max(bottom,float(shadow.get('cy'))+float(shadow.get('ry')))
    root.set('viewBox',f'{lo-padding:.3f} {top-padding:.3f} {hi-lo+padding*2:.3f} {bottom-top+padding*2:.3f}')
    source,_=validate_svg(ET.tostring(root,encoding='unicode'))
    parts[0].solid_scene = scene.model_dump(exclude_none=True)
    return source,parts
