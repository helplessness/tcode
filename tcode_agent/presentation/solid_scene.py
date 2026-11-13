"""Conservative continuous envelopes for the bounded, axis-aligned parts kit.

Warnings describe possible interference, never engineering certification.
"""
from __future__ import annotations

import math
from functools import lru_cache
from itertools import combinations

from .isometric import IsometricScene


def _portion(value, order, orders, sequential=False):
    if sequential:
        return max(0., min(1., value*len(orders)-orders.index(order))) if order in orders else 0.
    delay = orders.index(order) * .25
    step = max(0., min(1., value * (1 + (len(orders)-1)*.25) - delay))
    return step * step * (3-2*step) if len(orders)>1 else step


def bounds(piece):
    lo = list(piece.position)
    if piece.kind == 'sphere':
        return [v-piece.radius for v in lo], [v+piece.radius for v in lo]
    if piece.kind in ('plate','block','rail'):
        return lo, [v+s for v,s in zip(lo,piece.size)]
    axis = 'xyz'.index(piece.axis)
    if piece.kind == 'lathe':
        lo[axis] += piece.profile[0][0]
    hi = list(lo)
    for index in range(3):
        if index == axis: hi[index] = piece.position[index] + piece.length
        else: lo[index] -= piece.radius; hi[index] += piece.radius
    return lo, hi


def _overlap(a, b):
    # Touching faces are allowed. Marginal/tolerance cases still require review.
    return all(min(a[1][i],b[1][i])-max(a[0][i],b[0][i])>.05 for i in range(3))


def _bore_radius(a, b, offset_a, offset_b, upper_a, upper_b):
    """Largest shaft section swept across the axial span of this bore."""
    axis = 'xyz'.index(a.axis)
    lo = a.position[axis]+offset_a[axis]-b.position[axis]-upper_b[axis]
    hi = a.position[axis]+a.length+upper_a[axis]-b.position[axis]-offset_b[axis]
    profile = b.profile if b.kind == 'lathe' else [(0,b.radius),(b.length,b.radius)]
    radii = []
    for (x,r),(y,s) in zip(profile,profile[1:]):
        start,end=max(lo,x),min(hi,y)
        if end-start > 1e-8:
            radii.extend(r+(s-r)*(t-x)/(y-x) for t in (start,end))
    return max(radii, default=None)


def _through_hole(a, b, offset_a, offset_b, upper_a=None, upper_b=None):
    if b.kind=='sphere' and a.kind in ('ring','cylinder','lathe'):
        radial=[i for i in range(3) if i!='xyz'.index(a.axis)]
    elif a.kind=='ring' and b.kind in ('cylinder','ring','bolt','knob','lathe') and a.axis==b.axis:
        radial=[i for i in range(3) if i!='xyz'.index(a.axis)]
    else:
        return False
    upper_a=offset_a if upper_a is None else upper_a
    upper_b=offset_b if upper_b is None else upper_b
    if b.kind=='sphere':
        intervals=[(b.position[i]+offset_b[i]-a.position[i]-upper_a[i],
                    b.position[i]+upper_b[i]-a.position[i]-offset_a[i]) for i in radial]
        nearest=math.hypot(*(max(lo,-hi,0) for lo,hi in intervals))
        farthest=math.hypot(*(max(abs(lo),abs(hi)) for lo,hi in intervals))
        return nearest-b.radius>=a.radius or (a.kind=='ring' and farthest+b.radius<=a.inner_radius)
    # Farthest radial corner of the entire relative-motion interval. This also
    # covers staggered, nested plans; endpoints alone cannot prove clearance.
    distance=math.sqrt(sum(max(abs(a.position[i]+offset_a[i]-b.position[i]-upper_b[i]),
                              abs(a.position[i]+upper_a[i]-b.position[i]-offset_b[i]))**2 for i in radial))
    radius = _bore_radius(a,b,offset_a,offset_b,upper_a,upper_b) if b.kind=='lathe' else b.radius
    return radius is None or distance+radius<=a.inner_radius+.001


def check_scene(scene: IsometricScene, *, suggest=False):
    pieces=[(g,p) for g in scene.assemblies for p in g.pieces]
    base=[bounds(p) for _,p in pieces]
    sequential=bool(scene.bore_fits)
    orders=sorted({v.order for g in scene.assemblies for v in (g,*g.pieces)
                   if v.explode is not None and (not sequential or any(v.explode))})
    def offsets(share):
        return [[g.explode[i]*_portion(share,g.order,orders,sequential)+(p.explode[i]*_portion(share,p.order,orders,sequential) if p.explode is not None else 0) for i in range(3)] for g,p in pieces]
    def boxes(offset):
        return [([v+d for v,d in zip(lo,o)],[v+d for v,d in zip(hi,o)]) for (lo,hi),o in zip(base,offset)]
    pairs=[(i,j) for i,j in combinations(range(len(pieces)),2) if pieces[i][0].id!=pieces[j][0].id or pieces[i][1].explode is not None or pieces[j][1].explode is not None]
    initial=boxes(offsets(0)); initial_hits=set(); transit_hits=set()
    for i,j in pairs:
        if _overlap(initial[i],initial[j]) and not (_through_hole(pieces[i][1],pieces[j][1],[0]*3,[0]*3) or _through_hole(pieces[j][1],pieces[i][1],[0]*3,[0]*3)):
            initial_hits.add((i,j))
    # Each individual plan is monotonic. Sum their interval bounds separately:
    # opposite assembly/piece directions can overshoot their combined endpoints.
    fit_indices={p.id:i for i,(_,p) in enumerate(pieces)}
    zero=[0,0,0]
    fits=[]
    for fit in scene.bore_fits or []:
        i,j=fit_indices[fit.bore_id],fit_indices[fit.shaft_id]
        a,b=pieces[i][1],pieces[j][1]
        parallel=a.axis==b.axis
        axis='xyz'.index(a.axis)
        aligned=parallel and math.hypot(*(a.position[k]-b.position[k] for k in range(3) if k!=axis))<=.001
        radius=_bore_radius(a,b,zero,zero,zero,zero) if parallel else None
        fits.append((i,j,{'element_ids':[b.id,a.id],'labels':[b.label,a.label],
                         'coaxial':aligned,'bore_diameter':2*a.inner_radius,
                         'engaged_diameter':2*radius if radius is not None else None,
                         'swept_diameter':None}))
    times=sorted({i/64 for i in range(65)} | ({i/len(orders) for i in range(len(orders)+1)} if sequential and orders else set()))
    for start,end in zip(times,times[1:]):
        swept=[]; envelopes=[]
        for (g,p),(lo,hi) in zip(pieces,base):
            plans=[g]+([p] if p.explode is not None else [])
            ends=[[v*_portion(t,plan.order,orders,sequential) for v in plan.explode] for plan in plans for t in (start,end)]
            low=[sum(min(ends[j][i],ends[j+1][i]) for j in range(0,len(ends),2)) for i in range(3)]
            high=[sum(max(ends[j][i],ends[j+1][i]) for j in range(0,len(ends),2)) for i in range(3)]
            envelopes.append((low,high))
            swept.append(([v+d for v,d in zip(lo,low)],[v+d for v,d in zip(hi,high)]))
        if fits:
            # Within each guided stage the relative displacement is linear,
            # so a bore and shaft moving together do not sweep extra sections.
            first,last=offsets(start),offsets(end)
            for i,j,fit in fits:
                a,b=pieces[i][1],pieces[j][1]
                if a.axis!=b.axis: continue
                axis='xyz'.index(a.axis)
                lo,hi=sorted((first[i][axis]-first[j][axis],last[i][axis]-last[j][axis]))
                lower,upper=zero.copy(),zero.copy()
                lower[axis],upper[axis]=lo,hi
                radius=_bore_radius(a,b,lower,zero,upper,zero)
                if radius is not None:
                    fit['swept_diameter']=max(fit['swept_diameter'] or 0,2*radius)
        for i,j in pairs:
            if (i,j) in initial_hits or not _overlap(swept[i],swept[j]):continue
            # Exempt only if the complete radial interval fits inside the hole.
            if any(_through_hole(pieces[a][1],pieces[b][1],envelopes[a][0],envelopes[b][0],envelopes[a][1],envelopes[b][1]) for a,b in ((i,j),(j,i))):continue
            transit_hits.add((i,j))
    def issue(pair,phase):
        i,j=pair
        return {'element_ids':[pieces[i][1].id,pieces[j][1].id], 'labels':[pieces[i][1].label,pieces[j][1].label],
                'phase':phase,'message':f'{pieces[i][1].label}与{pieces[j][1].label}'+('在装配状态可能相交' if phase=='assembled' else '在拆解路径上可能相交')}
    issues=[issue(p,'assembled') for p in sorted(initial_hits)]+[issue(p,'motion') for p in sorted(transit_hits)]
    for _,_,fit in fits:
        diameter=fit['engaged_diameter']
        fit['diameter_ok']=None if diameter is None else diameter<=fit['bore_diameter']+.002
        swept_diameter=fit['swept_diameter']
        fit['shoulder_ok']=None if not fit['coaxial'] or not fit['diameter_ok'] or swept_diameter is None else swept_diameter<=fit['bore_diameter']+.002
    result={'status':'needs_review' if issues or any(not (f['coaxial'] and f['diameter_ok'] and f['shoulder_ok']) for _,_,f in fits) else 'no_risk_detected','initial_count':len(initial_hits),'motion_count':len(transit_hits),
            'bore_fits':[f for _,_,f in fits],
            'issues':issues[:24], 'issues_truncated':len(issues)>24,'suggestions':[],
            'note':'连续运动包络检查，已计入按顺序错开的同时运动；阶段会重叠，顺序数字的间隔不代表等待时间。同轴孔道仅在完整径向运动区间都可通过时豁免，其余圆曲面按保守边界处理。不证明承重、装配公差或任意机械结构的安全性。'}
    if sequential:
        result['note']='按逐步装配的同一条路径检查；同序同时移动，异序分步。轴肩检查比较扫过孔内的轴截面；侧向偏移及其他部件采用保守包络。不证明承重或装配公差。'
    if suggest and transit_hits:
        movable={v.id:v for g in scene.assemblies for v in (g,*g.pieces) if v.explode is not None and any(v.explode)}
        targets=sorted({v.id for pair in transit_hits for i in pair for v in pieces[i] if v.id in movable})
        best_score=len(transit_hits)
        # ponytail: at most three implicated groups and six straight-axis paths;
        # this is a bounded direction search, not arbitrary path planning.
        for identifier in targets[:3]:
            group=movable[identifier]
            distance=max(60,min(200,math.sqrt(sum(v*v for v in group.explode))))
            best=None
            for axis in range(3):
                for sign in (-1,1):
                    vector=[0.,0.,0.];vector[axis]=distance*sign
                    candidate=scene.model_copy(deep=True)
                    next(v for g in candidate.assemblies for v in (g,*g.pieces) if v.id==identifier).explode=vector
                    report=check_scene(candidate)
                    if report['motion_count']<best_score:
                        best_score=report['motion_count'];best={'element_id':identifier,'label':group.label,'vector':vector,'order':group.order,
                            'remaining_motion_risks':best_score,'message':f'{group.label}改为沿'+('X','Y','Z')[axis]+('正向' if sign>0 else '负向')+'移开'}
            if best:result['suggestions'].append(best)
            if best_score==0:break
    return result


@lru_cache(maxsize=16)
def cached_check(source: str, suggest=False):
    return check_scene(IsometricScene.model_validate_json(source),suggest=suggest)
