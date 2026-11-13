import{p as et}from"./chunk-JWPE2WC7-55oE0Ir_.js";import{g as at,s as rt,a as it,b as ot,o as nt,n as st,_ as l,l as E,c as lt,z as ct,C as dt,D as pt,d as gt,p as ht,A as ft}from"./mermaid.core-DCMBboas.js";import{p as ut}from"./mermaid-parser-YMkvD_Pr.js";import{E as U,F as mt,G as vt}from"./mermaid-d3-Cp2G_UdO.js";import"./vite-runtime-ckwbz45p.js";import"./mermaid-support-C04NTI7B.js";import"./commonjs-helpers-D6-XlEtG.js";import"./purify.es-DowdsyMd.js";import"./MarkdownContent-aCSgN-FF.js";import"./paper-theme-DyA_bsH6.js";import"./ImageLightbox-mBS0grnA.js";import"./inlineFiles-XrmpxRO1.js";import"./mermaid-rough-CycZv-lV.js";import"./lodash-es-4kinMtiL.js";var St=ft.pie,R={sections:new Map,showData:!1},T=R.sections,F=R.showData,xt=structuredClone(St),wt=l(()=>structuredClone(xt),"getConfig"),Ct=l(()=>{T=new Map,F=R.showData,ht()},"clear"),$t=l(({label:t,value:a})=>{if(a<0)throw new Error(`"${t}" has invalid value: ${a}. Negative values are not allowed in pie charts. All slice values must be >= 0.`);T.has(t)||(T.set(t,a),E.debug(`added new section: ${t}, with value: ${a}`))},"addSection"),Dt=l(()=>T,"getSections"),yt=l(t=>{F=t},"setShowData"),Tt=l(()=>F,"getShowData"),V={getConfig:wt,clear:Ct,setDiagramTitle:st,getDiagramTitle:nt,setAccTitle:ot,getAccTitle:it,setAccDescription:rt,getAccDescription:at,addSection:$t,getSections:Dt,setShowData:yt,getShowData:Tt},bt=l((t,a)=>{et(t,a),a.setShowData(t.showData),t.sections.map(a.addSection)},"populateDb"),At={parse:l(async t=>{const a=await ut("pie",t);E.debug(a),bt(a,V)},"parse")},_t=l(t=>`
  .pieCircle{
    stroke: ${t.pieStrokeColor};
    stroke-width : ${t.pieStrokeWidth};
    opacity : ${t.pieOpacity};
  }
  .pieCircle.highlighted{
    scale: 1.05;
    opacity: 1;
  }
  .pieCircle.highlightedOnHover:hover{
    transition-duration: 250ms;
    scale: 1.05;
    opacity: 1;
  }
  .pieOuterCircle{
    stroke: ${t.pieOuterStrokeColor};
    stroke-width: ${t.pieOuterStrokeWidth};
    fill: none;
  }
  .pieTitleText {
    text-anchor: middle;
    font-size: ${t.pieTitleTextSize};
    fill: ${t.pieTitleTextColor};
    font-family: ${t.fontFamily};
  }
  .slice {
    font-family: ${t.fontFamily};
    fill: ${t.pieSectionTextColor};
    font-size:${t.pieSectionTextSize};
    // fill: white;
  }
  .legend text {
    fill: ${t.pieLegendTextColor};
    font-family: ${t.fontFamily};
    font-size: ${t.pieLegendTextSize};
  }
`,"getStyles"),kt=_t,zt=l(t=>{const a=[...t.values()].reduce((n,m)=>n+m,0),L=[...t.entries()].map(([n,m])=>({label:n,value:m})).filter(n=>n.value/a*100>=1);return vt().value(n=>n.value).sort(null)(L)},"createPieArcs"),Et=l((t,a,L,W)=>{var I;E.debug(`rendering pie chart
`+t);const n=W.db,m=lt(),h=ct(n.getConfig(),m.pie),G=40,i=18,c=4,C=450,S=C,b=dt(a),$=b.append("g");$.attr("transform","translate("+S/2+","+C/2+")");const{themeVariables:o}=m;let[H]=pt(o.pieOuterStrokeWidth);H??(H=2);const X=h.legendPosition,M=h.textPosition,Z=h.donutHole>0&&h.donutHole<=.9?h.donutHole:0,f=Math.min(S,C)/2-G,j=U().innerRadius(Z*f).outerRadius(f),q=U().innerRadius(f*M).outerRadius(f*M),x=$.append("g");x.append("circle").attr("cx",0).attr("cy",0).attr("r",f+H/2).attr("class","pieOuterCircle");const D=n.getSections(),J=zt(D),K=[o.pie1,o.pie2,o.pie3,o.pie4,o.pie5,o.pie6,o.pie7,o.pie8,o.pie9,o.pie10,o.pie11,o.pie12];let A=0;D.forEach(e=>{A+=e});const O=J.filter(e=>(e.data.value/A*100).toFixed(0)!=="0"),_=mt(K).domain([...D.keys()]);x.selectAll("mySlices").data(O).enter().append("path").attr("d",j).attr("fill",e=>_(e.data.label)).attr("class",e=>{let r="pieCircle";return h.highlightSlice==="hover"?r+=" highlightedOnHover":h.highlightSlice===e.data.label&&(r+=" highlighted"),r}),x.selectAll("mySlices").data(O).enter().append("text").text(e=>(e.data.value/A*100).toFixed(0)+"%").attr("transform",e=>"translate("+q.centroid(e)+")").style("text-anchor","middle").attr("class","slice");const Q=$.append("text").text(n.getDiagramTitle()).attr("x",0).attr("y",-400/2).attr("class","pieTitleText"),w=[...D.entries()].map(([e,r])=>({label:e,value:r})),u=$.selectAll(".legend").data(w).enter().append("g").attr("class","legend");u.append("rect").attr("width",i).attr("height",i).style("fill",e=>_(e.label)).style("stroke",e=>_(e.label)),u.append("text").attr("x",i+c).attr("y",i-c).text(e=>n.getShowData()?`${e.label} [${e.value}]`:e.label);const v=Math.max(...u.selectAll("text").nodes().map(e=>(e==null?void 0:e.getBoundingClientRect().width)??0));let y=C,k=S+G;const s=i+c,z=w.length*s;switch(X){case"center":u.attr("transform",(e,r)=>{const d=s*w.length/2,p=-v/2-(i+c),g=r*s-d;return"translate("+p+","+g+")"});break;case"top":y+=z,u.attr("transform",(e,r)=>{const d=f,p=-v/2-(i+c),g=r*s-d;return`translate(${p}, ${g})`}),x.attr("transform",()=>`translate(0, ${z+s})`);break;case"bottom":y+=z,u.attr("transform",(e,r)=>{const d=-f-s,p=-v/2-(i+c),g=r*s-d;return"translate("+p+","+g+")"});break;case"left":k+=i+c+v,u.attr("transform",(e,r)=>{const d=s*w.length/2,p=-f-(i+c),g=r*s-d;return"translate("+p+","+g+")"}),x.attr("transform",()=>`translate(${v+i+c}, 0)`);break;case"right":default:k+=i+c+v,u.attr("transform",(e,r)=>{const d=s*w.length/2,p=12*i,g=r*s-d;return"translate("+p+","+g+")"});break}const P=((I=Q.node())==null?void 0:I.getBoundingClientRect().width)??0,Y=S/2-P/2,tt=S/2+P/2,B=Math.min(0,Y),N=Math.max(k,tt)-B;b.attr("viewBox",`${B} 0 ${N} ${y}`),gt(b,y,N,h.useMaxWidth)},"draw"),Rt={draw:Et},jt={parser:At,db:V,renderer:Rt,styles:kt};export{jt as diagram};
