from __future__ import annotations

import mini_app


COMMON_POLYGON_CSS = r'''
    .common-polygon-btn{width:100%;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:10px;padding:10px 13px;font-weight:800}
    .common-polygon-btn.active{background:#a16207;border-color:#facc15;color:#fff}
'''

COMMON_POLYGON_BUTTON = r'''
    <button id="commonPolygonBtn" class="common-polygon-btn" type="button">⬡ Спільний полігон</button>
'''

COMMON_POLYGON_JS = r'''
    const commonPolygonBtn=document.getElementById('commonPolygonBtn');
    let commonPolygonLayer=null,commonPolygonMode=false;

    function polyArea(poly){
      let a=0;
      for(let i=0;i<poly.length;i++){
        const p=poly[i],q=poly[(i+1)%poly.length];
        a+=p.x*q.y-q.x*p.y;
      }
      return a/2;
    }

    function ccw(poly){return polyArea(poly)<0?[...poly].reverse():poly}

    function sectorPolygon(p){
      const poly=[{x:p.lon,y:p.lat}];
      for(let o=-60;o<=60;o+=2){
        const q=dest(p.lat,p.lon,p.bearing+o,p.radiusKm*1000);
        poly.push({x:q.lng,y:q.lat});
      }
      return ccw(poly);
    }

    function cross(a,b,p){return (b.x-a.x)*(p.y-a.y)-(b.y-a.y)*(p.x-a.x)}

    function segmentLineIntersection(s,e,a,b){
      const dx1=e.x-s.x,dy1=e.y-s.y,dx2=b.x-a.x,dy2=b.y-a.y;
      const den=dx1*dy2-dy1*dx2;
      if(Math.abs(den)<1e-15)return e;
      const t=((a.x-s.x)*dy2-(a.y-s.y)*dx2)/den;
      return {x:s.x+t*dx1,y:s.y+t*dy1};
    }

    function clipConvex(subject,clip){
      let output=[...subject];
      for(let j=0;j<clip.length;j++){
        const a=clip[j],b=clip[(j+1)%clip.length],input=output;
        output=[];
        if(!input.length)break;
        let s=input[input.length-1];
        for(const e of input){
          const eInside=cross(a,b,e)>=-1e-12;
          const sInside=cross(a,b,s)>=-1e-12;
          if(eInside){
            if(!sInside)output.push(segmentLineIntersection(s,e,a,b));
            output.push(e);
          }else if(sInside){
            output.push(segmentLineIntersection(s,e,a,b));
          }
          s=e;
        }
      }
      return output;
    }

    function getCommonPolygon(){
      const active=points.filter(p=>p.lat!==null);
      if(active.length<2)return null;
      let intersection=sectorPolygon(active[0]);
      for(let i=1;i<active.length&&intersection.length>=3;i++)intersection=clipConvex(intersection,sectorPolygon(active[i]));
      return intersection.length>=3?intersection:null;
    }

    function hideSectorLayers(){
      points.forEach(p=>{
        if(p.sector&&map.hasLayer(p.sector))map.removeLayer(p.sector);
        if(p.marker&&map.hasLayer(p.marker))map.removeLayer(p.marker);
      });
    }

    function restoreSectorLayers(){
      points.forEach((p,i)=>{
        if(p.lat===null)return;
        if(p.marker&&!map.hasLayer(p.marker))p.marker.addTo(map);
        originalDrawSector(i,false);
      });
    }

    function removeCommonPolygon(){
      if(commonPolygonLayer){map.removeLayer(commonPolygonLayer);commonPolygonLayer=null}
    }

    function renderCommonPolygon(){
      if(!commonPolygonMode)return;
      const poly=getCommonPolygon();
      removeCommonPolygon();
      if(!poly){
        commonPolygonMode=false;
        commonPolygonBtn.classList.remove('active');
        commonPolygonBtn.textContent='⬡ Спільний полігон';
        restoreSectorLayers();
        status.textContent='Спільна область секторів відсутня.';
        return;
      }
      hideSectorLayers();
      const latlngs=poly.map(p=>L.latLng(p.y,p.x));
      commonPolygonLayer=L.polygon(latlngs,{color:'#facc15',weight:4,opacity:1,fill:false,interactive:false}).addTo(map);
      map.fitBounds(commonPolygonLayer.getBounds().pad(.20));
      status.textContent='Спільний полігон: показано лише область перетину секторів.';
    }

    function enterCommonPolygon(){
      const poly=getCommonPolygon();
      if(!poly){status.textContent=points.filter(p=>p.lat!==null).length<2?'Додайте щонайменше 2 точки.':'Сектори не мають спільної області.';return}
      commonPolygonMode=true;
      commonPolygonBtn.classList.add('active');
      commonPolygonBtn.textContent='↩ Показати сектори';
      renderCommonPolygon();
    }

    function exitCommonPolygon(){
      commonPolygonMode=false;
      removeCommonPolygon();
      restoreSectorLayers();
      commonPolygonBtn.classList.remove('active');
      commonPolygonBtn.textContent='⬡ Спільний полігон';
      fitAll();
      status.textContent='Показано всі сектори.';
    }

    const originalDrawSector=drawSector;
    drawSector=function(i,fit=false){
      originalDrawSector(i,fit);
      if(commonPolygonMode)setTimeout(renderCommonPolygon,0);
    };

    commonPolygonBtn.addEventListener('click',()=>commonPolygonMode?exitCommonPolygon():enterCommonPolygon());
    document.getElementById('clearBtn').addEventListener('click',()=>{if(commonPolygonMode)setTimeout(renderCommonPolygon,0)});
'''

html = mini_app.APP_HTML
if 'id="commonPolygonBtn"' not in html:
    html = html.replace('  </style>', COMMON_POLYGON_CSS + '\n  </style>', 1)
    html = html.replace('    <div class="status" id="status">', COMMON_POLYGON_BUTTON + '\n    <div class="status" id="status">', 1)
    html = html.replace('    syncControls();', COMMON_POLYGON_JS + '\n    syncControls();', 1)
    mini_app.APP_HTML = html
