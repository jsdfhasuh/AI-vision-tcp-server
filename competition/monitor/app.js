/* Live data only. Untrusted operator, barcode and catalog values are text, never HTML. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const publicView = location.pathname.startsWith('/board');
  const model = {project:'screw', csrf:'', loggedIn:false, epoch:0, timer:null, controller:null,
    snapshot:null, online:false, catalog:null, formRevision:null, saving:false,
    importContent:null, preview:null, reading:false, fileReadId:0, drafts:{}};
  const matches = {MATCH:'匹配', MISMATCH:'不匹配', UNCONFIGURED:'未配置标准', UNSELECTED:'未选择标准', UNREAD:'未读取条码'};
  let toastTimer;
  function node(tag,text,cls) {const e=document.createElement(tag);if(text!=null)e.textContent=String(text);if(cls)e.className=cls;return e;}
  function badge(text,tone='neutral') {return node('span',text,'badge '+tone);}
  function verdict(value) {return badge(value??'—',value==='OK'?'ok':value==='NG'?'bad':'neutral');}
  function barcodeBadge(value) {return badge(matches[value]??'—',value==='MATCH'?'ok':['MISMATCH','UNREAD'].includes(value)?'bad':'neutral');}
  function stamp(value) {if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?'—':d.toLocaleString('zh-CN',{hour12:false});}
  function toast(text,error=false) {const e=$('toast');e.textContent=text;e.hidden=false;e.className='toast'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.hidden=true,6500);}
  function requestId() {const b=new Uint8Array(16);crypto.getRandomValues(b);return Array.from(b,n=>n.toString(16).padStart(2,'0')).join('');}
  async function api(path,{method='GET',body,signal,timeout=8000}={}) {
    const ctl=new AbortController(), timer=setTimeout(()=>ctl.abort(),timeout), abort=()=>ctl.abort();
    if(signal){signal.addEventListener('abort',abort,{once:true});if(signal.aborted)ctl.abort();}
    try {
      const headers=method==='POST'?{'Content-Type':'application/json','X-CSRF-Token':model.csrf,'X-Request-ID':requestId()}:{};
      const r=await fetch(path,{method,body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',cache:'no-store',headers,signal:ctl.signal});
      const data=await r.json();
      if(!r.ok){const e=new Error(data.error?.message||'请求失败');e.status=r.status;throw e;}
      return data;
    }catch(e){if(e.name==='AbortError')throw new Error(method==='POST'?'提交响应超时，结果未知；请重新载入核对，不要反复提交。':'服务器响应超时。');throw e;}
    finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}
  }
  function stopPoll(){model.epoch++;clearTimeout(model.timer);model.controller?.abort();}
  function resetImport(){
    model.fileReadId++;model.importContent=null;model.preview=null;model.reading=false;
    model.formRevision=model.catalog?.revision??null;$('standard-file').value='';
    $('file-status').textContent='UTF-8 JSON，最多1000条且不超过256 KiB；选择后预览，导入才生效。';
  }
  function showLogin(message=''){
    stopPoll();model.loggedIn=false;model.csrf='';model.snapshot=null;model.online=false;model.catalog=null;model.drafts={};model.saving=false;
    resetImport();for(const id of ['stations','records-body','logs-body','catalog-body','backup-result'])$(id).replaceChildren();
    $('app-screen').hidden=true;$('login-screen').hidden=false;$('login-error').textContent=message;
    $('login-form').elements.password.value='';
  }
  function start(identity){
    model.loggedIn=!publicView;model.csrf=identity?.csrf||'';$('actor').textContent=publicView?'只读查看':identity.username;
    $('login-screen').hidden=true;$('app-screen').hidden=false;$('login-form').elements.password.value='';
    for(const id of ['logout','board-link','export-csv','export-jsonl','backup','logs-panel'])$(id).hidden=publicView;
    $('readonly-label').hidden=!publicView;selectProject(model.project);
  }
  function locked(){return !model.online||!model.loggedIn||model.saving||!!model.snapshot?.fatal;}
  function updateControls(){
    $('standard-file').disabled=locked()||!model.catalog;
    $('save-standard').disabled=locked()||model.reading||model.importContent===null||model.formRevision===null;
    $('reload-standard').disabled=!model.catalog||model.saving;
    $('backup').disabled=locked();
    for(const card of $('stations').children){
      const select=card.querySelector('.box-select');if(!select)continue;
      const d=model.drafts[card.dataset.station];select.disabled=locked()||!model.catalog;
      card.querySelector('.apply-box').disabled=locked()||!d?.dirty;
      card.querySelector('.reset-box').disabled=locked()||!d?.dirty;
    }
  }
  function field(label,value,cls){const e=node('div',null,'field');e.append(node('span',label,'field-label'));const v=node('div',null,'field-value '+(cls||''));v.append(value instanceof Node?value:node('span',value??'—'));e.append(v);return e;}
  function boxName(id){const b=model.catalog?.boxes.find(b=>b.id===id);return b?b.id+' · '+b.name:'未选择标准';}
  function renderCatalog(){
    if(publicView)return;
    const boxes=model.preview?.boxes??model.catalog?.boxes??[];
    const q=$('catalog-search').value.toLocaleLowerCase();
    const rows=boxes.filter(b=>[b.id,b.name,b.standard_barcode].some(x=>x.toLocaleLowerCase().includes(q)));
    $('catalog-title').textContent=(model.preview?'待导入预览（尚未生效）':'当前标准清单')+' · '+rows.length+' / '+boxes.length+' 条';
    const frag=document.createDocumentFragment();
    for(const b of rows){const tr=node('tr');tr.append(node('td',b.id,'nowrap'),node('td',b.name),node('td',b.standard_barcode,'code barcode'));frag.append(tr);}
    if(!rows.length){const tr=node('tr'),td=node('td',boxes.length?'没有匹配的标准':'空清单');td.colSpan=3;tr.append(td);frag.append(tr);}
    $('catalog-body').replaceChildren(frag);
    const conflict=(model.importContent!==null||model.reading)&&model.catalog?.revision!==model.formRevision;
    $('standard-note').textContent=conflict?'配置版本已变化，待导入文件没有被覆盖。请取消选择 / 重新载入后确认。':
      '当前配置版本 '+(model.catalog?.revision??'—')+'；批量导入为整批替换。换箱前先等待上一件结果入库，再切换对应工位标准。';
  }
  function syncSelector(card){
    if(!model.catalog)return;
    const n=card.dataset.station, selected=model.catalog.selections[n];
    let d=model.drafts[n];
    if(!d||!d.dirty){d={value:selected,revision:model.catalog.revision,dirty:false};model.drafts[n]=d;}
    const select=card.querySelector('.box-select');
    // Do not rebuild native selects on every poll: keeps open menus and pending selections intact.
    if(card.dataset.catalogRevision!==String(model.catalog.revision)){
      const options=[new Option('— 未选择标准 —','')];
      for(const b of model.catalog.boxes)options.push(new Option(b.id+' · '+b.name,b.id));
      if(d.value&&!model.catalog.boxes.some(b=>b.id===d.value))options.push(new Option(d.value+'（已从清单移除）',d.value));
      select.replaceChildren(...options);card.dataset.catalogRevision=String(model.catalog.revision);
    }
    if(select.value!==(d.value??''))select.value=d.value??'';
    card.querySelector('.current-box').textContent='当前生效：'+boxName(selected);
    card.querySelector('.selection-note').textContent=d.dirty?(d.revision!==model.catalog.revision?
      '配置已变化；保留待选值，请重新载入后确认。':'尚未应用；点击应用标准后，仅影响本工位之后接收的记录。'):
      '只与本工位选定标准比较；组别号不决定校验标准。';
  }
  function makeStation(s){
    const card=node('article',null,'station');card.dataset.station=String(s.station);
    card.append(node('div',null,'station-live'));
    if(!publicView&&model.project==='packaging'){
      const panel=node('section',null,'selection-control');
      const current=node('p',null,'current-box'),label=node('label','当前标准包装箱'),select=node('select',null,'box-select');
      select.id='box-select-'+s.station;label.append(select);
      const apply=node('button','应用标准','primary apply-box'),reset=node('button','重新载入','secondary reset-box');apply.type=reset.type='button';
      const actions=node('div',null,'selection-actions');actions.append(apply,reset);panel.append(current,label,actions,node('p',null,'muted small-text selection-note'));card.append(panel);
      select.addEventListener('change',()=>{model.drafts[card.dataset.station]={value:select.value||null,revision:model.catalog.revision,dirty:true};syncSelector(card);updateControls();});
      reset.addEventListener('click',()=>{delete model.drafts[card.dataset.station];syncSelector(card);updateControls();});
      apply.addEventListener('click',()=>applySelection(s.station));
    }
    return card;
  }
  function renderStations(){
    if(!model.snapshot)return;
    for(const s of model.snapshot.stations){
      let card=$('stations').querySelector('[data-station="'+s.station+'"]');
      if(!card){card=makeStation(s);$('stations').append(card);}
      const fresh=model.online,latest=s.latest,live=card.querySelector('.station-live');
      card.classList.toggle('is-stale',!fresh||!s.last_is_current);
      const head=node('div',null,'station-title');head.append(node('h3',s.station+'号工位'),badge(!fresh?'状态未知':s.connected?'已连接':'未连接',fresh&&s.connected?'ok':'neutral'));
      const frag=document.createDocumentFragment();frag.append(head,field('当前组别号',s.group_id||'—','worker'));
      if(!publicView)frag.append(field('客户端地址',s.address||'—','code'));
      frag.append(field('最近接收时间',stamp(latest?.received_utc)));
      const result=node('div',null,'result-block');
      if(model.project==='screw'){
        const count=node('div',null,'count-row'),value=node('strong',latest?.screw_count??'—','count');
        if(latest?.screw_count!=null)value.append(node('small','颗'));
        count.append(node('span','最近螺钉数量','count-label'),value);result.append(count,field('检测结果（上报）',verdict(latest?.detection_result)));
      }else{
        result.append(field('上传条码',latest?.barcode===''?'（未读取）':latest?.barcode??'—','code'));
        result.append(field('条码校验结果',barcodeBadge(latest?.barcode_status)),field('LOGO 检查',verdict(latest?.logo)),field('火焰标识检查',verdict(latest?.flame)),field('总结果（上报）',verdict(latest?.total_result)));
        if(!publicView&&latest)result.append(field('本条使用标准',latest.standard_box_name?latest.standard_box_id+' · '+latest.standard_box_name:latest.barcode_status==='UNSELECTED'?'未选择':latest.barcode_status==='UNCONFIGURED'?'未配置':'旧版 / 无标准'));
      }
      frag.append(result,node('p',!fresh?'页面已暂停更新，保留上次记录。':latest&&!s.last_is_current?'保留上次检测记录，本连接尚无新上报。':latest?'已保存工位上报数据。':'等待工位上传数据。','station-note'));live.replaceChildren(frag);
      if(card.querySelector('.box-select'))syncSelector(card);
    }
  }
  function renderTables(){
    const snapshot=model.snapshot;if(!snapshot)return;
    const station=Number($('station-filter').value),pkg=model.project==='packaging';
    const issue=r=>pkg?['MISMATCH','UNREAD'].includes(r.barcode_status)||r.logo==='NG'||r.flame==='NG'||r.total_result==='NG':r.detection_result==='NG';
    const rows=snapshot.records.filter(r=>(!station||r.station===station)&&(!$('issues-only').checked||issue(r)));
    const frag=document.createDocumentFragment();
    for(const r of rows){const tr=node('tr');for(const v of [stamp(r.received_utc),r.station+'号工位',r.group_id??'—'])tr.append(node('td',v,'nowrap'));
      if(pkg){
        tr.append(node('td',r.barcode===''?'（未读取）':r.barcode,'barcode code'));
        if(!publicView)tr.append(node('td',r.standard_box_name?r.standard_box_id+' · '+r.standard_box_name:'—'));
        for(const b of [barcodeBadge(r.barcode_status),verdict(r.logo),verdict(r.flame),verdict(r.total_result)]){const td=node('td');td.append(b);tr.append(td);}
      }else {tr.append(node('td',r.screw_count+' 颗','nowrap'));const td=node('td');td.append(verdict(r.detection_result));tr.append(td);}
      frag.append(tr);
    }
    $('records-body').replaceChildren(frag);$('records-empty').hidden=rows.length>0;$('records-empty').textContent=snapshot.records.length?'当前筛选下没有记录':'等待工位上传数据';
    const logs=document.createDocumentFragment();
    for(const r of (snapshot.logs||[]).filter(r=>!station||r.station===station)){const tr=node('tr');tr.append(node('td',stamp(r.at_utc),'nowrap'),node('td',r.direction,'direction '+(r.direction==='RX'?'rx':'')),node('td',r.station+'号工位','nowrap'),node('td',r.raw,'raw'));logs.append(tr);}
    $('logs-body').replaceChildren(logs);$('logs-empty').hidden=$('logs-body').children.length>0;
    $('export-csv').href='/api/admin/export?project='+model.project+'&station='+station+'&format=csv';
    $('export-jsonl').href='/api/admin/export?project='+model.project+'&station='+station+'&format=jsonl';
  }
  function render(snapshot){
    model.snapshot=snapshot;model.online=true;
    $('web-state').replaceWith(Object.assign(badge('实时连接','ok'),{id:'web-state'}));
    $('listen-state').textContent=publicView?'只读数据接口':'TCP · '+snapshot.listening;$('updated-at').textContent='更新于 '+stamp(snapshot.generated_utc);
    $('connection-alert').hidden=true;$('fatal-alert').hidden=!snapshot.fatal;$('fatal-alert').textContent=snapshot.fatal||'';
    if(snapshot.catalog&&(!model.catalog||snapshot.catalog.revision>=model.catalog.revision)){
      model.catalog=snapshot.catalog;
      if(model.importContent===null&&!model.reading)model.formRevision=model.catalog.revision;
    }
    renderCatalog();renderStations();renderTables();updateControls();
  }
  async function refresh(epoch){
    const project=model.project,ctl=new AbortController();model.controller=ctl;
    try{const data=await api((publicView?'/api/display':'/api/admin/state')+'?project='+project,{signal:ctl.signal});if(epoch===model.epoch)render(data);}
    catch(e){if(epoch!==model.epoch)return;if(e.status===401&&!publicView){showLogin('登录已过期，请重新登录。');return;}model.online=false;$('connection-alert').hidden=false;$('connection-alert').textContent=e.message+' 保留的数据不是实时状态。';$('web-state').textContent='连接中断';$('web-state').className='badge neutral';renderStations();updateControls();}
    finally{if(epoch===model.epoch&&(publicView||model.loggedIn))model.timer=setTimeout(()=>refresh(epoch),1500);}
  }
  function restartPoll(){stopPoll();model.online=false;updateControls();refresh(model.epoch);}
  function selectProject(project){
    if(!['screw','packaging'].includes(project))return;
    stopPoll();model.project=project;model.snapshot=null;model.online=false;
    const pkg=project==='packaging';document.querySelectorAll('[data-project]').forEach(b=>{b.classList.toggle('active',b.dataset.project===project);b.setAttribute('aria-current',b.dataset.project===project?'page':'false');});
    $('page-number').textContent=pkg?'PROJECT 02':'PROJECT 01';$('page-title').textContent=pkg?'包装箱检查项目':'电机螺钉项目';
    $('page-description').textContent=pkg?'按工位核对完整条码；LOGO、火焰标识、总结果均原样记录参赛端上报值。':'按工位接收组别号、螺钉数量及检测结果；不按数量重新判定。';
    $('issues-label').textContent=pkg?'仅不匹配 / NG':'仅 NG';
    $('records-note').textContent=pkg?'最近100条；条码按接收时的工位标准校验，总结果原样记录上报值。':'最近100条；检测结果原样记录上报值，不按数量重算。';
    $('barcode-config').hidden=!pkg||publicView;$('issues-control').hidden=false;$('records-title').textContent='检测记录（'+(pkg?'包装箱检查':'电机螺钉')+'）';
    const tr=node('tr');for(const h of ['接收时间','工位','组别号',...(pkg?['上传条码',...(!publicView?['本条校验标准']:[]),'条码校验','LOGO','火焰标识','总结果（上报）']:['螺钉数量','检测结果（上报）'])])tr.append(node('th',h));$('records-head').replaceChildren(tr);
    for(const id of ['stations','records-body','logs-body'])$(id).replaceChildren();$('records-empty').hidden=false;$('records-empty').textContent='正在载入项目数据…';updateControls();refresh(model.epoch);
  }
  async function applySelection(station){
    if(locked())return;
    const d=model.drafts[String(station)];if(!d?.dirty)return;
    if(d.value===null&&!confirm('确认取消本工位标准？之后收到的条码将标记为未选择标准。'))return;
    const identity=model.csrf;model.saving=true;updateControls();
    try{await api('/api/admin/standard-barcode/select',{method:'POST',body:{station,box_id:d.value,revision:d.revision}});
      if(!model.loggedIn||identity!==model.csrf)return;
      delete model.drafts[String(station)];toast(station+'号工位标准已应用；历史记录不变。');restartPoll();
    }catch(e){if(identity===model.csrf){if(e.status===401)showLogin('登录已过期。');else toast(e.message,true);}}
    finally{if(identity===model.csrf){model.saving=false;updateControls();}}
  }
  function previewFile(data){
    if(!data||typeof data!=='object'||Array.isArray(data)||Object.keys(data).length!==1)throw new Error('顶层必须是仅含 boxes 或 standard_barcode 的对象。');
    const checkText=(x,max,empty=false)=>typeof x==='string'&&[...x].length<=max&&(empty||x.length>0)&&!/[\u0000-\u001f\u007f]/.test(x);
    if(Object.hasOwn(data,'standard_barcode')){
      if(!checkText(data.standard_barcode,512,true))throw new Error('标准条码必须是字符串，最多512字符。');
      return {legacy:true,boxes:data.standard_barcode?[{id:'LEGACY',name:'原标准条码',standard_barcode:data.standard_barcode}]:[]};
    }
    if(!Object.hasOwn(data,'boxes')||!Array.isArray(data.boxes)||data.boxes.length>1000)throw new Error('boxes 必须是数组，最多1000条。');
    const ids=new Set();
    for(const b of data.boxes){
      if(!b||typeof b!=='object'||Array.isArray(b)||Object.keys(b).sort().join(',')!=='id,name,standard_barcode')throw new Error('每条必须只含 id、name、standard_barcode。');
      if(typeof b.id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(b.id)||ids.has(b.id))throw new Error('编号无效或重复。编号只用字母、数字、下划线、短横线。');
      ids.add(b.id);
      if(!checkText(b.name,100)||!b.name.trim()||!checkText(b.standard_barcode,512))throw new Error('名称或条码无效；条码必须是非空字符串，最多512字符。');
    }
    return {legacy:false,boxes:data.boxes};
  }
  $('standard-file').addEventListener('change',async()=>{
    const file=$('standard-file').files[0],readId=++model.fileReadId;
    model.importContent=null;model.preview=null;model.reading=false;model.formRevision=model.catalog?.revision??null;
    if(!file){resetImport();renderCatalog();updateControls();return;}
    model.reading=true;updateControls();$('file-status').textContent='正在读取 '+file.name+' …';
    try{
      if(!/\.json$/i.test(file.name)||!file.size||file.size>262144)throw new Error('请选择非空 .json 文件，最多256 KiB。');
      const bytes=await file.arrayBuffer();if(readId!==model.fileReadId||!model.loggedIn)return;
      const content=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);
      model.preview=previewFile(JSON.parse(content.replace(/^\uFEFF/,'')));model.importContent=content;
      // Preview only: server re-parses original text strictly, including duplicate keys.
      $('file-status').textContent='待导入：'+file.name+' · '+model.preview.boxes.length+'条 · 尚未生效'+(model.preview.legacy?' · 单条兼容格式将替换清单并应用到两个工位':' · 整批替换，首次导入后须分别选择工位标准');
    }catch(e){if(readId!==model.fileReadId||!model.loggedIn)return;model.importContent=null;model.preview=null;$('standard-file').value='';$('file-status').textContent='未导入，原配置未改变。'+e.message;toast($('file-status').textContent,true);}
    finally{if(readId===model.fileReadId){model.reading=false;renderCatalog();updateControls();}}
  });
  $('reload-standard').addEventListener('click',()=>{
    if((model.preview||model.reading)&&!confirm('放弃待导入文件，重新载入服务器清单？'))return;
    resetImport();model.drafts={};renderCatalog();renderStations();restartPoll();
  });
  $('barcode-form').addEventListener('submit',async e=>{
    e.preventDefault();if($('save-standard').disabled)return;
    const warning=!model.preview.boxes.length?'确认清空标准清单并取消两个工位的标准？':model.preview.legacy?
      '确认用单条标准替换整个清单，并应用到两个工位？':'确认整批替换标准清单？删除或更改条码的当前选项会被取消，须重新选择。';
    if(!confirm(warning))return;
    const identity=model.csrf;model.saving=true;updateControls();
    try{await api('/api/admin/standard-barcode/import',{method:'POST',body:{content:model.importContent,revision:model.formRevision}});
      if(!model.loggedIn||identity!==model.csrf)return;
      resetImport();model.drafts={};toast('标准清单已导入；请核对两个工位当前选定标准。');restartPoll();
    }catch(e){if(identity===model.csrf){if(e.status===401)showLogin('登录已过期。');else toast(e.message,true);}}
    finally{if(identity===model.csrf){model.saving=false;updateControls();}}
  });
  document.querySelectorAll('[data-project]').forEach(b=>b.addEventListener('click',()=>selectProject(b.dataset.project)));
  $('catalog-search').addEventListener('input',renderCatalog);$('station-filter').addEventListener('change',renderTables);$('issues-only').addEventListener('change',renderTables);
  $('login-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button');b.disabled=true;$('login-error').textContent='';try{start(await api('/api/auth/login',{method:'POST',body:Object.fromEntries(new FormData(e.currentTarget))}));}catch(e){$('login-error').textContent=e.message;}finally{b.disabled=false;}});
  $('logout').addEventListener('click',async()=>{try{await api('/api/auth/logout',{method:'POST',body:{}});showLogin();}catch(e){toast('退出未确认：'+e.message,true);}});
  $('backup').addEventListener('click',async()=>{model.saving=true;updateControls();const identity=model.csrf;
    try{const d=await api('/api/admin/backup',{method:'POST',body:{},timeout:40000});if(model.loggedIn&&identity===model.csrf){const a=node('a','下载本次备份');a.href=d.download_url;a.download=d.file;$('backup-result').replaceChildren(a);toast('工位数据库备份完成。');}}
    catch(e){if(identity===model.csrf)toast(e.message,true);}finally{if(identity===model.csrf){model.saving=false;updateControls();}}
  });
  if(publicView)start(null);else api('/api/auth/me').then(start).catch(()=>showLogin());
})();
