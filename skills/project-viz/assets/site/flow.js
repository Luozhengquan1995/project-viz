(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const text = value => value == null ? '' : typeof value === 'object' ? String(value.summary || value.text || value.label || value.message || '') : String(value);
  const state = {
    snapshot:null, revision:null, server:new Map(), history:new Map(), historyVersions:new Map(), historyMembers:new Map(),
    historyLoaded:new Set(), pending:new Map(), errors:new Map(), expanded:new Set(),
    selected:null, positions:new Map(), edges:[], bounds:null, sides:new Map(),
    zoom:1, pan:{x:0,y:0}, size:{width:0,height:0}, initialized:false, follow:false, branchKey:'',
    offline:false, polling:false, timer:null, clickTimer:null, query:'', suppressClickUntil:0, drawerToken:0, drawerView:null,
    detailSignature:'', pointerPositions:new Map(), gesture:null, miniGesture:null, renderedNodes:'', renderedEdges:''
  };
  const kindLabels = {project:'项目',stage:'阶段',task:'工作',action:'执行步骤',history:'历史记录',folder:'文件夹',file:'项目文件'};
  const statusLabels = {active:'进行中',waiting:'等待中',recent:'最近更新',error:'出现错误',partial:'待验证',planned:'待开展',idle:'',unknown:'待确认'};
  const detailIcon = '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.4"/><path d="M10 9v5M10 6v.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
  const stateIcons = {
    active:'<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4Z" fill="currentColor" stroke="none"/>',
    done:'<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9Z" fill="currentColor" stroke="none"/>',
    error:'<path d="m12 2 10 10-10 10L2 12Z"/><path d="m9 9 6 6m0-6-6 6"/>',
    success:'<path d="m12 2 8.7 5v10L12 22l-8.7-5V7Z"/><path d="m7.5 12 3 3 6-6"/>',
    waiting:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
    partial:'<path d="m12 3 10 18H2Z"/><path d="M12 9v5m0 3v.1"/>',
    planned:'<rect x="4" y="4" width="16" height="16" rx="4" stroke-dasharray="3 3"/>',
    unknown:'<circle cx="12" cy="12" r="9" stroke-dasharray="3 3"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2l-1.5 1v2m0 3v.1"/>'
  };
  function visualStatus(node){
    const status=String(node.status||'unknown').toLowerCase(),outcome=String(typeof node.outcome==='string'?node.outcome:node.outcome?.status||'').toLowerCase();
    if(['active','running','in_progress'].includes(status))return 'active';
    if(['error','failed','aborted'].includes(status))return 'error';
    if(['waiting','pending'].includes(status))return 'waiting';
    if(['failed','error'].includes(outcome))return 'error';
    if(['success','passed','verified'].includes(status)||['success','passed','verified'].includes(outcome))return 'success';
    // An ended execution is not a successful outcome.
    if(['done','completed','finished','turn_complete'].includes(status))return 'done';
    if(status==='partial')return 'partial';
    if(['planned','idle'].includes(status))return 'planned';
    return 'unknown';
  }
  const stateIcon = status => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${stateIcons[status]||stateIcons.unknown}</svg>`;
  const fileIcon = '<svg viewBox="0 0 16 18" fill="none" aria-hidden="true"><path d="M3 1h6l4 4v12H3V1Z" stroke="currentColor" stroke-width="1.2"/><path d="M9 1v5h4M6 10h4M6 13h4" stroke="currentColor" stroke-width="1.1"/></svg>';
  // Semantic identities belong to the server. Never regroup sessions or derive
  // new work cards from execution records in the browser.
  const allNodes = () => new Map([...state.history,...state.server]);
  const rootId = () => String(state.snapshot?.rootId || 'project');
  const normalize = node => ({...node,id:String(node.id),parentId:node.parentId == null ? null : String(node.parentId),label:text(node.label) || '未命名节点'});
  const historyVersion = node => JSON.stringify([node?.updatedAt,node?.eventCount,node?.label,node?.summary]);
  const historyInView = id => state.drawerView==='node'&&$('detail-drawer').open&&ancestors(state.selected).includes(id);
  function statusLabel(node) {
    const status=visualStatus(node);
    if(status==='done')return ({project:'执行已结束',stage:'执行已结束',task:'执行已结束',action:'已返回',history:'历史记录'})[node.kind]||'已执行';
    if(status==='success')return '成功';
    return ({active:'进行中',error:'失败',waiting:'等待中',partial:'待验证',planned:'未开展',unknown:'待确认'})[status];
  }
  const displayLabel = node => node?.kind==='action'&&window.flowShortLabel?window.flowShortLabel(node):text(node?.label);
  // Records and history pagination live in the drawer, not on the project graph.
  function isWorkNode(node,nodes=allNodes()){
    return Boolean(node&&['project','stage','task'].includes(node.kind)&&ancestors(node.id,nodes).every(id=>nodes.get(id)?.kind!=='history'));
  }
  const graphNodes = () => {const nodes=allNodes();return new Map([...nodes].filter(([,node])=>isWorkNode(node,nodes)));};
  function workOwner(id,nodes=allNodes()) {return ancestors(id,nodes).reverse().find(key=>isWorkNode(nodes.get(key),nodes))||rootId();}
  function timeLabel(value,full=false) { if(!value)return '';const d=new Date(typeof value==='number'&&value<1e12?value*1000:value);return Number.isNaN(+d)?'':new Intl.DateTimeFormat('zh-CN',full?{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}:{hour:'2-digit',minute:'2-digit',hour12:false}).format(d); }
  function setText(id,value){if($(id).textContent!==value)$(id).textContent=value;}
  async function fetchJson(url) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
    try{const response=await fetch(url,{cache:'no-store',credentials:'same-origin',signal:controller.signal});if(!response.ok)throw new Error(response.status===401||response.status===403?'访问凭据已失效，请重新打开授权链接。':`服务暂时不可用（${response.status}）`);return await response.json();}finally{clearTimeout(timer);}
  }
  function indexChildren(nodes){const children=new Map();nodes.forEach(node=>{if(!children.has(node.parentId))children.set(node.parentId,[]);children.get(node.parentId).push(node);});return children;}
  function ancestors(id,nodes=allNodes()){const path=[],seen=new Set();let node=nodes.get(id);while(node&&!seen.has(node.id)){seen.add(node.id);path.unshift(node.id);node=nodes.get(node.parentId);}return path;}
  function canExpand(node,children){return Boolean(children.get(node?.id)?.length);}
  function activePath(){const nodes=allNodes();return [...new Set((state.snapshot?.activePath||[]).map(id=>workOwner(String(id),nodes)))];}
  function dimensions(node){return {width:238,height:node.kind==='project'?84:66,overview:node.kind==='stage'};}
  function layout(){
    const nodes=graphNodes(),children=indexChildren(nodes),positions=new Map(),edges=[],measures=new Map(),walking=new Set();
    const visibleChildren=id=>state.expanded.has(id)?(children.get(id)||[]):[];
    const gapFor=kids=>kids.length&&kids.every(node=>node.kind==='action')?12:20;
    const measure=id=>{
      if(measures.has(id))return measures.get(id);const node=nodes.get(id);if(!node||walking.has(id))return 0;walking.add(id);
      const own=dimensions(node),kids=visibleChildren(id),total=kids.reduce((sum,child)=>sum+measure(child.id),0)+Math.max(0,kids.length-1)*gapFor(kids);
      walking.delete(id);const height=Math.max(own.height,total);measures.set(id,height);return height;
    };
    const place=(id,x,y,side,seen=new Set())=>{
      if(seen.has(id)||!nodes.has(id)||positions.has(id))return;const nextSeen=new Set(seen);nextSeen.add(id);
      const node=nodes.get(id),size=dimensions(node);positions.set(id,{id,node,x,y,...size,side});
      const kids=visibleChildren(id),gap=gapFor(kids),total=kids.reduce((sum,child)=>sum+measure(child.id),0)+Math.max(0,kids.length-1)*gap;let cursor=y-total/2;
      kids.forEach(child=>{const h=measure(child.id),cx=x+side*(size.width/2+105+dimensions(child).width/2),cy=cursor+h/2;edges.push({from:id,to:child.id,side});place(child.id,cx,cy,side,nextSeen);cursor+=h+gap;});
    };
    const root=nodes.get(rootId());if(!root)return {positions,edges,bounds:null};
    const roots=visibleChildren(root.id),bilateral=roots.length>=2;
    if(bilateral&&roots.length){
      const size=dimensions(root);positions.set(root.id,{id:root.id,node:root,x:0,y:0,...size,side:1});
      const left=[],right=[];
      const largest=[...roots].sort((a,b)=>measure(b.id)-measure(a.id))[0];
      const isolateLarge=root.id!==rootId()&&largest&&measure(largest.id)>roots.filter(node=>node.id!==largest.id).reduce((sum,node)=>sum+measure(node.id),0)*.75;
      roots.forEach((node,i)=>{let side;if(root.id===rootId()){if(!state.sides.has(node.id))state.sides.set(node.id,i<Math.ceil(roots.length/2)?-1:1);side=state.sides.get(node.id);}else side=isolateLarge?(node.id===largest.id?1:-1):(i<Math.ceil(roots.length/2)?-1:1);(side<0?left:right).push(node);});
      [[left,-1],[right,1]].forEach(([list,side])=>{const gap=gapFor(list),total=list.reduce((sum,node)=>sum+measure(node.id),0)+Math.max(0,list.length-1)*gap;let cursor=-total/2;list.forEach(node=>{const height=measure(node.id);edges.push({from:root.id,to:node.id,side});place(node.id,side*(size.width/2+115+dimensions(node).width/2),cursor+height/2,side,new Set([root.id]));cursor+=height+gap;});});
    }else place(root.id,0,0,1);
    const values=[...positions.values()];
    const bounds=values.length?{left:Math.min(...values.map(p=>p.x-p.width/2)),right:Math.max(...values.map(p=>p.x+p.width/2)),top:Math.min(...values.map(p=>p.y-p.height/2)),bottom:Math.max(...values.map(p=>p.y+p.height/2))}:null;
    return {positions,edges,bounds,children};
  }
  function reconcileNodes(html){
    const template=document.createElement('template');template.innerHTML=html;
    const existing=new Map([...$('node-layer').children].map(node=>[node.dataset.nodeId,node]));
    for(const incoming of template.content.children){
      const old=existing.get(incoming.dataset.nodeId);
      if(!old){$('node-layer').appendChild(incoming.cloneNode(true));continue;}
      existing.delete(incoming.dataset.nodeId);
      for(const attribute of incoming.attributes)if(old.getAttribute(attribute.name)!==attribute.value)old.setAttribute(attribute.name,attribute.value);
      for(const selector of ['.node-main','.node-toggle','.node-detail']){
        const button=old.querySelector(selector),next=incoming.querySelector(selector);
        for(const attribute of [...button.attributes])if(!next.hasAttribute(attribute.name))button.removeAttribute(attribute.name);
        for(const attribute of next.attributes)if(button.getAttribute(attribute.name)!==attribute.value)button.setAttribute(attribute.name,attribute.value);
        if(button.innerHTML!==next.innerHTML)button.innerHTML=next.innerHTML;
      }
    }
    existing.forEach(node=>node.remove());
  }
  function renderGraph({anchorId=null,fit=false}={}){
    const old=anchorId?state.positions.get(anchorId):null,anchor=old?{x:state.pan.x+old.x*state.zoom,y:state.pan.y+old.y*state.zoom}:null;
    const result=layout();state.positions=result.positions;state.edges=result.edges;state.bounds=result.bounds;
    const path=new Set(activePath());
    const nodeHTML=[...state.positions.values()].map(p=>{
      const node=p.node,open=state.expanded.has(node.id),expandable=canExpand(node,result.children),status=statusLabel(node),visual=visualStatus(node),visibleLabel=displayLabel(node);
      const count=(result.children.get(node.id)||[]).length;
      const label=open?'收起分支':`展开 ${count} 项工作`;
      const isActive=visual==='active',interaction=expandable?label:'选中任务，使用详情按钮查看过程';
      return `<article class="flow-node kind-${esc(node.kind||'task')}${node.isSubtask?' is-subtask':''}${p.overview?' overview-stage':''}${isActive?' is-active':''}${state.selected===node.id?' is-selected':''}" data-node-id="${esc(node.id)}" data-status="${esc(node.status||'unknown')}" data-visual-status="${visual}" style="left:${p.x-p.width/2}px;top:${p.y-p.height/2}px;width:${p.width}px;height:${p.height}px"><button class="node-main" type="button" data-activate="${esc(node.id)}" aria-label="${esc(visibleLabel)}，${esc(status)}，${esc(interaction)}" ${expandable?`aria-expanded="${open}"`:''} title="${esc(status)} · ${esc(interaction)}"><span class="node-state-icon" data-visual-status="${visual}" aria-hidden="true">${stateIcon(visual)}</span><span class="node-label">${esc(visibleLabel)}</span></button><button class="node-toggle" type="button" data-toggle="${esc(node.id)}" ${expandable?'':'hidden'} aria-expanded="${open}" aria-label="${esc(visibleLabel)}，${esc(label)}" title="${esc(label)}"><span aria-hidden="true">${open?'−':'+'}</span></button><button class="node-detail" type="button" data-detail="${esc(node.id)}" aria-label="查看${esc(visibleLabel)}详情" aria-haspopup="dialog" title="查看详情">${detailIcon}</button></article>`;
    }).join('');
    const edgeHTML=state.edges.map(edge=>{
      const a=state.positions.get(edge.from),b=state.positions.get(edge.to);if(!a||!b)return '';
      const x=a.x+edge.side*a.width/2,tx=b.x-edge.side*b.width/2,bend=(x+tx)/2;
      return `<path class="flow-edge${path.has(edge.from)&&path.has(edge.to)?' is-active':''}${state.selected===edge.to?' is-selected':''}" data-visual-status="${visualStatus(b.node)}" data-edge-id="${esc(edge.from+'→'+edge.to)}" data-from="${esc(edge.from)}" data-to="${esc(edge.to)}" d="M${x},${a.y} C${bend},${a.y} ${bend},${b.y} ${tx},${b.y}"/>`;
    }).join('');
    const focusedElement=document.activeElement,focused=focusedElement?.closest('[data-node-id]')?.dataset.nodeId;
    if(nodeHTML!==state.renderedNodes){reconcileNodes(nodeHTML);state.renderedNodes=nodeHTML;if(focused&&!focusedElement.isConnected&&!$('detail-drawer').open)[...$('node-layer').querySelectorAll('[data-activate]')].find(button=>button.dataset.activate===focused)?.focus({preventScroll:true});}
    if(edgeHTML!==state.renderedEdges){$('edge-layer').innerHTML=edgeHTML;state.renderedEdges=edgeHTML;}
    $('canvas-empty').hidden=state.positions.size>0;
    $('selected-detail').hidden=!state.selected||!allNodes().has(state.selected);
    if(anchor&&state.positions.has(anchorId)){const now=state.positions.get(anchorId);state.pan={x:anchor.x-now.x*state.zoom,y:anchor.y-now.y*state.zoom};}
    renderBreadcrumbs();renderMinimap();const workNodes=[...graphNodes().values()],topics=workNodes.filter(node=>node.kind==='task'&&['stage','project'].includes(workNodes.find(parent=>parent.id===node.parentId)?.kind)).length;setText('canvas-note',`${topics} 项工作主题 · 展开查看关键步骤`);
    if(fit)fitView();else applyCamera();
  }
  function applyCamera(){
    $('flow-world').style.transform=`translate(${state.pan.x}px, ${state.pan.y}px) scale(${state.zoom})`;
    setText('zoom-label',`${Math.round(state.zoom*100)}%`);$('zoom-out').disabled=state.zoom<=.18;$('zoom-in').disabled=state.zoom>=2.2;
    const rect=$('minimap-window'),width=$('flow-canvas').clientWidth,height=$('flow-canvas').clientHeight;
    rect.setAttribute('x',String(-state.pan.x/state.zoom));rect.setAttribute('y',String(-state.pan.y/state.zoom));rect.setAttribute('width',String(width/state.zoom));rect.setAttribute('height',String(height/state.zoom));
  }
  function fitBounds(b){
    if(!b)return;const view=$('flow-canvas'),w=b.right-b.left,h=b.bottom-b.top;
    state.zoom=Math.max(.18,Math.min(1.12,(view.clientWidth-115)/Math.max(1,w),(view.clientHeight-125)/Math.max(1,h)));
    state.pan={x:view.clientWidth/2-(b.left+b.right)/2*state.zoom,y:(view.clientHeight-24)/2-(b.top+b.bottom)/2*state.zoom};applyCamera();
  }
  function fitView(){fitBounds(state.bounds);}
  function zoomAt(zoom,x=$('flow-canvas').clientWidth/2,y=$('flow-canvas').clientHeight/2){
    const next=Math.max(.18,Math.min(2.2,zoom)),world={x:(x-state.pan.x)/state.zoom,y:(y-state.pan.y)/state.zoom};
    state.pan={x:x-world.x*next,y:y-world.y*next};state.zoom=next;applyCamera();
  }
  function centerNode(id,zoom=Math.max(.95,state.zoom)){
    const node=state.positions.get(id);if(!node)return;state.zoom=Math.max(.18,Math.min(2.2,zoom));state.pan={x:$('flow-canvas').clientWidth/2-node.x*state.zoom,y:$('flow-canvas').clientHeight/2-node.y*state.zoom};applyCamera();
  }
  function renderMinimap(){
    if(!state.bounds)return;const b=state.bounds,pad=60;$('minimap').setAttribute('viewBox',`${b.left-pad} ${b.top-pad} ${b.right-b.left+pad*2} ${b.bottom-b.top+pad*2}`);
    $('minimap-nodes').innerHTML=[...state.positions.values()].map(p=>`<rect x="${p.x-p.width/2}" y="${p.y-p.height/2}" width="${p.width}" height="${p.height}" rx="12" data-visual-status="${visualStatus(p.node)}"/>`).join('');
  }
  function renderBreadcrumbs(){
    const nodes=allNodes(),path=ancestors(state.selected||rootId(),nodes).filter(id=>isWorkNode(nodes.get(id),nodes));
    $('breadcrumbs').innerHTML=path.length<=1?'<span>项目图谱</span>':path.map((id,i)=>`${i?'<span aria-hidden="true">/</span>':''}${i===path.length-1?`<span class="crumb-current">${esc(displayLabel(nodes.get(id)))}</span>`:`<button type="button" data-focus="${esc(id)}">${i===0?'整个项目':esc(displayLabel(nodes.get(id)))}</button>`}`).join('');
  }
  function initializeGraph(){
    state.expanded=new Set([rootId(),...[...graphNodes().values()].filter(node=>node.kind==='stage').map(node=>node.id)]);
    state.selected=null;renderGraph({fit:true});
  }
  function locateCurrent(){
    const nodes=allNodes(),path=activePath();
    const task=[...path].reverse().find(id=>nodes.get(id)?.kind==='task');
    if(!task){setText('announcer','暂未观察到当前执行任务。');return;}
    focusBranch(task);setText('announcer',`已在图中定位${displayLabel(nodes.get(task))}`);
  }
  function focusBranch(id){
    const nodes=allNodes();id=workOwner(id,nodes);if(!nodes.has(id))return;
    // Navigation changes only the camera and required expansion; this is always
    // the same project tree, with the user's other branches kept intact.
    ancestors(id,nodes).forEach(key=>state.expanded.add(key));state.selected=id;renderGraph();
    if(id===rootId())return fitView();
    const branch=[...state.positions.values()].filter(position=>ancestors(position.id,nodes).includes(id));
    if(branch.length)fitBounds({left:Math.min(...branch.map(p=>p.x-p.width/2)),right:Math.max(...branch.map(p=>p.x+p.width/2)),top:Math.min(...branch.map(p=>p.y-p.height/2)),bottom:Math.max(...branch.map(p=>p.y+p.height/2))});
  }
  async function activateNode(id){
    if(Date.now()<state.suppressClickUntil)return;const nodes=allNodes(),node=nodes.get(id);if(!node)return;
    state.selected=id;const children=indexChildren(graphNodes());
    if(!canExpand(node,children)){renderGraph();return;}
    if(state.errors.has(id)){state.expanded.add(id);renderGraph({anchorId:id});return void loadHistory(id);}
    if(state.expanded.has(id))state.expanded.delete(id);else state.expanded.add(id);
    renderGraph({anchorId:id});
    if(state.expanded.has(id)&&node.kind==='history'&&!state.historyLoaded.has(id))await loadHistory(id);
  }
  async function loadHistory(id,{fit=false,automatic=false}={}){
    if(state.pending.has(id))return state.pending.get(id);const owner=allNodes().get(id),version=historyVersion(owner),refresh=[];
    const startingCamera={zoom:state.zoom,x:state.pan.x,y:state.pan.y};
    const promise=(async()=>{
      try{
        const data=await fetchJson(`/api/history?id=${encodeURIComponent(id)}`);if(!Array.isArray(data.nodes))throw new Error('记录暂时无法读取');
        const members=new Set(data.nodes.map(node=>String(node.id))),previous=state.historyMembers.get(id)||new Set();
        previous.forEach(child=>{if(!members.has(child)&&state.history.get(child)?.parentId===id)state.history.delete(child);});
        data.nodes.forEach(raw=>{const node=normalize(raw);if(node.kind==='history'&&state.historyLoaded.has(node.id)&&(historyVersion(node)!==state.historyVersions.get(node.id)||(!node.updatedAt&&previous.has(node.id)))){state.historyLoaded.delete(node.id);if(historyInView(node.id))refresh.push(node.id);}state.history.set(node.id,node);});
        state.historyMembers.set(id,members);state.historyVersions.set(id,version);state.historyLoaded.add(id);state.errors.delete(id);
      }catch(error){state.errors.set(id,error.message);setText('announcer','历史记录暂时离线，已保留原有图谱。');}
      finally{state.pending.delete(id);const unchangedCamera=state.zoom===startingCamera.zoom&&state.pan.x===startingCamera.x&&state.pan.y===startingCamera.y;renderGraph({anchorId:automatic?null:id,fit:fit&&unchangedCamera&&state.selected===id});if(state.drawerView==='node'&&$('detail-drawer').open)renderDetail(true);refresh.forEach(child=>void loadHistory(child,{automatic:true}));}
    })();state.pending.set(id,promise);renderGraph();return promise;
  }
  function updateCoverage(){
    const coverage=state.snapshot?.coverage;
    if(!coverage||typeof coverage!=='object'){
      setText('coverage-label','覆盖待确认');
      $('coverage-content').innerHTML='<p>服务尚未报告采集范围。</p>';
      return;
    }
    const count=value=>Number.isFinite(Number(value))&&Number(value)>=0?Number(value):null;
    const sources=count(coverage.sourceCount),indexed=count(coverage.indexedCount);
    const pending=Array.isArray(coverage.pendingSources)?coverage.pendingSources.length:count(coverage.pendingSources);
    const status=({disabled:'未启用采集',paused:'采集已暂停',degraded:'采集受限',indexing:'正在索引',watching:'来源同步中',ready:'来源已索引'})[coverage.status]||'采集覆盖';
    setText('coverage-label',pending>0?`待索引 ${pending} 个来源`:sources===0?'尚无采集来源':status);
    const complete=coverage.historyComplete===true?'已登记来源的历史已索引。':coverage.historyComplete===false?'历史索引尚未完成。':'历史完整性尚未确认。';
    const warnings=Array.isArray(coverage.warnings)?coverage.warnings:[];
    const html=`<p>${esc(status)}</p><p>${sources==null?'来源数未报告':`已登记 ${sources} 个来源`}${indexed==null?'':` · 索引计数 ${indexed}`}</p><p>${esc(complete)}${pending>0?` 尚有 ${pending} 个来源待处理。`:''}</p>${warnings.length?`<ul>${warnings.map(warning=>`<li>${esc(text(warning)||String(warning))}</li>`).join('')}</ul>`:''}<p class="coverage-note">这里只表示已登记来源的覆盖情况。</p>`;
    if($('coverage-content').innerHTML!==html)$('coverage-content').innerHTML=html;
  }
  function updateHeader(){
    updateCoverage();
    const data=state.snapshot,monitor=state.offline?'offline':data?.monitorStatus||'starting';$('connection').dataset.state=monitor;
    setText('connection-label',({watching:'实时同步',starting:'正在连接',degraded:'监测受限',offline:'暂时离线'})[monitor]||'状态待确认');
    const warning=state.offline?'连接暂时中断，已保留当前图谱；正在自动重连。':monitor==='degraded'?'部分记录暂时无法同步，当前显示最近可用数据。':'';$('offline-notice').hidden=!warning;setText('offline-message',warning);
    if(!data)return;const nodes=allNodes(),title=displayLabel(nodes.get(rootId()))||text(data.project?.title)||'Project Viz';setText('project-name',title);document.title=`${title} · Project Viz`;
    const path=activePath(),latest=[...path].reverse().map(id=>nodes.get(id)).find(node=>node?.kind==='task'),summary=text(data.currentActivity)||text(latest?.summary)||'';setText('activity-summary',latest?displayLabel(latest):'等待新的项目工作');$('activity-summary').title=summary?'在图中定位 · '+summary:'在图中定位正在执行的任务';$('activity-summary').disabled=!latest;
    $('current-dot').hidden=!(data.activeNodeIds||[]).length;
  }
  function renderSearch(){
    const query=state.query.trim().toLocaleLowerCase();$('search-results').hidden=!query;$('node-search').setAttribute('aria-expanded',String(Boolean(query)));if(!query)return;
    const nodes=allNodes(),owners=new Set();nodes.forEach(node=>{if([node.label,node.summary,...(Array.isArray(node.files)?node.files:[])].join(' ').toLocaleLowerCase().includes(query))owners.add(workOwner(node.id,nodes));});
    const matches=[...owners].map(id=>nodes.get(id)).filter(Boolean).slice(0,8);
    $('search-results').innerHTML=matches.length?matches.map(node=>`<button type="button" data-search-node="${esc(node.id)}"><span>${esc(displayLabel(node))}</span><small>${esc(kindLabels[node.kind]||'任务')} · ${esc(nodes.get(node.parentId)?displayLabel(nodes.get(node.parentId)):'整个项目')}</small></button>`).join(''):'<div class="search-empty">没有匹配的任务。</div>';
  }
  function locateSearch(id){
    const nodes=allNodes();id=workOwner(id,nodes);const path=ancestors(id,nodes);if(!path.length)return;
    path.filter(key=>key!==id).forEach(key=>state.expanded.add(key));state.selected=id;
    $('search-results').hidden=true;$('node-search').setAttribute('aria-expanded','false');renderGraph();centerNode(id,1.05);
    setText('announcer',`已定位到${nodes.get(id).label}`);
  }
  function detailText(value){if(value==null)return '';if(Array.isArray(value))return value.map(detailText).filter(Boolean).join('\n');if(typeof value==='object')return Object.entries(value).map(([key,item])=>`${key}：${detailText(item)}`).join('\n');return String(value);}
  function openDrawer(){if(!$('detail-drawer').open)$('detail-drawer').showModal();$('close-detail').focus();}
  function detailContent(node,nodes){
    const children=indexChildren(nodes),records=[],groups=[],seen=new Set();
    const walk=id=>{if(seen.has(id))return;seen.add(id);for(const child of children.get(id)||[]){
      if(child.kind==='action')records.push(child);
      else groups.push(child);
    }};
    walk(node.id);for(const id of node.relatedNodeIds||[]){const related=nodes.get(id);if(related&&!groups.some(group=>group.id===id))groups.push(related);}return {records,groups};
  }
  function renderDetail(preserve=false){
    const nodes=allNodes(),node=nodes.get(state.selected);if(!node)return;
    const {records,groups}=detailContent(node,nodes),signature=JSON.stringify([node,records,groups,state.pending.has(node.id),state.errors.get(node.id),state.historyLoaded.has(node.id)]);
    if(preserve&&signature===state.detailSignature)return;state.detailSignature=signature;
    const focused=preserve&&$('drawer-content').contains(document.activeElement)?document.activeElement:null;
    const focusSection=focused?.tagName==='SUMMARY'?focused.parentElement.dataset.section:null;
    const focusAttribute=focused?[...focused.attributes].find(attr=>attr.name.startsWith('data-')):null;
    const opened=new Set(preserve?[...$('drawer-content').querySelectorAll('details[open][data-section]')].map(el=>el.dataset.section):[]);
    const scroll=preserve?$('drawer-content').scrollTop:0,status=statusLabel(node),files=[...new Set([...(Array.isArray(node.files)?node.files:[]),...records.flatMap(record=>Array.isArray(record.files)?record.files:[])])].filter(file=>typeof file==='string');
    const disclosure=(key,title,content)=>`<details class="detail-section disclosure" data-section="${esc(key)}" ${opened.has(key)?'open':''}><summary>${esc(title)}</summary>${content}</details>`;
    const isWork=isWorkNode(node,nodes),parent=node.parentId&&nodes.get(node.parentId),lazy=node.kind==='history'&&!state.historyLoaded.has(node.id),pending=state.pending.has(node.id),error=state.errors.get(node.id);
    const heading=displayLabel(node),fullLabel=node.label!==heading?`<p class="detail-summary">${esc(node.label)}</p>`:'',summary=node.summary&&text(node.summary)!==node.label?`<p class="detail-summary">${esc(text(node.summary))}</p>`:'';
    const recordRows=[...records].reverse().map(record=>`<details class="execution-record" data-section="record:${esc(record.id)}" data-record-id="${esc(record.id)}" data-status="${esc(record.status)}" data-visual-status="${visualStatus(record)}" ${opened.has('record:'+record.id)?'open':''}><summary><span>${esc(displayLabel(record))}</span><small>${esc(statusLabel(record))}</small></summary><div class="record-body"><p class="record-label">${esc(record.label)}</p>${record.updatedAt?`<time>${esc(timeLabel(record.updatedAt,true))}</time>`:''}${record.summary?`<div class="detail-text">${esc(text(record.summary))}</div>`:''}${record.detail?`<pre class="record-text">${esc(detailText(record.detail))}</pre>`:''}</div></details>`).join('');
    const evidenceRows=(node.evidence||[]).map(item=>`<div class="evidence-item"><button type="button" class="detail-file" data-file="${esc(item.path||item.relative_path||'')}">${fileIcon}<span>${esc(item.path||item.relative_path||'')}${item.line?' · 第 '+esc(item.line)+' 行':''}</span></button>${item.note?`<p class="detail-summary">${esc(item.note)}</p>`:''}</div>`).join('');
    const evidenceInfo=evidenceRows?disclosure('evidence',`依据与结论 · ${(node.evidence||[]).length}`,`${node.sourceSnapshotAt?`<p class="detail-meta">证据快照：${esc(timeLabel(node.sourceSnapshotAt,true))}</p>`:''}${evidenceRows}`):'';
    const groupRows=groups.map(child=>`<button type="button" class="detail-group" data-detail="${esc(child.id)}"><span>${esc(child.kind==='task'&&isWorkNode(child,nodes)?displayLabel(child):child.label)}</span><span aria-hidden="true">›</span></button>`).join('');
    setText('detail-kicker',isWork?(kindLabels[node.kind]||'工作详情'):'过程记录');
    $('drawer-content').innerHTML=`${!isWork&&parent?`<button type="button" class="back-detail" data-detail="${esc(parent.id)}">← 返回${isWorkNode(parent,nodes)?'任务':'上级'}</button>`:''}${status?`<span class="detail-status" data-status="${esc(node.status)}" data-visual-status="${visualStatus(node)}">${esc(status)}</span>`:''}<h2 id="detail-title">${esc(heading)}</h2>${fullLabel}${summary}<div class="detail-meta">${node.updatedAt?`<span>更新于 ${esc(timeLabel(node.updatedAt,true))}</span>`:''}</div>${isWork?`<div class="detail-actions"><button type="button" class="detail-action" data-focus-detail="${esc(node.id)}">聚焦这个分支</button></div>`:''}${node.detail?disclosure('description','完整说明',`<div class="detail-text">${esc(detailText(node.detail))}</div>`):''}${evidenceInfo}${records.length?disclosure('execution',`执行记录 · ${records.length}`,`<div class="execution-records">${recordRows}</div>`):''}${groups.length?disclosure('groups',isWork?'相关工作与历史':'归档记录',groupRows):''}${lazy?`<div class="detail-section"><button type="button" class="detail-action" data-history-load="${esc(node.id)}" ${pending?'disabled':''}>${pending?'正在读取…':error?'重新读取记录':'查看归档记录'}</button>${error?`<p class="file-notice error">${esc(error)}</p>`:''}</div>`:''}${files.length?disclosure('files',`相关文件 · ${files.length}`,files.map(file=>`<button type="button" class="detail-file" data-file="${esc(file)}">${fileIcon}<span>${esc(file)}</span></button>`).join('')):''}`;
    if(focusSection)$('drawer-content').querySelector(`[data-section="${CSS.escape(focusSection)}"] > summary`)?.focus({preventScroll:true});
    else if(focusAttribute)$('drawer-content').querySelector(`[${focusAttribute.name}="${CSS.escape(focusAttribute.value)}"]`)?.focus({preventScroll:true});
    $('drawer-content').scrollTop=scroll;
  }
  function showDetail(id){if(!allNodes().has(id))return;state.selected=id;state.drawerView='node';state.drawerToken++;renderGraph();renderDetail();openDrawer();}
  async function showFile(path){
    const token=++state.drawerToken;state.drawerView='file';setText('detail-kicker','项目文件');
    const heading=`<button type="button" class="back-detail" data-back-detail>← 返回节点详情</button><h2 id="detail-title">${esc(path.split('/').pop()||'文件预览')}</h2><p class="detail-path">${esc(path)}</p>`;
    $('drawer-content').innerHTML=heading+'<p class="file-notice">正在读取文件…</p>';$('drawer-content').scrollTop=0;
    try{const data=await fetchJson(`/api/file?path=${encodeURIComponent(path)}`);if(token!==state.drawerToken||!$('detail-drawer').open)return;$('drawer-content').innerHTML=heading+(data.modifiedAt?`<p class="detail-meta">修改于 ${esc(timeLabel(data.modifiedAt,true))}</p>`:'')+(data.binary?'<p class="file-notice">这是二进制文件，暂不提供文本预览。</p>':`${data.truncated?'<p class="file-notice">文件较长，以下显示部分内容。</p>':''}<pre class="file-preview">${esc(data.text||'')}</pre>`);}catch(error){if(token!==state.drawerToken||!$('detail-drawer').open)return;$('drawer-content').innerHTML=heading+`<p class="file-notice error">暂时无法读取文件。${esc(error.name==='AbortError'?'连接超时，请重试。':error.message)}</p><button type="button" class="detail-action" data-file="${esc(path)}">重新读取</button>`;}
  }
  async function poll(){
    if(state.polling)return;clearTimeout(state.timer);state.polling=true;
    try{
      const data=await fetchJson('/api/tree');if(!data||!Array.isArray(data.nodes)||!data.nodes.some(node=>String(node?.id)===String(data.rootId||'project')))throw new Error('项目数据暂不可用');
      const first=!state.snapshot,changed=first||data.revision!==state.revision,next=new Map(data.nodes.map(raw=>{const node=normalize(raw);return[node.id,node];}));
      const branchKey=JSON.stringify((data.activePath||[]).map(String).filter(id=>next.get(id)?.kind!=='action')),branchChanged=branchKey!==state.branchKey;
      state.snapshot=data;state.offline=false;state.branchKey=branchKey;
      if(changed){
        state.server=next;state.revision=data.revision;
        if(first){state.initialized=true;initializeGraph();}
        else if(state.follow&&branchChanged)locateCurrent();
        else renderGraph();
        state.server.forEach(node=>{if(node.kind==='history'&&state.historyLoaded.has(node.id)&&historyVersion(node)!==state.historyVersions.get(node.id)){state.historyLoaded.delete(node.id);if(historyInView(node.id))void loadHistory(node.id,{automatic:true});}});
        if(state.drawerView==='node'&&$('detail-drawer').open)renderDetail(true);
        if(state.query&&!$('search-results').hidden)renderSearch();
      }
      updateHeader();
    }catch(error){state.offline=true;updateHeader();if(!state.snapshot){$('canvas-empty').innerHTML='<p>暂时无法连接项目图谱</p><small>正在自动重连，也可以点击上方“重新连接”。</small>';}}
    finally{state.polling=false;state.timer=setTimeout(poll,Math.max(500,Math.min(30000,(Number(state.snapshot?.pollSeconds)||2)*1000)));}
  }
  function localPoint(event){const rect=$('flow-canvas').getBoundingClientRect();return{x:event.clientX-rect.left,y:event.clientY-rect.top};}
  function resetGesture(){
    const points=[...state.pointerPositions.values()];
    if(points.length>=2){const a=points[0],b=points[1],center={x:(a.x+b.x)/2,y:(a.y+b.y)/2};state.gesture={type:'pinch',distance:Math.max(1,Math.hypot(b.x-a.x,b.y-a.y)),zoom:state.zoom,world:{x:(center.x-state.pan.x)/state.zoom,y:(center.y-state.pan.y)/state.zoom}};}
    else if(points.length===1)state.gesture={type:'pan',point:points[0],pan:{...state.pan},moved:false};else state.gesture=null;
  }
  $('flow-canvas').addEventListener('wheel',event=>{if(event.target.closest('.minimap-wrap,.zoom-controls,.selection-detail'))return;event.preventDefault();const p=localPoint(event);zoomAt(state.zoom*Math.exp(-event.deltaY*.0015),p.x,p.y);},{passive:false});
  $('flow-canvas').addEventListener('pointerdown',event=>{
    if(event.target.closest('.minimap-wrap,.zoom-controls,.selection-detail')||event.button>0)return;
    if(event.target.closest('.flow-node')&&event.pointerType!=='touch')return;
    state.pointerPositions.set(event.pointerId,localPoint(event));if(event.pointerType!=='touch')$('flow-canvas').setPointerCapture(event.pointerId);resetGesture();
  });
  $('flow-canvas').addEventListener('pointermove',event=>{
    if(!state.pointerPositions.has(event.pointerId)||!state.gesture)return;state.pointerPositions.set(event.pointerId,localPoint(event));const points=[...state.pointerPositions.values()],gesture=state.gesture;
    if(gesture.type==='pinch'&&points.length>=2){const a=points[0],b=points[1],center={x:(a.x+b.x)/2,y:(a.y+b.y)/2};state.zoom=Math.max(.18,Math.min(2.2,gesture.zoom*Math.hypot(b.x-a.x,b.y-a.y)/gesture.distance));state.pan={x:center.x-gesture.world.x*state.zoom,y:center.y-gesture.world.y*state.zoom};state.suppressClickUntil=Date.now()+400;$('flow-canvas').classList.add('is-dragging');applyCamera();}
    else if(gesture.type==='pan'&&points.length===1){const dx=points[0].x-gesture.point.x,dy=points[0].y-gesture.point.y;if(Math.hypot(dx,dy)>3||gesture.moved){gesture.moved=true;state.pan={x:gesture.pan.x+dx,y:gesture.pan.y+dy};state.suppressClickUntil=Date.now()+400;$('flow-canvas').classList.add('is-dragging');applyCamera();}}
  });
  function endPointer(event){if(!state.pointerPositions.has(event.pointerId))return;state.pointerPositions.delete(event.pointerId);if($('flow-canvas').hasPointerCapture(event.pointerId))$('flow-canvas').releasePointerCapture(event.pointerId);resetGesture();if(!state.pointerPositions.size)$('flow-canvas').classList.remove('is-dragging');}
  $('flow-canvas').addEventListener('pointerup',endPointer);$('flow-canvas').addEventListener('pointercancel',endPointer);$('flow-canvas').addEventListener('lostpointercapture',endPointer);
  $('node-layer').addEventListener('click',event=>{
    if(Date.now()<state.suppressClickUntil)return;const detail=event.target.closest('[data-detail]');if(detail){clearTimeout(state.clickTimer);showDetail(detail.dataset.detail);return;}
    const toggle=event.target.closest('[data-toggle]');if(toggle){clearTimeout(state.clickTimer);void activateNode(toggle.dataset.toggle);return;}
    const button=event.target.closest('[data-activate]');if(!button)return;clearTimeout(state.clickTimer);if(event.detail===0)return void activateNode(button.dataset.activate);
    if(event.detail>1)return;state.clickTimer=setTimeout(()=>void activateNode(button.dataset.activate),240);
  });
  $('node-layer').addEventListener('dblclick',event=>{const button=event.target.closest('[data-activate]');if(!button||Date.now()<state.suppressClickUntil)return;event.preventDefault();clearTimeout(state.clickTimer);focusBranch(button.dataset.activate);});
  $('node-layer').addEventListener('keydown',event=>{if(event.key==='Enter'&&event.shiftKey){const button=event.target.closest('[data-activate]');if(button){event.preventDefault();focusBranch(button.dataset.activate);}}});
  function miniPoint(event){const point=$('minimap').createSVGPoint();point.x=event.clientX;point.y=event.clientY;return point.matrixTransform($('minimap').getScreenCTM().inverse());}
  function panMini(event){const point=miniPoint(event),offset=state.miniGesture?.offset||{x:0,y:0};state.pan={x:$('flow-canvas').clientWidth/2-(point.x-offset.x)*state.zoom,y:$('flow-canvas').clientHeight/2-(point.y-offset.y)*state.zoom};applyCamera();}
  $('minimap').addEventListener('pointerdown',event=>{event.preventDefault();event.stopPropagation();const point=miniPoint(event),center={x:($('flow-canvas').clientWidth/2-state.pan.x)/state.zoom,y:($('flow-canvas').clientHeight/2-state.pan.y)/state.zoom};state.miniGesture={pointerId:event.pointerId,offset:event.target.id==='minimap-window'?{x:point.x-center.x,y:point.y-center.y}:{x:0,y:0}};$('minimap').setPointerCapture(event.pointerId);if(event.target.id!=='minimap-window')panMini(event);});
  $('minimap').addEventListener('pointermove',event=>{if(state.miniGesture?.pointerId===event.pointerId){event.stopPropagation();panMini(event);}});
  function endMini(event){if(state.miniGesture?.pointerId!==event.pointerId)return;state.miniGesture=null;if($('minimap').hasPointerCapture(event.pointerId))$('minimap').releasePointerCapture(event.pointerId);}
  $('minimap').addEventListener('pointerup',endMini);$('minimap').addEventListener('pointercancel',endMini);$('minimap').addEventListener('lostpointercapture',endMini);
  $('minimap').addEventListener('keydown',event=>{const moves={ArrowLeft:[55,0],ArrowRight:[-55,0],ArrowUp:[0,55],ArrowDown:[0,-55]};if(moves[event.key]){event.preventDefault();state.pan.x+=moves[event.key][0];state.pan.y+=moves[event.key][1];applyCamera();}});
  $('zoom-in').addEventListener('click',()=>zoomAt(state.zoom*1.25));$('zoom-out').addEventListener('click',()=>zoomAt(state.zoom/1.25));$('zoom-reset').addEventListener('click',()=>zoomAt(1));$('fit-view').addEventListener('click',fitView);
  $('activity-summary').addEventListener('click',locateCurrent);
  $('follow-current').addEventListener('change',()=>{state.follow=$('follow-current').checked;if(state.follow)locateCurrent();});
  $('breadcrumbs').addEventListener('click',event=>{const button=event.target.closest('[data-focus]');if(button)focusBranch(button.dataset.focus);});
  $('selected-detail').addEventListener('click',()=>showDetail(state.selected));
  $('node-search').addEventListener('input',()=>{state.query=$('node-search').value;renderSearch();});$('node-search').addEventListener('focus',()=>{if(state.query)renderSearch();});
  $('node-search').addEventListener('keydown',event=>{if(event.key==='Escape'){$('search-results').hidden=true;$('node-search').setAttribute('aria-expanded','false');}if(event.key==='ArrowDown'){event.preventDefault();$('search-results').querySelector('button')?.focus();}if(event.key==='Enter'){const first=$('search-results').querySelector('[data-search-node]');if(first)locateSearch(first.dataset.searchNode);}});
  $('search-results').addEventListener('click',event=>{const button=event.target.closest('[data-search-node]');if(button)locateSearch(button.dataset.searchNode);});
  $('search-results').addEventListener('keydown',event=>{const buttons=[...$('search-results').querySelectorAll('button')],at=buttons.indexOf(document.activeElement);if(event.key==='ArrowDown'){event.preventDefault();buttons[Math.min(at+1,buttons.length-1)]?.focus();}if(event.key==='ArrowUp'){event.preventDefault();if(at<=0)$('node-search').focus();else buttons[at-1]?.focus();}if(event.key==='Escape'){$('search-results').hidden=true;$('node-search').focus();}});
  document.addEventListener('pointerdown',event=>{if(!event.target.closest('.search-wrap')){$('search-results').hidden=true;$('node-search').setAttribute('aria-expanded','false');}});
  $('retry-button').addEventListener('click',()=>void poll());$('close-detail').addEventListener('click',()=>{$('detail-drawer').close();});
  $('detail-drawer').addEventListener('close',()=>{state.drawerToken++;[...$('node-layer').querySelectorAll('[data-activate]')].find(button=>button.dataset.activate===state.selected)?.focus({preventScroll:true});});
  $('detail-drawer').addEventListener('click',event=>{if(event.target===$('detail-drawer')){const b=$('detail-drawer').getBoundingClientRect();if(event.clientX<b.left)$('detail-drawer').close();}});
  $('drawer-content').addEventListener('click',event=>{
    const record=event.target.closest('[data-detail]');if(record)return void showDetail(record.dataset.detail);
    const history=event.target.closest('[data-history-load]');if(history){void loadHistory(history.dataset.historyLoad);renderDetail(true);return;}
    const file=event.target.closest('[data-file]');if(file)return void showFile(file.dataset.file);
    if(event.target.closest('[data-back-detail]')){state.drawerToken++;state.drawerView='node';renderDetail();return;}
    const focus=event.target.closest('[data-focus-detail]');if(focus){$('detail-drawer').close();focusBranch(focus.dataset.focusDetail);return;}
    const locate=event.target.closest('[data-locate-detail]');if(locate){$('detail-drawer').close();if(state.positions.has(locate.dataset.locateDetail))centerNode(locate.dataset.locateDetail);else locateSearch(locate.dataset.locateDetail);}
  });
  $('flow-canvas').addEventListener('keydown',event=>{
    if(event.target!==$('flow-canvas'))return;if(event.key==='+'||event.key==='='){event.preventDefault();zoomAt(state.zoom*1.25);}if(event.key==='-'){event.preventDefault();zoomAt(state.zoom/1.25);}if(event.key==='0'){event.preventDefault();fitView();}
    const moves={ArrowLeft:[60,0],ArrowRight:[-60,0],ArrowUp:[0,60],ArrowDown:[0,-60]};if(moves[event.key]){event.preventDefault();state.pan.x+=moves[event.key][0];state.pan.y+=moves[event.key][1];applyCamera();}
  });
  new ResizeObserver(()=>{const next={width:$('flow-canvas').clientWidth,height:$('flow-canvas').clientHeight};if(state.initialized&&state.size.width){state.pan.x+=(next.width-state.size.width)/2;state.pan.y+=(next.height-state.size.height)/2;applyCamera();}state.size=next;}).observe($('flow-canvas'));
  window.addEventListener('online',()=>void poll());document.addEventListener('visibilitychange',()=>{if(!document.hidden)void poll();});
  void poll();
})();
