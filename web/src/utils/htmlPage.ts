import type { CanvasStyle } from '../api/canvas';
const interactions = `let sizeFrame=0,sizeWidth=innerWidth;const reportSize=()=>{cancelAnimationFrame(sizeFrame);sizeFrame=requestAnimationFrame(()=>parent.postMessage({type:'tcode:html-size',height:Math.ceil(document.body.getBoundingClientRect().height)},'*'));};new MutationObserver(reportSize).observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true});window.addEventListener('resize',()=>{if(innerWidth!==sizeWidth){sizeWidth=innerWidth;reportSize();}});window.addEventListener('load',reportSize);reportSize();
document.addEventListener('keydown',event=>{if(event.key==='Escape')parent.postMessage({type:'tcode:html-close'},'*');});
document.querySelectorAll('[data-ui-target]').forEach(control=>{
 const target=document.getElementById(control.dataset.uiTarget);if(!target)return;
 if(control.dataset.uiAction==='toggle')control.setAttribute('aria-expanded',String(!target.hidden));
 else target.setAttribute('aria-live','polite');
});
document.addEventListener('click',event=>{
 const control=event.target.closest('[data-ui-action]');if(!control)return;
 const target=document.getElementById(control.dataset.uiTarget);if(!target)return;
 if(control.dataset.uiAction==='toggle'){target.hidden=!target.hidden;control.setAttribute('aria-expanded',String(!target.hidden));}
 if(control.dataset.uiAction==='select'){target.textContent=control.dataset.uiValue||control.textContent;document.querySelectorAll('[data-ui-action="select"]').forEach(item=>{if(item.dataset.uiTarget===control.dataset.uiTarget)item.setAttribute('aria-pressed',String(item===control));});}
});
document.addEventListener('input',event=>{const control=event.target;if(control.dataset.uiAction!=='input')return;const target=document.getElementById(control.dataset.uiTarget);if(target)target.textContent=control.type==='checkbox'?String(control.checked):control.value;});`;
// Project UI color declarations onto the selected palette, including older model output.
// Inline SVG attributes retain deliberate diagram/data colors.
export function themeHtmlCss(css: string): string {
 return css.replace(/(^|[;{])\s*(color|background(?:-color)?|border(?:-(?:color|top|right|bottom|left))?|accent-color|outline(?:-color)?|box-shadow)\s*:\s*([^;{}]+)/gi, (_match, prefix, property, value) => {
  const name = property.toLowerCase();
  const token = name === 'color' ? '--ui-text' : name === 'accent-color' ? '--ui-accent' : name.startsWith('background') ? '--ui-surface' : '--ui-border';
  const themed = value.replace(/var\([^)]*\)|#[0-9a-f]{3,8}\b|(?:rgba?|hsla?)\([^)]*\)|\b(?:white|black|purple|blue|pink|lavender)\b/gi, (color: string) => color.startsWith('var(') ? color : `var(${token})`);
  return `${prefix}${property}:${themed}`;
 });
}
export function htmlPageDocument(html: string, css: string, style: CanvasStyle, nonce: string) {
 const csp = `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`;
 const theme = style === 'clay'
 ? '--ui-bg:#f8f5fa;--ui-surface:#fff;--ui-text:#30283b;--ui-muted:#665a70;--ui-accent:#76518c;--ui-border:#e8dfec;--ui-radius:20px;'
 : '--ui-bg:#fff;--ui-surface:#f6f8fb;--ui-text:#172235;--ui-muted:#59677b;--ui-accent:#2563eb;--ui-border:#e2e8f0;--ui-radius:12px;';
 return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
 :root{${theme}color-scheme:light;font-family:system-ui,-apple-system,sans-serif;color:var(--ui-text);background:var(--ui-bg)}
 *{box-sizing:border-box}body{margin:0;padding:28px;line-height:1.65;overflow-wrap:anywhere}h1,h2,h3,h4{line-height:1.3;letter-spacing:-.02em;margin:0 0 18px}p{margin:0 0 18px}small{color:var(--ui-muted)}
 button,input,select{font:inherit}button,select,input{min-height:40px}button{border:1px solid var(--ui-border);background:var(--ui-surface);color:var(--ui-text);border-radius:var(--ui-radius);padding:9px 16px;cursor:pointer}button:hover{border-color:var(--ui-accent)}[aria-pressed=true]{background:var(--ui-accent);color:#fff}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--ui-accent);outline-offset:3px}button:disabled{opacity:.5;cursor:default}
 svg{max-width:100%;height:auto}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid var(--ui-border)}[hidden]{display:none!important}
 @media(max-width:480px){body{padding:18px}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
 ${themeHtmlCss(css)}
button[aria-pressed=true]{background:var(--ui-accent)!important;color:#fff!important;border-color:var(--ui-accent)!important}
</style></head><body>${html.replace(/ style="([^"]*)"/g, (_match, inline) => ` style="${themeHtmlCss(inline)}"`)}<script nonce="${nonce}">${interactions}</script></body></html>`;
}
