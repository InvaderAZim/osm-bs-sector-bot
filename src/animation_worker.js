import appWorker from './production_worker.js';

const ANIMATION_STYLE = `<style id="duga-motion">
@keyframes dugaTopIn{from{opacity:0;transform:translateY(-14px)}to{opacity:1;transform:translateY(0)}}
@keyframes dugaControlsIn{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:translateY(0)}}
@keyframes dugaFadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes dugaPulse{0%,100%{box-shadow:0 0 0 0 rgba(61,233,255,0)}50%{box-shadow:0 0 0 6px rgba(61,233,255,.08)}}
@keyframes dugaDot{0%,100%{opacity:.55;transform:scale(.9)}50%{opacity:1;transform:scale(1.18)}}
@keyframes dugaSpin{to{transform:rotate(360deg)}}
@keyframes dugaMarker{0%,100%{filter:drop-shadow(0 0 0 rgba(61,233,255,0))}50%{filter:drop-shadow(0 0 8px rgba(61,233,255,.34))}}

.top{animation:dugaTopIn .42s cubic-bezier(.2,.8,.2,1) both}
.controls{animation:dugaControlsIn .48s .05s cubic-bezier(.2,.8,.2,1) both}
.title,.search,.point-tabs,.grid,.radii,.actions,.common-polygon-btn,.status{will-change:transform,opacity}
.title{animation:dugaFadeUp .32s .06s ease-out both}
.search{animation:dugaFadeUp .34s .10s ease-out both}
.point-tabs{animation:dugaFadeUp .34s .12s ease-out both}
.grid{animation:dugaFadeUp .34s .16s ease-out both}
.radii{animation:dugaFadeUp .34s .20s ease-out both}
.actions{animation:dugaFadeUp .34s .24s ease-out both}
.common-polygon-btn{animation:dugaFadeUp .34s .28s ease-out both}
.status{animation:dugaFadeUp .34s .32s ease-out both}

button{transition:transform .16s cubic-bezier(.2,.8,.2,1),filter .16s ease,box-shadow .2s ease,border-color .2s ease,background .2s ease!important}
button:hover{transform:translateY(-1px)}
button:active{transform:translateY(1px) scale(.985)!important}
.search input,.field input{transition:border-color .2s ease,box-shadow .2s ease,background .2s ease,transform .2s ease!important}
.search input:focus,.field input:focus{transform:translateY(-1px)}
.point-tabs button.active,.radii button.active,.common-polygon-btn.active{animation:dugaPulse 1.9s ease-in-out infinite}
.status::before{animation:dugaDot 1.45s ease-in-out infinite}
.point-icon{animation:dugaMarker 2.2s ease-in-out infinite;transition:transform .18s ease,filter .18s ease}
.fullscreen-btn{transition:transform .2s ease,box-shadow .2s ease,background .2s ease!important}
.fullscreen-btn:hover{box-shadow:0 10px 28px rgba(0,0,0,.42),0 0 0 1px rgba(61,233,255,.12)}

#results{transform-origin:top center;transition:opacity .18s ease,transform .18s ease}
#results .result{animation:dugaFadeUp .22s ease-out both;transition:background .16s ease,transform .16s ease}
#results .result:nth-child(2){animation-delay:.035s}
#results .result:nth-child(3){animation-delay:.07s}
#results .result:nth-child(4){animation-delay:.105s}
#results .result:nth-child(5){animation-delay:.14s}
#results .result:hover{transform:translateX(2px)}

#searchBtn.is-loading{position:relative;pointer-events:none;padding-right:38px;filter:saturate(.8)}
#searchBtn.is-loading::after{content:'';position:absolute;right:14px;width:14px;height:14px;border:2px solid rgba(3,27,33,.28);border-top-color:#031b21;border-radius:50%;animation:dugaSpin .7s linear infinite}

body.duga-fullscreen .fullscreen-btn{animation:dugaFadeUp .24s ease-out both}
.leaflet-control-zoom a{transition:background .16s ease,color .16s ease,transform .16s ease!important}
.leaflet-control-zoom a:active{transform:scale(.94)}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important;scroll-behavior:auto!important}
  button:hover,.search input:focus,.field input:focus,#results .result:hover{transform:none!important}
}
</style>`;

const ANIMATION_SCRIPT = `<script id="duga-motion-runtime">(()=>{
  const searchBtn=document.getElementById('searchBtn');
  const results=document.getElementById('results');
  if(searchBtn&&results){
    const stop=()=>searchBtn.classList.remove('is-loading');
    searchBtn.addEventListener('click',()=>{searchBtn.classList.add('is-loading');setTimeout(stop,12000)},true);
    const observer=new MutationObserver(()=>{if(results.children.length)stop()});
    observer.observe(results,{childList:true,subtree:false});
  }
  document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target instanceof HTMLButtonElement){e.target.animate([{transform:'scale(1)'},{transform:'scale(.97)'},{transform:'scale(1)'}],{duration:180,easing:'ease-out'})}});
})();</script>`;

function injectAnimations(html) {
  let output = String(html || '');
  if (!output.includes('id="duga-motion"')) output = output.replace('</head>', `${ANIMATION_STYLE}</head>`);
  if (!output.includes('id="duga-motion-runtime"')) output = output.replace('</body>', `${ANIMATION_SCRIPT}</body>`);
  return output;
}

export default {
  async fetch(request, env, ctx) {
    const response = await appWorker.fetch(request, env, ctx);
    const url = new URL(request.url);
    if (url.pathname !== '/api/app' || !response.ok) return response;
    const contentType = response.headers.get('Content-Type') || '';
    if (!contentType.includes('text/html')) return response;
    const html = await response.text();
    const headers = new Headers(response.headers);
    headers.set('Cache-Control', 'no-store');
    return new Response(injectAnimations(html), { status: response.status, headers });
  },
  async queue(batch, env) {
    if (typeof appWorker.queue === 'function') return appWorker.queue(batch, env);
  },
};
