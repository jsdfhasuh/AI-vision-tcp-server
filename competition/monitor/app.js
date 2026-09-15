/* Live data only. All operator/barcode/wire text is rendered as text, never HTML. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const publicView = location.pathname.startsWith('/board');
  const model = {project:'screw', csrf:'', loggedIn:false, epoch:0, timer:null, controller:null,
    snapshot:null, online:false, standard:null, formRevision:null, dirty:false, saving:false, importContent:null, reading:false, fileReadId:0};
  const matches = {MATCH:'匹配', MISMATCH:'不匹配', UNCONFIGURED:'未配置标准', UNREAD:'未读取条码'};
  let toastTimer;
  function node(tag, text, cls) { const e=document.createElement(tag); if(text!=null)e.textContent=String(text);if(cls)e.className=cls;return e; }
  function badge(text, tone='neutral') {return node('span',text,'badge '+tone);}
  function verdict(value) {return badge(value??'—',value==='OK'?'ok':value==='NG'?'bad':'neutral');}
  function barcodeBadge(value) {return badge(matches[value]??'—',value==='MATCH'?'ok':['MISMATCH','UNREAD'].includes(value)?'bad':'neutral');}
  function stamp(value) {if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?'—':d.toLocaleString('zh-CN',{hour12:false});}
  function toast(text,error=false) {const e=$('toast');e.textContent=text;e.hidden=false;e.className='toast'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.hidden=true,5500);}
  function requestId() {const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Array.from(bytes,n=>n.toString(16).padStart(2,'0')).join('');}
  async function api(path,{method='GET',body,signal,timeout=8000,...rest}={}) {
    const ctl=new AbortController();const timer=setTimeout(()=>ctl.abort(),timeout);
    const abort=()=>ctl.abort();if(signal){signal.addEventListener('abort',abort,{once:true});if(signal.aborted)ctl.abort();}
    try {
      const headers=method==='POST'?{'Content-Type':'application/json','X-CSRF-Token':model.csrf,'X-Request-ID':requestId()}:{};
      const r=await fetch(path,{method,body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',cache:'no-store',headers,signal:ctl.signal,...rest});
      const data=await r.json();
      if(!r.ok){const err=new Error(data.error?.message||'请求失败');err.status=r.status;throw err;}
      return data;
    } catch(err){if(err.name==='AbortError')throw new Error(method==='POST'?'提交响应超时，结果未知；请重新载入核对，不要反复提交。':'服务器响应超时。');throw err;}
    finally {clearTimeout(timer);signal?.removeEventListener('abort',abort);}
  }
  function stopPoll(){model.epoch++;clearTimeout(model.timer);model.controller?.abort();}
  function showLogin(message='') {
    stopPoll();model.loggedIn=false;model.csrf='';model.snapshot=null;model.online=false;model.standard=null;model.formRevision=null;model.dirty=false;
    resetImport();model.saving=false;
    $('standard-barcode').value='';$('stations').replaceChildren();$('records-body').replaceChildren();$('logs-body').replaceChildren();
    $('app-screen').hidden=true;$('login-screen').hidden=false;$('login-error').textContent=message;
    $('login-form').elements.password.value='';
  }
  function start(identity) {
    model.loggedIn=!publicView;model.csrf=identity?.csrf||'';
    $('actor').textContent=publicView?'只读查看':identity.username;
    $('login-screen').hidden=true;$('app-screen').hidden=false;
    $('login-form').elements.password.value='';
    for(const id of ['logout','board-link','export-csv','export-jsonl','backup','logs-panel'])$(id).hidden=publicView;
    $('readonly-label').hidden=!publicView;selectProject(model.project);
  }
  function updateControls(){
    const locked=!model.online||!model.loggedIn||model.saving||!!model.snapshot?.fatal;
    $('standard-file').disabled=locked||model.formRevision===null;
    $('save-standard').disabled=locked||model.reading||model.importContent===null||model.formRevision===null;
    $('reload-standard').disabled=!model.standard||model.saving;
    $('backup').disabled=locked;
  }
  function field(label,value,cls){const e=node('div',null,'field');e.append(node('span',label,'field-label'));const v=node('div',null,'field-value '+(cls||''));v.append(value instanceof Node?value:node('span',value??'—'));e.append(v);return e;}
  function stationCard(s){
    const fresh=model.online, latest=s.latest;
    const card=node('article',null,'station'+(!fresh||!s.last_is_current?' is-stale':''));card.dataset.station=String(s.station);
    const head=node('div',null,'station-title');head.append(node('h3',s.station+'号工位'),badge(!fresh?'状态未知':s.connected?'已连接':'未连接',fresh&&s.connected?'ok':'neutral'));card.append(head);
    card.append(field('当前选手工号',s.worker_id||'—','worker'));
    if(!publicView)card.append(field('客户端地址',s.address||'—','code'));
    card.append(field('最近接收时间',stamp(latest?.received_utc)));
    const result=node('div',null,'result-block');
    if(model.project==='screw'){
      const count=node('div',null,'count-row');count.append(node('span','最近螺钉数量','count-label'));
      const value=node('strong',latest?.screw_count??'—','count');if(latest?.screw_count!=null)value.append(node('small','颗'));count.append(value);result.append(count);
    }else{
      result.append(field('上传条码',latest?.barcode===''?'（未读取）':latest?.barcode??'—','code'));
      result.append(field('条码校验结果',barcodeBadge(latest?.barcode_status)),field('LOGO 检查',verdict(latest?.logo)),field('火焰标识检查',verdict(latest?.flame)));
    }
    card.append(result,node('p',!fresh?'页面已暂停更新，下列数据为上次记录。':latest&&!s.last_is_current?'保留上次检测记录，本连接尚无新上报。':latest?'已保存工位上报数据。':'等待工位上传数据。','station-note'));
    return card;
  }
  function renderTables(){
    const snapshot=model.snapshot;if(!snapshot)return;
    const station=Number($('station-filter').value), isPackage=model.project==='packaging';
    const issue=r=>['MISMATCH','UNREAD'].includes(r.barcode_status)||r.logo==='NG'||r.flame==='NG';
    const rows=snapshot.records.filter(r=>(!station||r.station===station)&&(!isPackage||!$('issues-only').checked||issue(r)));
    const frag=document.createDocumentFragment();
    for(const r of rows){
      const tr=node('tr');
      const values=[stamp(r.received_utc),r.station+'号工位',r.worker_id];
      for(const v of values)tr.append(node('td',v,'nowrap'));
      if(isPackage){tr.append(node('td',r.barcode===''?'（未读取）':r.barcode,'barcode code'));for(const b of [barcodeBadge(r.barcode_status),verdict(r.logo),verdict(r.flame)]){const td=node('td');td.append(b);tr.append(td);}}
      else tr.append(node('td',r.screw_count+' 颗','nowrap'));
      frag.append(tr);
    }
    $('records-body').replaceChildren(frag);$('records-empty').hidden=rows.length>0;
    $('records-empty').textContent=snapshot.records.length?'当前筛选下没有记录':'等待工位上传数据';
    const logs=document.createDocumentFragment();
    for(const row of (snapshot.logs||[]).filter(r=>!station||r.station===station)){
      const tr=node('tr');tr.append(node('td',stamp(row.at_utc),'nowrap'),node('td',row.direction,'direction '+(row.direction==='RX'?'rx':'')),node('td',row.station+'号工位','nowrap'),node('td',row.raw,'raw'));logs.append(tr);
    }
    $('logs-body').replaceChildren(logs);$('logs-empty').hidden=$('logs-body').children.length>0;
    $('export-csv').href='/api/admin/export?project='+model.project+'&station='+station+'&format=csv';
    $('export-jsonl').href='/api/admin/export?project='+model.project+'&station='+station+'&format=jsonl';
  }
  function applyStandard(){
    if(!model.standard)return;
    resetImport();
    $('standard-barcode').value=model.standard.barcode;model.formRevision=model.standard.revision;model.dirty=false;
    $('standard-note').textContent='服务器标准版本 '+model.formRevision+' · 两个包装箱工位共用；JSON 导入后保存在服务器，重启仍有效。';
  }
  function render(snapshot){
    model.snapshot=snapshot;model.online=true;
    $('web-state').replaceWith(Object.assign(badge('实时连接','ok'),{id:'web-state'}));
    $('listen-state').textContent=publicView?'只读数据接口':'TCP · '+snapshot.listening;
    $('updated-at').textContent='更新于 '+stamp(snapshot.generated_utc);
    $('connection-alert').hidden=true;$('fatal-alert').hidden=!snapshot.fatal;$('fatal-alert').textContent=snapshot.fatal||'';
    if(snapshot.standard&&(!model.standard||snapshot.standard.revision>=model.standard.revision)){model.standard=snapshot.standard;if(!model.dirty&&!model.saving)applyStandard();else if(model.formRevision!==snapshot.standard.revision)$('standard-note').textContent='服务器标准已更新。未覆盖待导入文件；请重新载入并重新选择文件。';}
    $('stations').replaceChildren(...snapshot.stations.map(stationCard));renderTables();updateControls();
  }
  async function refresh(epoch){
    const project=model.project;const ctl=new AbortController();model.controller=ctl;
    try {const snapshot=await api((publicView?'/api/display':'/api/admin/state')+'?project='+project,{signal:ctl.signal});if(epoch===model.epoch)render(snapshot);}
    catch(err){if(epoch!==model.epoch)return;if(err.status===401&&!publicView){showLogin('登录已过期，请重新登录。');return;}model.online=false;$('connection-alert').hidden=false;$('connection-alert').textContent=err.message+' 保留的数据不是实时状态。';$('web-state').textContent='连接中断';$('web-state').className='badge neutral';if(model.snapshot)$('stations').replaceChildren(...model.snapshot.stations.map(stationCard));updateControls();}
    finally {if(epoch===model.epoch&&(publicView||model.loggedIn))model.timer=setTimeout(()=>refresh(epoch),1500);}
  }
  function selectProject(project){
    if(!['screw','packaging'].includes(project))return;
    stopPoll();model.project=project;model.snapshot=null;model.online=false;
    const pkg=project==='packaging';document.querySelectorAll('[data-project]').forEach(b=>{b.classList.toggle('active',b.dataset.project===project);b.setAttribute('aria-current',b.dataset.project===project?'page':'false');});
    $('page-number').textContent=pkg?'PROJECT 02':'PROJECT 01';$('page-title').textContent=pkg?'包装箱检查项目':'电机螺钉项目';
    $('page-description').textContent=pkg?'完整条码由服务器比对；LOGO 与火焰标识记录选手上报的 OK / NG。':'接收检测到的螺钉数量，工号仅标明当前操作者。';
    $('barcode-config').hidden=!pkg||publicView;$('issues-control').hidden=!pkg;
    $('records-title').textContent='检测记录（'+(pkg?'包装箱检查':'电机螺钉')+'）';
    const tr=node('tr');for(const h of ['接收时间','工位','选手工号',...(pkg?['上传条码','条码校验','LOGO','火焰标识']:['螺钉数量'])])tr.append(node('th',h));$('records-head').replaceChildren(tr);
    $('stations').replaceChildren();$('records-body').replaceChildren();$('logs-body').replaceChildren();$('records-empty').hidden=false;$('records-empty').textContent='正在载入项目数据…';updateControls();refresh(model.epoch);
  }
  document.querySelectorAll('[data-project]').forEach(b=>b.addEventListener('click',()=>selectProject(b.dataset.project)));
  $('station-filter').addEventListener('change',renderTables);$('issues-only').addEventListener('change',renderTables);
  function resetImport(){
    model.fileReadId++;model.importContent=null;model.reading=false;
    $('standard-file').value='';
    $('file-status').textContent='UTF-8 JSON，最多4 KiB；选择文件后预览，点击导入才生效。';
  }
  $('standard-file').addEventListener('change',async()=>{
    const file=$('standard-file').files[0], readId=++model.fileReadId;
    model.importContent=null;model.reading=false;model.dirty=!!file;
    $('standard-barcode').value=model.standard?.barcode??'';
    if(!file){applyStandard();updateControls();return;}
    model.reading=true;updateControls();
    $('file-status').textContent='正在读取 '+file.name+' …';
    try {
      if(!/\.json$/i.test(file.name)||file.size===0||file.size>4096)throw new Error('请选择非空的 .json 文件，最多4 KiB。');
      const raw=await file.arrayBuffer();
      if(readId!==model.fileReadId||!model.loggedIn)return;
      const content=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(raw);
      const data=JSON.parse(content.replace(/^\uFEFF/,''));
      if(!data||Array.isArray(data)||Object.keys(data).length!==1||!Object.hasOwn(data,'standard_barcode'))throw new Error('JSON 只能包含 standard_barcode 一个字段。');
      if(typeof data.standard_barcode!=='string'||[...data.standard_barcode].length>512||/[\u0000-\u001f\u007f]/.test(data.standard_barcode))throw new Error('条码必须是字符串，最多512个字符且不能含控制字符，不能填数字。');
      // Preview only. The server strictly parses the original text again, including duplicate-key checks.
      model.importContent=content;$('standard-barcode').value=data.standard_barcode;
      $('file-status').textContent='待导入：'+file.name+' · 尚未生效'+(data.standard_barcode===''?' · 空字符串将清除标准':'');
    } catch(err){
      if(readId!==model.fileReadId||!model.loggedIn)return;
      model.importContent=null;model.dirty=false;
      $('file-status').textContent='文件未导入，原标准未改变。'+err.message;
      toast($('file-status').textContent,true);
      $('standard-file').value='';
    } finally {
      if(readId===model.fileReadId){model.reading=false;updateControls();}
    }
  });
  $('reload-standard').addEventListener('click',()=>{if(!model.dirty||confirm('放弃待导入文件，重新载入服务器标准？')){applyStandard();updateControls();}});
  $('barcode-form').addEventListener('submit',async e=>{
    e.preventDefault();if($('save-standard').disabled)return;
    if($('standard-barcode').value===''&&!confirm('文件中的条码为空。确认清除标准并暂停条码匹配校验？'))return;
    const identity=model.csrf, barcode=$('standard-barcode').value;
    model.saving=true;updateControls();
    try {
      const result=await api('/api/admin/standard-barcode/import',{method:'POST',body:{content:model.importContent,revision:model.formRevision}});
      if(!model.loggedIn||identity!==model.csrf)return;
      if(!model.standard||model.standard.revision<=result.revision)model.standard={barcode,revision:result.revision};
      applyStandard();toast('JSON 已导入并生效，仅用于服务器比对。');
    }
    catch(err){if(model.loggedIn&&identity===model.csrf){if(err.status===401)showLogin('登录已过期。');else toast(err.message,true);}}
    finally {if(identity===model.csrf){model.saving=false;updateControls();}}
  });
  $('login-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button');b.disabled=true;$('login-error').textContent='';try{start(await api('/api/auth/login',{method:'POST',body:Object.fromEntries(new FormData(e.currentTarget))}));}catch(err){$('login-error').textContent=err.message;}finally{b.disabled=false;}});
  $('logout').addEventListener('click',async()=>{try{await api('/api/auth/logout',{method:'POST',body:{}});showLogin();}catch(err){toast('退出未确认：'+err.message,true);}});
  $('backup').addEventListener('click',async()=>{model.saving=true;updateControls();try{const d=await api('/api/admin/backup',{method:'POST',body:{},timeout:40000});if(model.loggedIn){const a=node('a','下载本次备份');a.href=d.download_url;a.download=d.file;$('backup-result').replaceChildren(a);toast('工位数据库备份完成。');}}catch(err){toast(err.message,true);}finally{model.saving=false;updateControls();}});
  if(publicView)start(null);else api('/api/auth/me').then(start).catch(()=>showLogin());
})();
