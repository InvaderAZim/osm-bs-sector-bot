from __future__ import annotations

import mini_app


FULLSCREEN_CSS = r'''
    .fullscreen-btn{position:absolute;z-index:1000;right:10px;top:10px;border:1px solid rgba(255,255,255,.25);background:rgba(23,26,34,.92);color:#fff;border-radius:10px;padding:9px 11px;font-weight:800;box-shadow:0 2px 10px rgba(0,0,0,.35);backdrop-filter:blur(6px)}
    body.duga-fullscreen .top,body.duga-fullscreen .controls{display:none}
    body.duga-fullscreen #map{height:100vh;min-height:100vh}
'''

FULLSCREEN_BUTTON = r'''
  <button id="fullscreenBtn" class="fullscreen-btn" type="button" title="На весь екран">⛶ На весь екран</button>
'''

FULLSCREEN_JS = r'''
    const fullscreenBtn=document.getElementById('fullscreenBtn');
    function isDugaFullscreen(){return Boolean(document.fullscreenElement)||(tg&&Boolean(tg.isFullscreen))||document.body.classList.contains('duga-fullscreen')}
    function updateFullscreenButton(){fullscreenBtn.textContent=isDugaFullscreen()?'⛶ Вийти':'⛶ На весь екран';setTimeout(()=>map.invalidateSize(),80)}
    async function enterDugaFullscreen(){
      document.body.classList.add('duga-fullscreen');
      try{
        if(tg&&typeof tg.requestFullscreen==='function'){tg.requestFullscreen()}
        else if(document.documentElement.requestFullscreen){await document.documentElement.requestFullscreen()}
        else if(tg&&typeof tg.expand==='function'){tg.expand()}
      }catch(e){if(tg&&typeof tg.expand==='function')tg.expand()}
      updateFullscreenButton();
    }
    async function exitDugaFullscreen(){
      document.body.classList.remove('duga-fullscreen');
      try{
        if(tg&&typeof tg.exitFullscreen==='function'){tg.exitFullscreen()}
        else if(document.fullscreenElement&&document.exitFullscreen){await document.exitFullscreen()}
      }catch(e){}
      updateFullscreenButton();
    }
    fullscreenBtn.addEventListener('click',()=>isDugaFullscreen()?exitDugaFullscreen():enterDugaFullscreen());
    document.addEventListener('fullscreenchange',updateFullscreenButton);
    if(tg&&typeof tg.onEvent==='function')tg.onEvent('fullscreenChanged',()=>{if(!tg.isFullscreen)document.body.classList.remove('duga-fullscreen');updateFullscreenButton()});
'''

html = mini_app.APP_HTML
if 'id="fullscreenBtn"' not in html:
    html = html.replace('  </style>', FULLSCREEN_CSS + '\n  </style>', 1)
    html = html.replace('  <div id="map"></div>', '  <div id="map">\n' + FULLSCREEN_BUTTON + '  </div>', 1)
    html = html.replace('    syncControls();', FULLSCREEN_JS + '\n    syncControls();', 1)
    mini_app.APP_HTML = html
