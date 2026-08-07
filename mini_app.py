from __future__ import annotations

from fastapi import Query
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

import address_search_v2
import launcher as bot

APP_URL = f"{bot.settings().public_url}/app"


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("🚀 Запустити DUGA", web_app=WebAppInfo(url=APP_URL))],
        [KeyboardButton(bot.BTN_RESTART)],
        [KeyboardButton(bot.BTN_CANCEL)],
    ]
    if bot.is_admin(user_id):
        rows.insert(1, [KeyboardButton(bot.BTN_USERS)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


@bot.api.get("/api/geocode")
async def api_geocode(q: str = Query(min_length=2, max_length=250)):
    try:
        items = await address_search_v2.robust_geocode(q)
    except Exception:
        bot.log.exception("Mini App geocoding failed")
        return JSONResponse({"results": []}, status_code=200)
    return {
        "results": [
            {
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "label": str(item.get("label") or q),
                "source": str(item.get("source") or "Карта"),
            }
            for item in items[:8]
        ]
    }


@bot.api.get("/app", response_class=HTMLResponse)
async def mini_app_page():
    return HTMLResponse(APP_HTML)


APP_HTML = r'''<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
  <title>DUGA</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root{--bg:#0f1117;--panel:#171a22;--card:#20242e;--text:#f4f6fb;--muted:#aeb5c4;--line:#343a46;--accent:#ef4444}
    *{box-sizing:border-box}html,body{height:100%;margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}
    body{display:flex;flex-direction:column;overflow:hidden}.top{padding:10px 12px;background:var(--panel);border-bottom:1px solid var(--line);display:grid;gap:8px}
    .title{display:flex;align-items:center;justify-content:space-between}.title b{font-size:18px}.title span{font-size:12px;color:var(--muted)}
    .search{display:flex;gap:7px}.search input{flex:1;min-width:0;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:10px;padding:11px 12px;font-size:15px}.search button,.btn{border:0;border-radius:10px;padding:10px 13px;font-weight:700;background:var(--accent);color:white}
    #results{display:none;max-height:160px;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}.result{padding:10px;border-bottom:1px solid var(--line);font-size:13px}.result:last-child{border-bottom:0}.result small{display:block;color:var(--muted);margin-top:3px}
    #map{flex:1;min-height:220px}.controls{padding:10px 12px;background:var(--panel);border-top:1px solid var(--line);display:grid;gap:9px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.field{display:grid;gap:5px}.field label{font-size:12px;color:var(--muted)}.field input{width:100%;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:10px;padding:10px;font-size:16px}
    .point-tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.point-tabs button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:9px;padding:9px 4px;font-weight:800}.point-tabs button.active{outline:2px solid #fff}.point-tabs button[data-i="0"].active{background:#b91c1c}.point-tabs button[data-i="1"].active{background:#1d4ed8}.point-tabs button[data-i="2"].active{background:#15803d}.point-tabs button.filled::after{content:' •';color:#fff}
    .radii{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.radii button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:9px;padding:9px 4px;font-weight:700}.radii button.active{background:var(--accent);border-color:var(--accent);color:white}
    .actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.secondary{background:var(--card);border:1px solid var(--line);color:var(--text)}.status{font-size:12px;color:var(--muted);min-height:16px}.leaflet-control-attribution{font-size:9px}.sector-canvas{position:absolute;pointer-events:none}.point-icon{width:28px;height:28px;border-radius:50%;border:3px solid #fff;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:900;box-shadow:0 1px 8px rgba(0,0,0,.55)}
  </style>
</head>
<body>
  <section class="top">
    <div class="title"><b>DUGA</b><span>До 3 секторів одночасно</span></div>
    <div class="search"><input id="address" placeholder="Адреса у будь-якому форматі"><button id="searchBtn">Знайти</button></div>
    <div id="results"></div>
  </section>
  <div id="map"></div>
  <section class="controls">
    <div class="point-tabs" id="pointTabs">
      <button data-i="0" class="active">Точка 1</button>
      <button data-i="1">Точка 2</button>
      <button data-i="2">Точка 3</button>
    </div>
    <div class="grid">
      <div class="field"><label for="azimuth">Азимут активної точки, 0–359°</label><input id="azimuth" type="number" min="0" max="359" step="1" value="0"></div>
      <div class="field"><label>Координати активної точки</label><input id="coords" readonly value="Натисніть на карту"></div>
    </div>
    <div class="radii" id="radii"><button data-r="1">1 км</button><button data-r="3" class="active">3 км</button><button data-r="5">5 км</button><button data-r="10">10 км</button></div>
    <div class="actions"><button id="clearBtn" class="btn secondary">Очистити точку</button><button id="fitBtn" class="btn">Показати всі</button></div>
    <div class="status" id="status">Точка 1: натисніть на карту або знайдіть адресу.</div>
  </section>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand();tg.setHeaderColor('#171a22');tg.setBackgroundColor('#0f1117')}
    const map=L.map('map',{zoomControl:true}).setView([50.2547,28.6587],11);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
    map.createPane('sectorPane');map.getPane('sectorPane').style.zIndex='350';map.getPane('sectorPane').style.pointerEvents='none';
    const R=6371008.8,rad=x=>x*Math.PI/180,deg=x=>x*180/Math.PI;
    function dest(lat,lon,bearing,distance){const p1=rad(lat),l1=rad(lon),t=rad((bearing%360+360)%360),d=distance/R;const p2=Math.asin(Math.sin(p1)*Math.cos(d)+Math.cos(p1)*Math.sin(d)*Math.cos(t));const l2=l1+Math.atan2(Math.sin(t)*Math.sin(d)*Math.cos(p1),Math.cos(d)-Math.sin(p1)*Math.sin(p2));return L.latLng(deg(p2),((deg(l2)+540)%360)-180)}
    const COLORS=[{css:'#dc2626',rgb:'220,38,38'},{css:'#2563eb',rgb:'37,99,235'},{css:'#16a34a',rgb:'22,163,74'}];
    const points=[0,1,2].map(i=>({lat:null,lon:null,label:'',bearing:0,radiusKm:3,marker:null,sector:null,color:COLORS[i]}));
    let activeIndex=0;
    const coords=document.getElementById('coords'),az=document.getElementById('azimuth'),status=document.getElementById('status'),results=document.getElementById('results');
    function pointIcon(i){return L.divIcon({className:'',html:`<div class="point-icon" style="background:${points[i].color.css}">${i+1}</div>`,iconSize:[28,28],iconAnchor:[14,14]})}
    class SectorLayer extends L.Layer{constructor(cfg){super();this.cfg=cfg;this.draw=this.draw.bind(this)}onAdd(m){this.m=m;this.c=L.DomUtil.create('canvas','sector-canvas');m.getPane('sectorPane').appendChild(this.c);m.on('move zoom resize viewreset',this.draw);this.draw()}onRemove(m){m.off('move zoom resize viewreset',this.draw);L.DomUtil.remove(this.c)}draw(){const {lat,lon,bearing,distance,rgb}=this.cfg,s=this.m.getSize(),tl=this.m.containerPointToLayerPoint([0,0]);L.DomUtil.setPosition(this.c,tl);const q=Math.max(1,devicePixelRatio||1);this.c.style.width=s.x+'px';this.c.style.height=s.y+'px';this.c.width=s.x*q;this.c.height=s.y*q;const x=this.c.getContext('2d');x.setTransform(q,0,0,q,0,0);x.clearRect(0,0,s.x,s.y);const center=L.latLng(lat,lon),arc=[];for(let o=-60;o<=60;o+=2)arc.push(dest(lat,lon,bearing+o,distance));const cp=this.m.latLngToContainerPoint(center),ap=arc.map(p=>this.m.latLngToContainerPoint(p)),lp=ap[0],rp=ap[ap.length-1],azp=this.m.latLngToContainerPoint(dest(lat,lon,bearing,distance)),rr=Math.hypot(azp.x-cp.x,azp.y-cp.y);x.save();x.beginPath();x.moveTo(cp.x,cp.y);ap.forEach(p=>x.lineTo(p.x,p.y));x.closePath();x.clip();const g=x.createRadialGradient(cp.x,cp.y,0,cp.x,cp.y,rr);g.addColorStop(0,`rgba(${rgb},.46)`);g.addColorStop(.55,`rgba(${rgb},.22)`);g.addColorStop(1,`rgba(${rgb},.04)`);x.fillStyle=g;x.fillRect(0,0,s.x,s.y);x.restore();x.strokeStyle=`rgba(${rgb},.95)`;x.lineWidth=3;x.beginPath();x.moveTo(cp.x,cp.y);x.lineTo(lp.x,lp.y);x.moveTo(cp.x,cp.y);x.lineTo(rp.x,rp.y);x.stroke();x.lineWidth=2;x.beginPath();ap.forEach((p,i)=>i?x.lineTo(p.x,p.y):x.moveTo(p.x,p.y));x.stroke()}}
    function syncTabs(){document.querySelectorAll('#pointTabs button').forEach((b,i)=>{b.classList.toggle('active',i===activeIndex);b.classList.toggle('filled',points[i].lat!==null)})}
    function syncControls(){const p=points[activeIndex];az.value=String(p.bearing);coords.value=p.lat===null?'Натисніть на карту':`${p.lat.toFixed(7)}, ${p.lon.toFixed(7)}`;document.querySelectorAll('#radii button').forEach(b=>b.classList.toggle('active',Number(b.dataset.r)===p.radiusKm));syncTabs();status.textContent=p.lat===null?`Точка ${activeIndex+1}: натисніть на карту або знайдіть адресу.`:`Точка ${activeIndex+1}: азимут ${p.bearing}° · радіус ${p.radiusKm} км`}
    function activate(i){activeIndex=i;syncControls()}
    function chooseFor(i,lat,lon,label='Точка на карті'){const p=points[i];p.lat=lat;p.lon=lon;p.label=label;if(!p.marker){p.marker=L.marker([lat,lon],{draggable:true,icon:pointIcon(i)}).addTo(map);p.marker.on('dragend',e=>{const q=e.target.getLatLng();chooseFor(i,q.lat,q.lng,'Переміщена точка')})}else p.marker.setLatLng([lat,lon]);drawSector(i,false);if(i===activeIndex)syncControls()}
    function drawSector(i,fit=false){const p=points[i];if(p.lat===null)return;if(p.sector)map.removeLayer(p.sector);p.sector=new SectorLayer({lat:p.lat,lon:p.lon,bearing:p.bearing,distance:p.radiusKm*1000,rgb:p.color.rgb}).addTo(map);if(fit)fitAll()}
    function fitAll(){const all=[];points.forEach(p=>{if(p.lat===null)return;all.push(L.latLng(p.lat,p.lon));for(let o=-60;o<=60;o+=10)all.push(dest(p.lat,p.lon,p.bearing+o,p.radiusKm*1000))});if(all.length)map.fitBounds(L.latLngBounds(all).pad(.15));else status.textContent='Спочатку додайте хоча б одну точку.'}
    function clearPoint(i){const p=points[i];if(p.marker){map.removeLayer(p.marker);p.marker=null}if(p.sector){map.removeLayer(p.sector);p.sector=null}p.lat=null;p.lon=null;p.label='';p.bearing=0;p.radiusKm=3;syncControls()}
    document.querySelectorAll('#pointTabs button').forEach(b=>b.addEventListener('click',()=>activate(Number(b.dataset.i))));
    map.on('click',e=>chooseFor(activeIndex,e.latlng.lat,e.latlng.lng));
    az.addEventListener('input',()=>{const v=Number(az.value);if(!Number.isFinite(v)||v<0||v>=360){status.textContent='Азимут має бути від 0 до 359';return}points[activeIndex].bearing=v;drawSector(activeIndex,false);syncControls()});
    document.querySelectorAll('#radii button').forEach(b=>b.addEventListener('click',()=>{points[activeIndex].radiusKm=Number(b.dataset.r);drawSector(activeIndex,false);syncControls()}));
    document.getElementById('clearBtn').addEventListener('click',()=>clearPoint(activeIndex));document.getElementById('fitBtn').addEventListener('click',fitAll);
    async function searchAddress(){const q=document.getElementById('address').value.trim();if(q.length<2)return;status.textContent=`Пошук адреси для точки ${activeIndex+1}…`;results.innerHTML='';results.style.display='block';try{const r=await fetch('/api/geocode?q='+encodeURIComponent(q));const data=await r.json();if(!data.results?.length){results.innerHTML='<div class="result">Нічого не знайдено</div>';status.textContent='Уточніть адресу';return}data.results.forEach(item=>{const d=document.createElement('div');d.className='result';d.innerHTML=`${item.label}<small>${item.source}</small>`;d.addEventListener('click',()=>{chooseFor(activeIndex,item.lat,item.lon,item.label);map.setView([item.lat,item.lon],16);results.style.display='none'});results.appendChild(d)});status.textContent=`Точка ${activeIndex+1}: оберіть правильний варіант`}catch(e){results.innerHTML='<div class="result">Помилка пошуку</div>';status.textContent='Сервіс пошуку недоступний'}}
    document.getElementById('searchBtn').addEventListener('click',searchAddress);document.getElementById('address').addEventListener('keydown',e=>{if(e.key==='Enter')searchAddress()});
    map.locate({setView:false,maxZoom:16});map.on('locationfound',e=>{if(points.every(p=>p.lat===null)){chooseFor(0,e.latlng.lat,e.latlng.lng,'Ваше поточне місцезнаходження');map.setView(e.latlng,15)}});
    syncControls();
  </script>
</body>
</html>'''


bot.main_keyboard = main_keyboard
