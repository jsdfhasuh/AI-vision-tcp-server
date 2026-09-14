/* Referee-only UI. All untrusted text uses textContent; no frontend secret storage. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const names = {packaging:'包装箱双相机检测', screw:'螺钉漏打检测'};
  const modes = {PRACTICE:'练习场次', OFFICIAL:'正式场次', LEGACY:'旧版未分类'};
  const status = {WAITING:'待结果',RECEIVED:'已收到',TIMEOUT:'超时未收',LATE:'迟到结果',CANCELLED:'已取消',INTERRUPTED:'中断',OPEN:'进行中',CLOSED:'已结束'};
  const cases = {packaging:['合格品','正面LOGO破损','侧面LOGO破损','正面LOGO脏污','侧面LOGO脏污','产品型号错误','标签类型错误','标签位置异常','标签方向异常','强光合格品','暗光合格品'],screw:['完整品','1号螺钉漏打','2号螺钉漏打','3号螺钉漏打','4号螺钉漏打','强光完整品','强光漏打品','暗光完整品','暗光漏打品']};
  let csrf = '', state = null, online = false, busy = false, loggedIn = false, tab = 'live';
  let seenSid = null, brandingLoaded = false, historyOffset = 0, historyTotal = 0;
  let detailSid = null, detailOffset = 0, detailTotal = 0, toastTimer, pollTimer;
  const f = id => Object.fromEntries(new FormData($(id)).entries());
  const field = (form, name) => $(form).elements.namedItem(name);
  const stamp = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '—';
  const ms = v => v == null ? '—' : Number(v).toFixed(1);
  const msg = (text, bad = false) => {
    $('toast').textContent = text; $('toast').className = 'toast' + (bad ? ' error':'');
    $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, bad ? 7500:4200);
  };
  function pill(el, label, tone='neutral') { el.textContent=label; el.className='pill '+tone; }
  function node(tag,text,cls) { const n=document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls; return n; }
  function id() {
    const b=new Uint8Array(16); crypto.getRandomValues(b);
    return Array.from(b,n=>n.toString(16).padStart(2,'0')).join('');
  }
  async function api(path, options={}) {
    const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),12000);
    try {
      const r=await fetch(path,{credentials:'same-origin',cache:'no-store',...options,signal:controller.signal,
        headers:{...(options.method==='POST'?{'Content-Type':'application/json','X-CSRF-Token':csrf,'X-Request-ID':id()}:{}),...(options.headers||{})}});
      const d=await r.json();
      if(!r.ok) {
        if(r.status===401 && !path.endsWith('/login')) showLogin('登录已过期。重新登录后可继续查看，服务端检测不会停止。');
        const e=new Error(d.error?.message || '请求未成功'); e.code=d.error?.code; e.http=r.status; throw e;
      }
      return d;
    } catch(e) {
      if(e.name==='AbortError') throw new Error(options.method==='POST'?'提交响应超时，结果未知；请刷新核对记录，不要反复点击。':'服务器响应超时。');
      throw e;
    } finally { clearTimeout(timer); }
  }
  function showLogin(message='') {
    loggedIn=false; online=false; csrf=''; state=null; seenSid=null; brandingLoaded=false; clearTimeout(pollTimer);
    $('login-screen').hidden=false; $('app-screen').hidden=true; $('login-error').textContent=message;
    field('login-form','password').value=''; $('access-code').textContent='•••• ••••';
  }
  async function signedIn(d) {
    loggedIn=true; csrf=d.csrf; $('actor').textContent=d.username;
    $('login-screen').hidden=true; $('app-screen').hidden=false; $('login-error').textContent='';
    field('login-form','password').value=''; await refresh(); schedule();
  }
  function controls() {
    const locked=!online || busy || !state || !!state.fatal;
    document.querySelectorAll('[data-control]').forEach(b=>b.disabled=locked);
    const s=state?.session, active=!!state?.active_id;
    $('new-session').disabled=locked || active;
    $('end-session').disabled=locked || !s || active;
    $('start-round').disabled=locked || !s || !state?.ready || active;
    $('cancel-round').disabled=locked || !active;
    $('set-target').disabled=locked || !s || s.competition!=='packaging' || active;
    $('start-listener').disabled=locked || active || state?.listening!=='未监听';
    $('stop-listener').disabled=locked || active || state?.listening==='未监听';
    $('export-current').disabled=!loggedIn || !online || busy || !s;
    $('reveal-code').disabled=!loggedIn || !online || !s;
    document.querySelectorAll('#target-form input').forEach(x=>x.disabled=!s||s.competition!=='packaging'||active);
    $('db-check').disabled=$('db-backup').disabled=!online || busy;
  }
  function rows(target, data) {
    const frag=document.createDocumentFragment();
    for(const r of data) {
      const tr=node('tr');
      const values=[String(r.number).padStart(2,'0'),r.case_name,r.expected,r.actual??'—',r.matched==null?'待核对':r.matched?'一致':'不一致',status[r.status]??r.status,ms(r.elapsed_ms),stamp(r.received_utc)];
      values.forEach((value,i)=>{
        const td=node('td');
        if(i===3 && r.actual) td.append(node('span',value,'pill '+(r.actual==='OK'?'good':'warn')));
        else if(i===4 && r.matched!=null) td.append(node('span',value,'pill '+(r.matched?'good':'warn')));
        else td.textContent=value;
        if(i===1) td.title=r.case_name;
        tr.append(td);
      }); frag.append(tr);
    }
    $(target).replaceChildren(frag);
  }
  function render(s) {
    state=s; const session=s.session, sid=session?.id??null;
    pill($('web-link'),'实时连接','good'); $('offline-alert').hidden=true;
    $('fatal-alert').hidden=!s.fatal; $('fatal-alert').textContent=s.fatal||'';
    pill($('tcp-state'),s.listening==='未监听'?'TCP 已停止':'TCP · '+s.listening,s.listening==='未监听'?'warn':'good');
    $('sync-time').textContent='更新于 '+new Date(s.generated_utc).toLocaleTimeString('zh-CN',{hour12:false});
    $('current-team').textContent=session?.team||'尚未建立场次';
    $('current-session-meta').textContent=session?`${names[session.competition]}  ·  客户端 ${session.client_id}  ·  场次 ${sid.slice(0,8)}`:'先创建练习场次完成联调，再开始正式比赛。';
    pill($('mode-badge'),session?modes[session.mode]:'候场',session?.mode==='OFFICIAL'?'official':'neutral');
    pill($('client-state'),s.ready?'参赛端已就绪':s.connected?'等待目标确认':'参赛端未连接',s.ready?'good':s.connected?'warn':'neutral');
    const st=s.stats||{};
    $('s-total').textContent=st.total??0; $('s-received').textContent=st.received??0;
    $('s-matched').textContent=st.matched??0; $('s-timeout').textContent=`${st.timeout??0} / ${st.late??0}`;
    pill($('target-revision'),session?.competition==='packaging'?'版本 '+session.target_revision:'本赛项不使用','neutral');
    $('target-hint').textContent=session?.competition==='screw'?'螺钉赛项无需箱型；仍由参赛软件汇总四个位置并发送最终结果。':'仅包装箱赛项使用；修改后须等待参赛端重新确认。';
    if(sid!==seenSid) {
      seenSid=sid; $('access-code').textContent='•••• ••••'; $('reveal-code').textContent='显示接入码';
      if(session) {
        for(const k of ['box_type','product_model','label_type']) field('target-form',k).value=session.target?.[k]||'';
        const options=cases[session.competition];
        $('case-options').replaceChildren(...options.map(x=>{const o=node('option');o.value=x;return o;}));
        field('round-form','case_name').value=options[0];
      }
    }
    if(!brandingLoaded) {
      field('branding-form','title').value=s.branding.title; field('branding-form','subtitle').value=s.branding.subtitle; brandingLoaded=true;
    }
    const r=s.rows[0]; $('last-verdict').textContent=r?.actual??'—';
    $('last-verdict').className='verdict '+(r?.actual==='OK'?'good':r?.actual==='NG'?'warn':'');
    pill($('last-status'),r?`第 ${r.number} 轮 · ${status[r.status]}`:'等待记录',r?.status==='RECEIVED'?'good':'neutral');
    $('last-match').textContent=!r?'尚无核对结果':r.matched==null?'尚未收到有效最终结果':r.matched?'与裁判预期一致':'与裁判预期不一致';
    $('last-timing').textContent=r?`服务端观测耗时 ${ms(r.elapsed_ms)} ms`:'样件不合格输出 NG，也可能是正确识别。';
    rows('live-rows',s.rows); $('live-empty').hidden=!!s.rows.length; controls();
  }
  async function refresh() {
    if(!loggedIn)return;
    try { const s=await api('/api/admin/state'); if(!loggedIn)return; online=true; render(s); }
    catch(e) { if(!loggedIn)return; online=false; pill($('web-link'),'连接中断','warn'); $('offline-alert').hidden=false; controls(); }
  }
  function schedule() {
    clearTimeout(pollTimer); if(!loggedIn)return;
    pollTimer=setTimeout(async()=>{ await refresh(); if(tab==='database')await loadJobs().catch(()=>{}); schedule(); },1000);
  }
  async function command(name,data={},confirmText='') {
    if(busy||!online) {msg('请等待连接恢复或当前操作完成。',true);return null;}
    const isJob=['backup','export','check'].includes(name);
    const payload=isJob?data:{...data,state_token:state.state_token}; // capture BEFORE confirmation
    if(confirmText&&!window.confirm(confirmText))return null;
    busy=true; controls();
    try {
      const d=await api('/api/admin/commands/'+name,{method:'POST',body:JSON.stringify(payload)});
      msg(d.job?'任务已提交，正在后台处理。':'操作已完成。');
      if(d.job){showTab('database');await loadJobs();}
      return d;
    } catch(e) {msg(e.message,true); return null;}
    finally {busy=false;await refresh();controls();}
  }
  function showTab(name) {
    tab=name; document.querySelectorAll('[data-panel]').forEach(n=>n.hidden=n.dataset.panel!==name);
    document.querySelectorAll('[data-tab]').forEach(n=>n.classList.toggle('active',n.dataset.tab===name));
    $('page-title').textContent={live:'现场控制',history:'历史场次',database:'数据与备份',settings:'展板设置'}[name];
    if(name==='history') loadHistory().catch(e=>msg(e.message,true));
    if(name==='database') loadJobs().catch(e=>msg(e.message,true));
  }
  async function loadHistory() {
    const q=new URLSearchParams({...f('history-form'),offset:historyOffset,limit:30});
    const d=await api('/api/admin/sessions?'+q); historyTotal=d.total;
    const frag=document.createDocumentFragment();
    for(const r of d.sessions) {
      const tr=node('tr'); [stamp(r.created_utc),r.team,names[r.competition],modes[r.mode],status[r.state],r.rounds].forEach(v=>tr.append(node('td',v)));
      const td=node('td'); const b=node('button','查看','text-button'); b.type='button'; b.addEventListener('click',()=>{detailSid=r.id;detailOffset=0;loadDetail().catch(e=>msg(e.message,true));});td.append(b);tr.append(td);frag.append(tr);
    }
    $('history-rows').replaceChildren(frag); $('history-page').textContent=`共 ${d.total} 场 · 第 ${Math.floor(historyOffset/30)+1} 页`;
    $('history-prev').disabled=historyOffset===0; $('history-next').disabled=historyOffset+30>=d.total;
  }
  async function loadDetail() {
    if(!detailSid)return;
    const d=await api(`/api/admin/sessions/${detailSid}?offset=${detailOffset}&limit=100`);
    detailTotal=d.stats.total; $('history-detail').hidden=false;
    $('history-detail-title').textContent=d.session.team+' · '+modes[d.session.mode]+' · 只读';
    rows('history-detail-rows',d.rows); $('detail-page').textContent=`共 ${detailTotal} 轮 · 第 ${Math.floor(detailOffset/100)+1} 页`;
    $('detail-prev').disabled=detailOffset===0; $('detail-next').disabled=detailOffset+100>=detailTotal;
  }
  function renderDb(d) {
    $('db-info-card').hidden=false;
    const pairs=[['完整性检查',d.integrity_ok?'通过':'未通过，请暂停比赛并保留数据库'],['结构版本',d.schema_version],['SQLite 运行时',d.sqlite_runtime],['日志模式',d.journal_mode],['场次 / 轮次',`${d.counts.sessions} / ${d.counts.rounds}`],['剩余磁盘空间',(d.free_disk_bytes/1024**3).toFixed(2)+' GiB'],['数据文件',d.path],['检查时间',stamp(d.checked_utc)]];
    const frag=document.createDocumentFragment();pairs.forEach(([k,v])=>frag.append(node('dt',k),node('dd',v)));$('db-info').replaceChildren(frag);
    $('db-warning').hidden=!d.runtime_warning; $('db-warning').textContent=d.runtime_warning||'';
  }
  async function loadJobs() {
    const d=await api('/api/admin/jobs'); const frag=document.createDocumentFragment();
    let shownCheck=false;
    for(const j of d.jobs) {
      const row=node('div',null,'job-row');const p=node('span',null);pill(p,{QUEUED:'排队中',RUNNING:'处理中',DONE:'已完成',FAILED:'失败'}[j.status],j.status==='DONE'?'good':j.status==='FAILED'?'warn':'neutral');
      const c=node('div',null,'job-copy');c.append(node('strong',({backup:'数据库备份',export:'场次证据导出',check:'数据库检查'}[j.kind])+(j.automatic?' · 自动':'')),node('small',stamp(j.created_utc)+(j.error?' · '+j.error:'')));row.append(p,c);
      if(j.download_ready){const a=node('a','下载私有文件 ↧','download');a.href=`/api/admin/jobs/${j.id}/download`;row.append(a);}
      if(j.kind==='check'&&j.status==='DONE'&&!shownCheck){renderDb(j.result);shownCheck=true;}
      frag.append(row);
    }
    if(!d.jobs.length)frag.append(node('div','暂无后台任务。可发起检查或一致性备份。','empty'));
    $('jobs-list').replaceChildren(frag);
  }
  $('login-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button');b.disabled=true;$('login-error').textContent='';try{await signedIn(await api('/api/auth/login',{method:'POST',body:JSON.stringify(f('login-form'))}));}catch(err){$('login-error').textContent=err.message;}finally{b.disabled=false;}});
  $('logout-button').addEventListener('click',async()=>{try{await api('/api/auth/logout',{method:'POST',body:'{}'});showLogin();}catch(e){msg(e.message,true);}});
  document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.tab)));
  $('session-form').addEventListener('submit',e=>{e.preventDefault();const d=f('session-form');command('new_session',d,`确认创建${modes[d.mode]}：${d.team}？${state?.session?'当前场次将结束，旧连接失效。':''}`);});
  $('target-form').addEventListener('submit',e=>{e.preventDefault();command('target',f('target-form'));});
  $('round-form').addEventListener('submit',e=>{e.preventDefault();const d=f('round-form');d.timeout_ms=Number(d.timeout_ms);command('start_round',d);});
  $('cancel-round').addEventListener('click',()=>{const reason=prompt('请输入取消本轮原因（记录将保留，不会补成 NG）：');if(reason?.trim())command('cancel_round',{reason});});
  $('end-session').addEventListener('click',()=>command('end_session',{},'确认结束当前场次？已接收记录会保留，参赛端将断开。'));
  $('reveal-code').addEventListener('click',async()=>{if($('access-code').textContent!=='•••• ••••'){$('access-code').textContent='•••• ••••';$('reveal-code').textContent='显示接入码';return;}try{const d=await api('/api/admin/access-code');if(d.session_id===state?.session?.id){$('access-code').textContent=d.access_code;$('reveal-code').textContent='隐藏接入码';}}catch(e){msg(e.message,true);}});
  $('export-current').addEventListener('click',()=>{if(state?.session)command('export',{session_id:state.session.id});});
  $('export-history').addEventListener('click',()=>{if(detailSid)command('export',{session_id:detailSid});});
  $('db-check').addEventListener('click',()=>command('check'));
  $('db-backup').addEventListener('click',()=>command('backup'));
  $('branding-form').addEventListener('submit',e=>{e.preventDefault();command('branding',f('branding-form'));});
  $('start-listener').addEventListener('click',()=>command('start_listener'));
  $('stop-listener').addEventListener('click',()=>command('stop_listener',{},'确认停止 TCP 监听？参赛客户端将断开。'));
  $('history-form').addEventListener('submit',e=>{e.preventDefault();historyOffset=0;loadHistory().catch(x=>msg(x.message,true));});
  $('history-prev').addEventListener('click',()=>{historyOffset=Math.max(0,historyOffset-30);loadHistory().catch(e=>msg(e.message,true));});
  $('history-next').addEventListener('click',()=>{if(historyOffset+30<historyTotal)historyOffset+=30;loadHistory().catch(e=>msg(e.message,true));});
  $('detail-prev').addEventListener('click',()=>{detailOffset=Math.max(0,detailOffset-100);loadDetail().catch(e=>msg(e.message,true));});
  $('detail-next').addEventListener('click',()=>{if(detailOffset+100<detailTotal)detailOffset+=100;loadDetail().catch(e=>msg(e.message,true));});
  api('/api/auth/me').then(signedIn).catch(()=>showLogin());
})();
