/* Offline-capable, read-only competition display. No external scripts or fonts.
 * Live mode NEVER falls back to mock data. Local preview is explicitly labelled.
 */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const stage = $("stage");
  const demo = document.querySelector('meta[name="vision-display-mode"]').content === "demo";
  const statusNames = { WAITING: "等待结果", RECEIVED: "已接收", TIMEOUT: "超时未收", LATE: "迟到结果", CANCELLED: "裁判取消", INTERRUPTED: "测试中断" };
  const competitionNames = { packaging: "包装箱双相机检测", screw: "螺钉漏打检测" };
  let snapshot = null;
  let fresh = false;
  let lastFreshAt = 0;
  let elapsedBase = null;
  let elapsedAt = 0;
  let tableSignature = "";
  let chartSignature = "";
  let toolbarTimer = null;
  let toastTimer = null;
  let demoStep = 0;
  let demoMode = "packaging";
  let demoPaused = false;
  let pollTimer = null;
  let pollInProgress = false;
  let demoStarted = performance.now();

  function text(id, value) { $(id).textContent = String(value); }
  function fit() { stage.style.setProperty("--scale", String(Math.min(innerWidth / 1920, innerHeight / 1080))); }
  function number(value) { return Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 1 }) : "—"; }
  function clockTime(value) {
    if (!value) return "—";
    const dt = new Date(value);
    return Number.isNaN(dt.getTime()) ? "—" : dt.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function updateClock() {
    const now = new Date();
    text("clock", clockTime(now));
    text("date", now.toLocaleDateString("zh-CN", {year:"numeric", month:"2-digit", day:"2-digit", weekday:"short"}));
  }
  function setElapsed(value) {
    const host = $("elapsed");
    host.replaceChildren(document.createTextNode(number(value)));
    const unit = document.createElement("small");
    unit.textContent = " ms";
    host.append(unit);
  }
  function toast(message) {
    text("toast", message); $("toast").hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3800);
  }
  function showToolbar() {
    stage.classList.remove("presentation");
    $("toolbar").classList.add("visible");
    clearTimeout(toolbarTimer);
    toolbarTimer = setTimeout(() => {
      if (!$("toolbar").contains(document.activeElement)) $("toolbar").classList.remove("visible");
      if (document.fullscreenElement) stage.classList.add("presentation");
    }, 3200);
  }
  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
      else toast("浏览器未提供全屏接口，请按 F11 全屏。");
    } catch (_) { toast("浏览器未允许全屏，请按 F11，或再次点击全屏按钮。"); }
  }
  function updateFullscreenButton() {
    text("fullscreen-button", document.fullscreenElement ? "⛶ 退出全屏" : "⛶ 全屏");
    showToolbar();
  }
  function toggleContrast() {
    const light = document.body.classList.toggle("light");
    text("contrast-button", light ? "深色模式" : "明亮模式");
    try { localStorage.setItem("vision-display-theme", light ? "light" : "dark"); } catch (_) { /* file:// and private mode can reject storage */ }
  }
  function renderConnection(data, stale) {
    const el = $("client-status");
    const info = data?.connection;
    let label = "等待连接", state = "offline", detail = "等待服务端状态";
    if (stale) { label = "状态未知"; detail = "展板数据已暂停更新"; }
    else if (info?.fault) { label = "服务异常"; detail = "请联系现场裁判"; }
    else if (!data?.session) { label = "待开赛"; state = "idle"; detail = "裁判尚未建立场次"; }
    else if (!info?.listening) { label = "TCP 未监听"; detail = "检测服务尚未启动"; }
    else if (!info?.connected) { label = "客户端未连接"; detail = "等待参赛端建立连接"; }
    else if (!info?.ready) { label = "客户端已连接"; state = "pending"; detail = "等待确认目标版本"; }
    else { label = "客户端已就绪"; state = "ready"; detail = "TCP 已连接 · 目标已确认"; }
    el.dataset.state = state;
    el.querySelector("span").textContent = label;
    text("client-description", detail);
  }
  function renderHero(data, stale) {
    const row = data?.latest;
    let value = "待开赛", description = "准备就绪，等待比赛开始", detail = "展板只展示结果，不参与视觉检测与裁判评分。";
    let state = "idle", icon = "M60 90h60", source = "等待实时数据";
    if (stale) {
      value = "已离线"; description = "展板数据暂不可用"; detail = "正在自动重连；旧结果不会作为实时结果展示。"; state = "warning";
    } else if (data?.connection.fault) {
      value = "已暂停"; description = "检测服务异常，请联系裁判"; detail = "已停止展示当前判定，请以裁判端核验为准。"; state = "warning";
    } else if (!data?.session) {
      source = "等待场次建立";
    } else if (!row) {
      value = "待检测"; description = "等待裁判开始第一轮测试"; source = "已建立比赛场次";
      detail = "参赛软件完成检测后，将上报最终 OK / NG。";
    } else if (row.status === "WAITING") {
      value = "待结果"; state = "waiting";
      description = !data.connection.connected ? "本轮等待中 · 客户端已断开" : row.action === "arm" ? "轮次已布置 · 请按现场约定触发" : "已下发检测命令 · 等待最终结果";
      detail = row.timeout_ms > 0 ? `本轮时限 ${number(row.timeout_ms)} ms；超时不会自动判为 NG。` : "本轮只记录耗时，不设置自动超时判定。";
      icon = "M90 48v42l28 17"; source = "参赛端结果待接收";
    } else if (row.actual === "OK" || row.actual === "NG") {
      value = row.actual; state = row.actual.toLowerCase();
      description = row.actual === "OK" ? "产品合格 · 客户端判定" : "产品不合格 · 客户端判定";
      detail = row.actual === "OK" ? "已收到参赛端最终结果，最终评分由裁判确认。" : "NG 是产品判定，不等于参赛者检测错误。";
      icon = row.actual === "OK" ? "M56 92l23 23 46-49" : "M65 65l50 50M115 65l-50 50";
      source = row.status === "LATE" ? "迟到结果 · 不计为按时到达" : "最终结果已接收";
    } else {
      const labels = { TIMEOUT: ["已超时", "规定时间内未收到有效结果", "未收到结果不等于 NG，请以裁判端记录为准。"], CANCELLED: ["已取消", "本轮测试已由裁判取消", "该轮不产生正常检测结果，等待下一轮。"], INTERRUPTED: ["已中断", "本轮测试未正常完成", "该轮不产生正常检测结果，请联系裁判。"] };
      [value, description, detail] = labels[row.status] || ["待核验", "未知的轮次状态", "请联系现场裁判核验。"];
      state = "warning"; icon = "M90 52v48M90 120v2"; source = "没有有效产品判定";
    }
    $("hero").dataset.state = state;
    text("result-value", value); $("result-value").classList.toggle("long", value.length > 2);
    text("result-description", description); text("result-detail", detail); text("result-source", source);
    text("result-heading", row && row.status !== "WAITING" ? "最近一轮检测结果" : "当前检测结果");
    $("state-icon").setAttribute("d", icon);
    setElapsed(stale || data?.connection.fault ? null : row?.elapsed_ms);
    text("receipt-status", stale ? "状态未知" : (row ? statusNames[row.status] || "待核验" : "尚无记录"));
    text("received-time", stale ? "—" : clockTime(row?.received_utc));
    elapsedBase = !stale && !data?.connection.fault && row?.status === "WAITING" ? row.elapsed_ms : null;
    elapsedAt = performance.now();
  }
  function renderTable(rows) {
    const signature = JSON.stringify(rows.map((r) => [r.number, r.actual, r.status, r.status === "WAITING" ? null : r.elapsed_ms, r.received_utc]));
    if (signature === tableSignature) return;
    tableSignature = signature;
    const host = $("records-body"); host.replaceChildren();
    $("records-empty").hidden = rows.length > 0;
    for (const row of rows) {
      const tr = document.createElement("tr");
      function cell(value, css = "") { const td = document.createElement("td"); td.textContent = value; td.className = css; tr.append(td); return td; }
      cell(String(row.number).padStart(2, "0"));
      const badge = document.createElement("span"); badge.className = "result-chip" + (row.actual === "OK" ? " ok" : row.actual === "NG" ? " ng" : ""); badge.textContent = row.actual || "—";
      cell("").append(badge);
      cell(statusNames[row.status] || "未知状态", ["TIMEOUT", "LATE", "INTERRUPTED"].includes(row.status) ? "row-warning" : "");
      cell(row.status === "WAITING" ? "计时中" : (number(row.elapsed_ms) + (row.elapsed_ms == null ? "" : " ms")), "right numeric");
      cell(clockTime(row.received_utc), "right numeric");
      host.append(tr);
    }
  }
  function renderChart(rows) {
    const signature = JSON.stringify(rows);
    if (signature === chartSignature) return;
    chartSignature = signature;
    const valid = rows.filter((r) => Number.isFinite(r.elapsed_ms));
    const svg = $("timing-chart"); svg.replaceChildren();
    $("chart-empty").hidden = valid.length > 0;
    function add(tag, attrs, content) { const el = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, String(v))); if (content != null) el.textContent = content; svg.append(el); return el; }
    if (!valid.length) { text("timing-average", "—"); text("timing-max", "—"); return; }
    const vals = valid.map((r) => r.elapsed_ms), peak = Math.max(...vals), avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    text("timing-average", number(Math.round(avg))); text("timing-max", number(Math.round(peak)));
    const yTop = Math.max(100, Math.ceil(peak * 1.15 / 100) * 100);
    const left = 55, right = 635, top = 10, base = 106;
    for (const fraction of [0, .5, 1]) {
      const y = base - (base - top) * fraction;
      add("path", {d:`M${left},${y}H${right}`, class:"chart-grid"});
      add("text", {x:left - 10, y:y + 4, "text-anchor":"end"}, number(Math.round(yTop * fraction)));
    }
    const points = valid.map((r, i) => [valid.length === 1 ? (left + right) / 2 : left + (right - left) * i / (valid.length - 1), base - (base - top) * r.elapsed_ms / yTop]);
    const d = points.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ");
    if (points.length > 1) add("path", {d:d + `L${points.at(-1)[0]},${base}L${points[0][0]},${base}Z`, class:"chart-fill"});
    add("path", {d, class:"chart-line"});
    points.forEach(([x,y], i) => { add("circle", {cx:x, cy:y, r:3, class:"chart-point" + (valid[i].late ? " late" : "")}); });
    const indices = [...new Set([0, Math.floor((valid.length - 1) / 2), valid.length - 1])];
    indices.forEach((i) => add("text", {x:points[i][0], y:129, "text-anchor":"middle"}, `R${String(valid[i].number).padStart(2,"0")}`));
  }
  function setFeed(data, stale, message) {
    stage.classList.toggle("stale", stale);
    const el = $("feed-status");
    el.replaceChildren(document.createElement("i"), document.createTextNode(message));
    el.className = "feed-status " + (stale ? "warning" : "live");
    $("connection-banner").hidden = !stale;
    text("banner-title", snapshot ? "展板数据已暂停更新" : "尚未连接展板服务");
    text("banner-description", snapshot ? "正在自动重连。下方仅保留上次记录，不代表当前比赛实时状态。" : "请先启动裁判服务端。此页面不会自行生成比赛数据。");
    renderConnection(data, stale);
  }
  function render(data, stale = false, message = "") {
    const session = data?.session;
    if (data) {
      text("event-title", data.title); text("event-subtitle", data.subtitle);
      $("event-title").title = data.title;
      document.title = `${data.title} · 现场展板`;
    }
    text("team-name", session?.team || "等待场次建立"); $("team-name").title = session?.team || "";
    text("competition-name", competitionNames[session?.competition] || "尚未选择赛项");
    const purpose = {PRACTICE: "练习联调", OFFICIAL: "正式比赛", LEGACY: "旧版未分类"}[session?.mode];
    text("contest-label", purpose ? `竞赛项目 · ${purpose}` : "竞赛项目");
    text("round-number", data?.latest ? String(data.latest.number).padStart(3, "0") : "—");
    renderHero(data, stale);
    const counts = data?.counts || {};
    for (const key of ["total","received","ok","ng"]) text(`count-${key}`, number(counts[key] || 0));
    text("receipt-note", `超时未收 ${counts.timeout || 0} · 迟到 ${counts.late || 0}`);
    text("round-goal", `已接收 ${counts.received || 0} / 10 · 非连续测试判定`);
    $("progress-fill").style.width = `${Math.min(100, (counts.received || 0) * 10)}%`;
    renderTable(data?.recent || []); renderChart(data?.timings || []);
    setFeed(data, stale, message || (demo ? "界面演示 · 模拟数据，不计入比赛" : `展板在线 · 更新于 ${clockTime(data?.generated_utc)}`));
  }
  function markStale(message) {
    fresh = false;
    renderHero(snapshot, true);
    setFeed(snapshot, true, message);
  }
  async function poll() {
    if (demo || pollInProgress) return;
    pollInProgress = true;
    clearTimeout(pollTimer);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 1800);
    try {
      const response = await fetch("/api/display", {cache:"no-store", credentials:"omit", signal:controller.signal});
      if (!response.ok) throw new Error("http");
      const packet = await response.json();
      if (!packet.available || !packet.data || packet.data.schema !== 1 || packet.data.demo !== false) throw new Error("schema");
      // Do not refresh the apparent live age from stale responses or stalled publishers.
      if (packet.stale || packet.age_ms > 2500 || packet.age_ms == null) { markStale("展板快照已过期 · 自动重连中"); return; }
      snapshot = packet.data; fresh = true; lastFreshAt = performance.now() - Math.max(0, packet.age_ms);
      render(snapshot);
    } catch (_) { markStale("展板连接中断 · 自动重连中"); }
    finally { clearTimeout(timeout); pollInProgress = false; pollTimer = setTimeout(poll, 500); }
  }

  // Isolated preview dataset: embedded only in explicitly marked local HTML mode.
  // Live pages never call this function and never display these values on failure.
  function makeDemo() {
    const now = new Date();
    const timings = [732, 688, 854, 705, 643, 791, 667, 719, 610, 682, 704, 628];
    const results = ["OK", "NG", "OK", "OK", "NG", "OK", "OK", "NG", "OK", "OK", "NG", "OK"];
    const variants = ["OK", "NG", "WAITING", "TIMEOUT", "DISCONNECTED", "LATE", "CANCELLED", "INTERRUPTED"];
    const variant = variants[demoStep % variants.length];
    if (variant === "NG") results[11] = "NG";
    let rows = results.map((actual, i) => ({number:i + 1, status:"RECEIVED", actual, elapsed_ms:timings[i], timeout_ms:0, started_utc:new Date(now.getTime() - (12-i)*18000).toISOString(), received_utc:new Date(now.getTime() - (12-i)*18000 + timings[i]).toISOString()}));
    if (["WAITING","TIMEOUT","CANCELLED","INTERRUPTED"].includes(variant)) {
      rows.push({number:13, status:variant, actual:null, elapsed_ms:variant === "WAITING" ? performance.now() - demoStarted : null, timeout_ms: variant === "TIMEOUT" ? 2000 : 0, started_utc:now.toISOString(), received_utc:null});
    }
    if (variant === "LATE") rows[11] = {...rows[11], status:"LATE", actual:"NG", elapsed_ms:2410, timeout_ms:2000};
    const done = rows.filter((r) => r.actual != null);
    return {schema:1, demo:true, title:"工业视觉技能竞赛", subtitle:"双赛项现场展示 · 以视觉见实力", generated_utc:now.toISOString(), session:{display_id:"DEMO_ONLY",team:"智视先锋队 · 03",competition:demoMode}, connection:{listening:true,connected:variant !== "DISCONNECTED",ready:variant !== "DISCONNECTED",fault:false}, counts:{total:rows.length,received:done.length,ok:done.filter((r) => r.actual === "OK").length,ng:done.filter((r) => r.actual === "NG").length,timeout:variant === "TIMEOUT" ? 1 : 0,late:variant === "LATE" ? 1 : 0}, latest:rows.at(-1), recent:[...rows].reverse().slice(0,6), timings:done.map((r) => ({number:r.number,elapsed_ms:r.elapsed_ms,late:r.status === "LATE"}))};
  }
  function showDemo() { snapshot = makeDemo(); fresh = true; lastFreshAt = performance.now(); render(snapshot); }
  function nextDemo() { demoStep += 1; demoStarted = performance.now(); showDemo(); }

  window.addEventListener("resize", fit);
  document.addEventListener("mousemove", showToolbar);
  document.addEventListener("touchstart", showToolbar, {passive:true});
  document.addEventListener("keydown", (event) => {
    if (event.key.toLowerCase() === "f" && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); fullscreen(); }
    if (event.key === "Tab") showToolbar();
  });
  document.addEventListener("fullscreenchange", updateFullscreenButton);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !demo) { if (performance.now() - lastFreshAt > 2500) markStale("正在恢复展板连接"); poll(); } });
  window.addEventListener("online", () => { if (!demo) poll(); });
  window.addEventListener("offline", () => { if (!demo) markStale("网络已断开 · 自动重连中"); });
  $("fullscreen-button").addEventListener("click", fullscreen);
  $("contrast-button").addEventListener("click", toggleContrast);
  try { if (localStorage.getItem("vision-display-theme") === "light") toggleContrast(); } catch (_) {}
  fit(); updateClock(); showToolbar();
  setInterval(updateClock, 1000);
  setInterval(() => {
    if (!demo && fresh && performance.now() - lastFreshAt > 2500) markStale("展板数据已过期 · 自动重连中");
    if (fresh && elapsedBase != null) setElapsed(elapsedBase + performance.now() - elapsedAt);
  }, 100);
  if (demo) {
    $("demo-badge").hidden = false; $("demo-controls").hidden = false;
    $("demo-mode").addEventListener("click", () => { demoMode = demoMode === "packaging" ? "screw" : "packaging"; showDemo(); });
    $("demo-next").addEventListener("click", nextDemo);
    $("demo-pause").addEventListener("click", () => { demoPaused = !demoPaused; text("demo-pause", demoPaused ? "继续演示" : "暂停演示"); });
    showDemo(); setInterval(() => { if (!demoPaused) nextDemo(); }, 6500);
  } else { render(null, true, "正在连接展板服务"); poll(); }
})();
