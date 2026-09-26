/* 公交刷卡票价核验台 —— 原生前端逻辑（无框架、无构建） */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------------- 通用请求 / 提示 ---------------- */

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (_) { /* 非 JSON 响应 */ }
  if (!resp.ok) {
    const err = new Error("HTTP " + resp.status);
    err.payload = data;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(message, ok = true) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast " + (ok ? "ok" : "bad");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

function putText(path, text) {
  return api(path, { method: "PUT", body: JSON.stringify({ text }) });
}

function yuan(v) {
  return Number(v).toFixed(2);
}

function actionText(a) {
  return a === "IN" ? "进 ▲" : "出 ▼";
}

/* ---------------- 1. 规则参数 ---------------- */

async function loadConfig() {
  const cfg = await api("/api/config");
  $("cfg-split").value = cfg.day_split_hour;
  $("cfg-minutes").value = cfg.transfer_minutes;
  $("cfg-discount").value = cfg.transfer_discount;
  $("cfg-cap").value = cfg.daily_cap;
  return cfg;
}

async function saveConfig() {
  const body = {
    day_split_hour: Number($("cfg-split").value),
    transfer_minutes: Number($("cfg-minutes").value),
    transfer_discount: Number($("cfg-discount").value),
    daily_cap: Number($("cfg-cap").value),
  };
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    toast("规则参数已保存");
  } catch (e) {
    toast(summaryError(e, "参数校验失败"), false);
  }
}

/* ---------------- 2. 规则试算对比 ---------------- */

function fillTrialInputs(cfg) {
  $("trial-split").value = cfg.day_split_hour;
  $("trial-minutes").value = cfg.transfer_minutes;
  $("trial-discount").value = cfg.transfer_discount;
  $("trial-cap").value = cfg.daily_cap;
}

async function copyCurrentToTrial() {
  fillTrialInputs(await api("/api/config"));
  toast("已将当前已保存参数填入草案，可在此基础上修改");
}

function fmtConfig(c) {
  return `分界 ${c.day_split_hour}:00 · 换乘 ${c.transfer_minutes} 分钟 · ` +
         `抵扣 ${yuan(c.transfer_discount)} 元 · 封顶 ${yuan(c.daily_cap)} 元`;
}

function deltaClass(v) {
  return v > 0 ? "delta-up" : v < 0 ? "delta-down" : "delta-zero";
}

function deltaText(v) {
  return (v > 0 ? "+" : "") + yuan(v);
}

function renderTrialErrors(p) {
  const panel = $("trial-errors");
  panel.hidden = false;
  $("trial-result").hidden = true;

  const sides = [
    ["trial-err-current", p.current],
    ["trial-err-draft", p.draft],
  ];
  for (const [blockId, side] of sides) {
    const block = $(blockId);
    const ul = block.querySelector("ul");
    ul.innerHTML = "";
    const errors = (side && side.errors) || [];
    for (const e of errors) ul.appendChild(renderErrorLi(e));
    block.hidden = side ? side.ok !== false : true;
  }
}

function renderTrialResult(data) {
  $("trial-errors").hidden = true;
  const panel = $("trial-result");
  panel.hidden = false;

  $("trial-params").innerHTML =
    `<div><span class="tag tag-cur">当前配置</span>${fmtConfig(data.current.config)}</div>` +
    `<div><span class="tag tag-draft">草案配置</span>${fmtConfig(data.draft.config)}</div>`;

  const b = data.batch_summary;
  $("trial-batch").innerHTML =
    `全批次 <strong>${b.trip_count}</strong> 笔行程：` +
    `实扣合计 <strong>${yuan(b.current_charge_total)}</strong> 元 → ` +
    `<strong>${yuan(b.draft_charge_total)}</strong> 元，` +
    `<span class="${deltaClass(b.charge_diff)}">实扣差额 ${deltaText(b.charge_diff)} 元</span>` +
    `；优惠合计 ${yuan(b.current_discount_total)} → ${yuan(b.draft_discount_total)} 元` +
    `（优惠差额 ${deltaText(b.discount_diff)} 元，正=优惠更多）`;

  const tb = $("trial-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const r of data.rows) {
    const tr = document.createElement("tr");
    const trip = `${escapeHtml(r.entry_station)} → ${escapeHtml(r.exit_station)}` +
      `<div class="sub">${r.entry_time} ～ ${r.exit_time}</div>`;
    // 优惠差额正负的颜色与实扣相反：优惠变多（正）对乘客是好事，用绿色
    const discCls = r.discount_diff > 0 ? "delta-down"
      : r.discount_diff < 0 ? "delta-up" : "delta-zero";
    tr.innerHTML = `
      <td>${escapeHtml(r.card)}</td>
      <td class="num">${r.seq}</td>
      <td class="trip">${trip}</td>
      <td class="${r.day_changed ? "day-changed" : ""}">${r.current_operating_day}</td>
      <td class="${r.day_changed ? "day-changed" : ""}">${r.draft_operating_day}</td>
      <td class="num discount">-${yuan(r.current_discount)}</td>
      <td class="num discount">-${yuan(r.draft_discount)}</td>
      <td class="num ${discCls}">${deltaText(r.discount_diff)}</td>
      <td class="num">${yuan(r.current_charge)}</td>
      <td class="num charge">${yuan(r.draft_charge)}</td>
      <td class="num ${deltaClass(r.charge_diff)}">${deltaText(r.charge_diff)}</td>
      <td class="wrap">${escapeHtml(r.draft_discount_source)}</td>`;
    tb.appendChild(tr);
  }

  const cb = $("trial-cards-table").querySelector("tbody");
  cb.innerHTML = "";
  for (const c of data.card_summaries) {
    const tr = document.createElement("tr");
    const discCls = c.discount_diff > 0 ? "delta-down"
      : c.discount_diff < 0 ? "delta-up" : "delta-zero";
    tr.innerHTML = `
      <td>${escapeHtml(c.card)}</td>
      <td class="num">${c.trip_count}</td>
      <td class="num">${yuan(c.current_charge_total)}</td>
      <td class="num">${yuan(c.draft_charge_total)}</td>
      <td class="num ${deltaClass(c.charge_diff)}">${deltaText(c.charge_diff)}</td>
      <td class="num discount">${yuan(c.current_discount_total)}</td>
      <td class="num discount">${yuan(c.draft_discount_total)}</td>
      <td class="num ${discCls}">${deltaText(c.discount_diff)}</td>`;
    cb.appendChild(tr);
  }
}

async function runTrialCompare() {
  const status = $("trial-status");
  const body = {
    day_split_hour: Number($("trial-split").value),
    transfer_minutes: Number($("trial-minutes").value),
    transfer_discount: Number($("trial-discount").value),
    daily_cap: Number($("trial-cap").value),
  };
  if (Object.values(body).some((v) => !Number.isFinite(v))) {
    status.textContent = "草案四项参数必须全部填写为有效数字";
    status.className = "status bad";
    return;
  }
  status.textContent = "试算中…";
  status.className = "status";
  $("trial-errors").hidden = true;
  $("trial-result").hidden = true;
  try {
    const data = await api("/api/trial-compare", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderTrialResult(data);
    status.textContent =
      `试算成功：共对比 ${data.batch_summary.trip_count} 笔行程（草案未保存、账单未写入）`;
    status.className = "status ok";
    toast("规则试算完成");
  } catch (e) {
    const p = e.payload;
    if (p && (p.current || p.draft)) {
      renderTrialErrors(p);
      const failed = [];
      if (p.current && p.current.ok === false) failed.push("当前");
      if (p.draft && p.draft.ok === false) failed.push("草案");
      status.textContent =
        `试算未通过：${failed.join("、")}方案存在错误（配置未改动、账单未写入）`;
    } else {
      status.textContent = summaryError(e, "试算请求失败");
    }
    status.className = "status bad";
    toast("规则试算未通过", false);
  }
}

/* ---------------- 2-4. 文本录入与表格渲染 ---------------- */

function renderStations(items) {
  const tb = $("stations-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const s of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(s.name)}</td><td class="num">${s.zone}</td>`;
    tb.appendChild(tr);
  }
}

function renderFares(items) {
  const tb = $("fares-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const f of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="num">${f.from_zone}</td><td class="num">${f.to_zone}</td>
      <td class="num">${yuan(f.price)}</td>`;
    tb.appendChild(tr);
  }
}

function renderRecords(items) {
  const tb = $("records-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    tr.dataset.srcLine = r.src_line;
    tr.innerHTML = `<td>${escapeHtml(r.card)}</td><td>${r.time}</td>
      <td>${escapeHtml(r.station)}</td><td>${actionText(r.action)}</td>
      <td class="num">第 ${r.src_line} 行</td>`;
    tb.appendChild(tr);
  }
}

async function saveTextSection(key, path, text, render) {
  try {
    const data = await putText(path, text);
    const fresh = await api(path);
    render(fresh.items);
    toast(`已保存 ${data.count} 条 ${key}`);
  } catch (e) {
    const errs = (e.payload && e.payload.errors) || [];
    if (errs.length) showParseErrors(errs);
    toast(summaryError(e, `保存${key}失败`), false);
  }
}

function showParseErrors(errors) {
  const list = errors.slice(0, 12).map((e) =>
    `第 ${e.location && e.location.line || "?"} 行：${e.message}`).join("\n");
  toast(`录入解析失败：\n${list}`, false);
  // toast 不支持换行样式时，仍可在核验结果区展示
  const ul = $("errors-list");
  ul.innerHTML = "";
  const panel = $("errors-panel");
  for (const e of errors) {
    ul.appendChild(renderErrorLi(e));
  }
  panel.hidden = false;
  $("bills-panel").hidden = true;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadBack(kind) {
  const fresh = await api(`/api/${kind}`);
  let text = "";
  if (kind === "stations") {
    text = fresh.items.map((s) => `${s.name}, ${s.zone}`).join("\n");
    renderStations(fresh.items);
  } else if (kind === "fares") {
    text = fresh.items.map((f) =>
      `${f.from_zone}, ${f.to_zone}, ${yuan(f.price)}`).join("\n");
    renderFares(fresh.items);
  } else if (kind === "records") {
    text = fresh.items.map((r) =>
      `${r.card}, ${r.time}, ${r.station}, ${r.action}`).join("\n");
    renderRecords(fresh.items);
  }
  $(`${kind}-text`).value = text;
  toast("已从数据库回填");
}

/* ---------------- 票价矩阵模板 ---------------- */

function genFareTemplate() {
  const n = Math.max(1, Math.min(20, Number($("fare-zones").value) || 3));
  const lines = [];
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= n; j++) {
      // 模板建议价：同区 2 元，每跨一区 +1 元（用户可自行改价）
      const price = 2 + Math.abs(i - j);
      lines.push(`${i}, ${j}, ${price}.00`);
    }
  }
  $("fares-text").value = lines.join("\n");
  toast(`已生成 ${n}×${n} 对称模板，请核对并保存`);
}

/* ---------------- 5. 核验 ---------------- */

function renderErrorLi(e) {
  const li = document.createElement("li");
  const code = document.createElement("span");
  code.className = "error-code";
  code.textContent = e.code;
  li.appendChild(code);
  li.appendChild(document.createTextNode(e.message));
  if (e.location) {
    const parts = [];
    const loc = e.location;
    if (loc.line !== undefined) parts.push(`文本第 ${loc.line} 行`);
    if (loc.card) parts.push(`卡号 ${loc.card}`);
    if (loc.time) parts.push(`时刻 ${loc.time}`);
    if (loc.station) parts.push(`站点 ${loc.station}`);
    if (parts.length) {
      const span = document.createElement("span");
      span.className = "error-loc";
      span.textContent = "定位：" + parts.join(" · ");
      li.appendChild(span);
    }
  }
  return li;
}

function renderBills(bills) {
  const tb = $("bills-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const b of bills) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(b.card)}</td>
      <td class="num">${b.seq}</td>
      <td>${b.operating_day}</td>
      <td>${b.entry_time}</td>
      <td>${b.exit_time}</td>
      <td>${escapeHtml(b.entry_station)}（${b.from_zone}区）</td>
      <td>${escapeHtml(b.exit_station)}（${b.to_zone}区）</td>
      <td class="num">${yuan(b.base_fare)}</td>
      <td class="num discount">-${yuan(b.discount)}</td>
      <td class="num charge">${yuan(b.charged_fare)}</td>
      <td class="num">${yuan(b.daily_cumulative)} / ${yuan(b.daily_cap)}</td>
      <td class="wrap">${escapeHtml(b.discount_source)}</td>`;
    tb.appendChild(tr);
  }
}

async function runVerify() {
  const status = $("verify-status");
  status.textContent = "核验中…";
  status.className = "status";
  try {
    const data = await api("/api/verify", { method: "POST" });
    renderBills(data.bills || []);
    $("bills-panel").hidden = false;
    $("errors-panel").hidden = true;
    status.textContent = `核验通过，共 ${data.bill_count} 笔行程账单`;
    status.className = "status ok";
    toast(`核验通过：${data.bill_count} 笔行程`);
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    const ul = $("errors-list");
    ul.innerHTML = "";
    for (const err of errors) ul.appendChild(renderErrorLi(err));
    $("errors-panel").hidden = false;
    $("bills-panel").hidden = true;
    status.textContent = `核验未通过：${errors.length} 个错误（未生成账单）`;
    status.className = "status bad";
    toast(`核验未通过：${errors.length} 个错误`, false);
  }
}

/* ---------------- 6. 配对修复预览（只读） ---------------- */

function renderRepairErrors(errors) {
  const ul = $("repair-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("repair-errors").hidden = false;
  $("repair-result").hidden = true;
}

function changeItemHtml(ch) {
  const where = `${escapeHtml(ch.card)} ${ch.time} @ ${escapeHtml(ch.station)}` +
    `<span class="sub">（来源第 ${ch.line} 行）</span>`;
  if (ch.op === "delete") {
    return `<span class="tag tag-del">删除</span>${where}` +
      `<span class="sub">原方向 ${actionText(ch.from_action)}</span>`;
  }
  return `<span class="tag tag-flip">翻转</span>${where}` +
    `<span class="sub">${actionText(ch.from_action)} → ${actionText(ch.to_action)}</span>`;
}

function renderRepairPlans(data) {
  $("repair-errors").hidden = true;
  const panel = $("repair-result");
  panel.hidden = false;

  const summary = $("repair-summary");
  const plansBox = $("repair-plans");
  plansBox.innerHTML = "";

  if (data.min_edits === 0) {
    summary.innerHTML = "✅ 已保存的记录可完整配对，<strong>无需修复</strong>。" +
      "（预览为只读操作，未改动任何数据）";
    return;
  }

  const hiddenNote = data.hidden_plans > 0
    ? `，另有 <strong>${data.hidden_plans}</strong> 个同成本方案未展示` : "";
  summary.innerHTML =
    `最小编辑数 <strong>${data.min_edits}</strong> 次 · ` +
    `共 <strong>${data.total_plans}</strong> 个同成本方案，` +
    `按稳定顺序展示 ${data.shown_plans} 个${hiddenNote}。<br>` +
    `<span class="sub">请人工核对后自行选择：系统不会替您选定方案；` +
    `预览全程只读，未保存记录、未清空账单。</span>`;

  data.plans.forEach((p, idx) => {
    const div = document.createElement("div");
    div.className = "repair-plan";

    const head = document.createElement("div");
    head.className = "repair-plan-head";
    head.innerHTML =
      `<h3>方案 ${idx + 1}</h3>` +
      `<span class="hint">编辑 ${p.edit_count} 次 · 变更 ${p.change_count} 条 · ` +
      `修复后 ${p.kept_count} 条记录 / ${p.trip_count} 笔行程</span>`;
    const btn = document.createElement("button");
    btn.className = "btn btn-ghost";
    btn.type = "button";
    btn.textContent = "📋 复制修复后记录";
    btn.addEventListener("click", () =>
      copyText(p.fixed_text, "已复制修复后的记录文本（仍未保存，可粘贴到记录区核对）"));
    head.appendChild(btn);
    div.appendChild(head);

    const ul = document.createElement("ul");
    ul.className = "change-list";
    for (const ch of p.changes) {
      const li = document.createElement("li");
      li.innerHTML = changeItemHtml(ch);
      ul.appendChild(li);
    }
    div.appendChild(ul);

    const pre = document.createElement("pre");
    pre.className = "fixed-text";
    pre.textContent = p.fixed_text;
    div.appendChild(pre);

    plansBox.appendChild(div);
  });
}

async function copyText(text, message) {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    toast(message || "已复制到剪贴板（仍未保存任何改动）");
  } catch (_) {
    toast("复制失败，请手动选中文本复制", false);
  }
}

async function runRepairPreview() {
  const status = $("repair-status");
  status.textContent = "搜索最小编辑方案…";
  status.className = "status";
  $("repair-errors").hidden = true;
  $("repair-result").hidden = true;
  try {
    const data = await api("/api/repair-preview", { method: "POST" });
    renderRepairPlans(data);
    if (data.min_edits === 0) {
      status.textContent = "记录已可完整配对，无需修复";
    } else {
      status.textContent =
        `已生成 ${data.shown_plans} 个最小编辑方案` +
        (data.hidden_plans > 0 ? `（另有 ${data.hidden_plans} 个未展示）` : "") +
        "，记录与账单均未改动";
    }
    status.className = "status ok";
    toast("修复预览已生成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderRepairErrors(errors);
      status.textContent =
        `存在 ${errors.length} 个非配对错误，未生成修复方案（记录与账单均未改动）`;
    } else {
      status.textContent = summaryError(e, "修复预览请求失败");
    }
    status.className = "status bad";
    toast("无法生成修复预览", false);
  }
}

/* ---------------- 8. 票价矩阵路径审计（只读） ---------------- */

let lastSuggestLines = [];

function renderAuditErrors(errors) {
  const ul = $("audit-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("audit-errors").hidden = false;
  $("audit-result").hidden = true;
}

function affectedCellHtml(list) {
  return list.map((a) =>
    `<div>${escapeHtml(a.card)} 第${a.seq}程` +
    `<span class="sub">（进站第 ${a.entry_line} 行 / 出站第 ${a.exit_line} 行）</span>` +
    `<div class="sub">${a.entry_time} ${escapeHtml(a.entry_station)} → ` +
    `${escapeHtml(a.exit_station)}</div></div>`
  ).join("");
}

function pathCellHtml(item) {
  const legs = (item.legs || []).map((l) =>
    `${l.from_zone}→${l.to_zone} ${yuan(l.price)}`).join(" ＋ ");
  return `<div class="path-text">${escapeHtml(item.path_text)}</div>` +
    (legs ? `<div class="sub">${escapeHtml(legs)}</div>` : "");
}

function renderFareAudit(data) {
  $("audit-errors").hidden = true;
  $("audit-result").hidden = false;

  const miss = data.missing || [];
  const over = data.overpriced || [];
  const unreachable = miss.filter((m) => !m.reachable).length;
  const reachable = miss.length - unreachable;

  const summary = $("audit-summary");
  if (!miss.length && !over.length) {
    summary.innerHTML =
      `✅ 共审计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个实际行程分区对：均有直达票价，` +
      `且直达票价不高于任何间接路径。<br>` +
      `<span class="sub">审计全程只读：未修改票价矩阵，未写入或清空账单。</span>`;
  } else {
    summary.innerHTML =
      `共审计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个实际行程分区对：` +
      `缺直达票价 <strong>${miss.length}</strong> 对` +
      `（可达可补录 ${reachable} 对，不可达保留 MISSING_FARE ${unreachable} 对），` +
      `直达票价高于间接路径 <strong>${over.length}</strong> 对。<br>` +
      `<span class="sub">审计全程只读：未修改票价矩阵，未写入或清空账单；` +
      `建议补录行请人工核对后粘贴到「4. 票价矩阵」保存。</span>`;
  }

  // 缺直达票价
  const mBlock = $("audit-missing-block");
  mBlock.hidden = miss.length === 0;
  const mtb = $("audit-missing-table").querySelector("tbody");
  mtb.innerHTML = "";
  lastSuggestLines = [];
  for (const m of miss) {
    const tr = document.createElement("tr");
    let statusHtml, priceHtml, pathHtml, lineHtml;
    if (m.reachable) {
      statusHtml = `<span class="tag tag-ok">可补录</span>`;
      priceHtml = `<span class="charge">${yuan(m.suggested_price)}</span>`;
      pathHtml = pathCellHtml(m);
      lineHtml = `<code>${escapeHtml(m.suggest_line)}</code> `;
      lastSuggestLines.push(m.suggest_line);
    } else {
      statusHtml = `<span class="tag tag-unreachable">不可达</span>` +
        `<div class="sub">保留 MISSING_FARE</div>` +
        (m.errors || []).map((e) =>
          `<div class="sub err-line">${escapeHtml(e.message)}</div>`).join("");
      priceHtml = "—";
      pathHtml = "—";
      lineHtml = "—";
    }
    tr.innerHTML = `
      <td class="num">${m.from_zone} → ${m.to_zone}</td>
      <td>${statusHtml}</td>
      <td class="num">${priceHtml}</td>
      <td class="wrap">${pathHtml}</td>
      <td class="wrap">${affectedCellHtml(m.affected)}</td>
      <td class="suggest-cell">${lineHtml}</td>`;
    if (m.reachable) {
      const btn = document.createElement("button");
      btn.className = "btn btn-ghost btn-mini";
      btn.type = "button";
      btn.textContent = "📋 复制";
      btn.addEventListener("click", () =>
        copyText(m.suggest_line,
          `已复制建议补录行「${m.suggest_line}」（矩阵未改动，粘贴保存后生效）`));
      tr.querySelector(".suggest-cell").appendChild(btn);
    }
    mtb.appendChild(tr);
  }
  $("btn-copy-suggests").hidden = lastSuggestLines.length === 0;

  // 直达票价高于间接路径
  const oBlock = $("audit-overpriced-block");
  oBlock.hidden = over.length === 0;
  const otb = $("audit-overpriced-table").querySelector("tbody");
  otb.innerHTML = "";
  for (const o of over) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="num">${o.from_zone} → ${o.to_zone}</td>
      <td class="num">${yuan(o.direct_price)}</td>
      <td class="num">${yuan(o.best_price)}</td>
      <td class="num delta-up">+${yuan(o.diff)}</td>
      <td class="wrap">${pathCellHtml(o)}</td>
      <td class="wrap">${affectedCellHtml(o.affected)}</td>`;
    otb.appendChild(tr);
  }
}

async function runFareAudit() {
  const status = $("audit-status");
  status.textContent = "审计中…";
  status.className = "status";
  $("audit-errors").hidden = true;
  $("audit-result").hidden = true;
  $("btn-copy-suggests").hidden = true;
  try {
    const data = await api("/api/fare-path-audit", { method: "POST" });
    renderFareAudit(data);
    const miss = data.missing || [];
    const over = data.overpriced || [];
    if (!miss.length && !over.length) {
      status.textContent = `审计通过：${data.pair_count} 个分区对均无异常（只读，未改动数据）`;
    } else {
      status.textContent =
        `审计完成：${miss.length} 对缺直达票价，${over.length} 对直达偏贵` +
        "（只读，矩阵与账单均未改动）";
    }
    status.className = "status ok";
    toast("路径审计完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderAuditErrors(errors);
      status.textContent =
        `存在 ${errors.length} 个数据错误，未生成审计结果（矩阵与账单均未改动）`;
    } else {
      status.textContent = summaryError(e, "路径审计请求失败");
    }
    status.className = "status bad";
    toast("无法执行路径审计", false);
  }
}

function copyAllSuggests() {
  if (!lastSuggestLines.length) return;
  copyText(lastSuggestLines.join("\n"),
    `已复制 ${lastSuggestLines.length} 行建议补录行（可粘贴到「4. 票价矩阵」，核对后保存）`);
}

/* ---------------- 9. 行程耗时异常审计（只读） ---------------- */

function renderDurationErrors(errors) {
  const ul = $("duration-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("duration-errors").hidden = false;
  $("duration-result").hidden = true;
}

function fmtMin(v) {
  return Number(v).toFixed(2);
}

function renderDurationAudit(data) {
  $("duration-errors").hidden = true;
  $("duration-result").hidden = false;

  const anomalies = data.anomalies || [];
  const insufficient = data.insufficient || [];
  const longCount = anomalies.filter((a) => a.kind === "long").length;
  const shortCount = anomalies.length - longCount;

  const insNote = insufficient.length
    ? `另有 <strong>${insufficient.length}</strong> 个站点对样本不足` +
      `（&lt; ${data.min_samples} 程），未参与判定。`
    : "";
  const summary = $("duration-summary");
  if (!anomalies.length) {
    summary.innerHTML =
      `✅ 共统计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个有向站点对，其中 ` +
      `<strong>${data.audited_pairs}</strong> 个站点对样本数达标` +
      `（≥ ${data.min_samples} 程）：未发现耗时异常。${insNote}<br>` +
      `<span class="sub">审计全程只读：未修改记录、配置或账单。</span>`;
  } else {
    summary.innerHTML =
      `共统计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个有向站点对，其中 ` +
      `<strong>${data.audited_pairs}</strong> 个站点对样本数达标` +
      `（≥ ${data.min_samples} 程）：发现耗时异常 ` +
      `<strong>${anomalies.length}</strong> 笔` +
      `（过长 ${longCount} 笔 / 过短 ${shortCount} 笔）。${insNote}<br>` +
      `<span class="sub">阈值 = max(10 分钟, 3 × 1.4826 × MAD)，` +
      `基线已排除行程自身；审计全程只读：未修改记录、配置或账单。</span>`;
  }

  // 耗时异常行程
  const aBlock = $("duration-anomalies-block");
  aBlock.hidden = anomalies.length === 0;
  const atb = $("duration-anomalies-table").querySelector("tbody");
  atb.innerHTML = "";
  for (const a of anomalies) {
    const isLong = a.kind === "long";
    const devText = (a.deviation_min > 0 ? "+" : "") + fmtMin(a.deviation_min);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="tag ${isLong ? "tag-long" : "tag-short"}">${isLong ? "过长" : "过短"}</span></td>
      <td>${escapeHtml(a.card)}</td>
      <td class="num">${a.seq}</td>
      <td>${escapeHtml(a.entry_station)} → ${escapeHtml(a.exit_station)}</td>
      <td>${a.entry_time}</td>
      <td>${a.exit_time}</td>
      <td class="num">${fmtMin(a.duration_min)}</td>
      <td class="num">${a.baseline_count}</td>
      <td class="num">${fmtMin(a.median_min)}</td>
      <td class="num">${fmtMin(a.mad_min)}</td>
      <td class="num">${fmtMin(a.threshold_min)}</td>
      <td class="num ${isLong ? "delta-up" : "delta-down"}">${devText}</td>
      <td class="num">进 ${a.entry_line} / 出 ${a.exit_line}</td>`;
    atb.appendChild(tr);
  }

  // 样本不足的站点对
  const iBlock = $("duration-insufficient-block");
  iBlock.hidden = insufficient.length === 0;
  const itb = $("duration-insufficient-table").querySelector("tbody");
  itb.innerHTML = "";
  for (const p of insufficient) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p.entry_station)} → ${escapeHtml(p.exit_station)}</td>
      <td class="num">${p.trip_count}</td>
      <td>需 ≥ ${p.required} 程才参与判定，当前 ${p.trip_count} 程</td>`;
    itb.appendChild(tr);
  }
}

async function runDurationAudit() {
  const status = $("duration-status");
  status.textContent = "审计中…";
  status.className = "status";
  $("duration-errors").hidden = true;
  $("duration-result").hidden = true;
  try {
    const data = await api("/api/duration-audit", { method: "POST" });
    renderDurationAudit(data);
    if (!data.anomaly_count) {
      status.textContent =
        `审计完成：${data.audited_pairs} 个达标站点对均无耗时异常（只读，未改动数据）`;
    } else {
      status.textContent =
        `审计完成：标记 ${data.anomaly_count} 笔耗时异常` +
        "（只读，记录 / 配置 / 账单均未改动）";
    }
    status.className = "status ok";
    toast("耗时审计完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderDurationErrors(errors);
      status.textContent =
        `存在 ${errors.length} 个配对 / 时间类错误，未生成审计结果（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "耗时审计请求失败");
    }
    status.className = "status bad";
    toast("无法执行耗时审计", false);
  }
}

/* ---------------- 10. 站点闸机峰值审计（只读） ---------------- */

function renderPeakErrors(errors) {
  const ul = $("peak-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("peak-errors").hidden = false;
  $("peak-result").hidden = true;
}

function peakLineChips(lines) {
  return lines.map((ln) =>
    `<button type="button" class="line-chip" data-line="${ln}">第 ${ln} 行</button>`
  ).join(" ");
}

function renderPeakAudit(data) {
  $("peak-errors").hidden = true;
  $("peak-result").hidden = false;

  const groups = data.groups || [];
  const summary = $("peak-summary");
  if (!groups.length) {
    summary.innerHTML =
      `已保存记录数 <strong>${data.record_count}</strong>，没有可统计的站点 / 方向分组。<br>` +
      `<span class="sub">审计全程只读：未修改记录、配置或账单。</span>`;
  } else {
    const alertNote = data.alert_count
      ? `其中 <strong>${data.alert_count}</strong> 组达到最低告警数 ` +
        `（≥ ${data.min_alert} 笔），已用 🚨 标记。`
      : `没有组达到最低告警数（峰值 ≥ ${data.min_alert} 笔才标记）。`;
    summary.innerHTML =
      `统计窗口 <strong>${data.window_minutes}</strong> 分钟 · ` +
      `已保存刷卡 <strong>${data.record_count}</strong> 条 · ` +
      `<strong>${data.group_count}</strong> 个「站点 × 方向」组，` +
      `${alertNote}<br>` +
      `<span class="sub">候选窗口只从实际刷卡时刻起算，采用半开区间 [开始, 结束)；` +
      `并列时按更早开始时刻、站点名排序。审计全程只读：未修改记录、配置或账单。` +
      `点击行号可定位到「5. 刷卡记录」中的原始行。</span>`;
  }

  const tb = $("peak-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const g of groups) {
    const tr = document.createElement("tr");
    if (g.alert) tr.classList.add("peak-alert-row");
    tr.innerHTML = `
      <td>${g.alert ? '<span class="tag tag-alert">🚨 告警</span>' : "—"}</td>
      <td>${escapeHtml(g.station)}</td>
      <td><span class="tag ${g.action === "IN" ? "tag-in" : "tag-out"}">` +
        `${g.action === "IN" ? "进 ▲" : "出 ▼"}</span></td>
      <td class="num charge">${g.peak_count}</td>
      <td class="num">${g.card_count}</td>
      <td>${g.start_time}</td>
      <td>${g.end_time}</td>
      <td class="num">${g.total_count}</td>
      <td class="line-chip-cell">${peakLineChips(g.lines)}</td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll(".line-chip").forEach((chip) => {
    chip.addEventListener("click", () =>
      locateRecordLine(Number(chip.dataset.line)));
  });
}

async function locateRecordLine(line) {
  // 以已保存记录为准（忽略文本框中未保存的改动），在「5. 刷卡记录」
  // 结果表中按原始行号 src_line 定位对应行并高亮。
  const tb = $("records-table").querySelector("tbody");
  let row = tb.querySelector(`tr[data-src-line="${line}"]`);
  if (!row) {
    // 页面表格可能不是最新的已保存数据，重新拉取后再定位
    try {
      const fresh = await api("/api/records");
      renderRecords(fresh.items);
      row = tb.querySelector(`tr[data-src-line="${line}"]`);
    } catch (e) {
      toast(summaryError(e, "定位原始行失败"), false);
      return;
    }
  }
  if (!row) {
    toast(`已保存记录中找不到第 ${line} 行（记录可能已被重新保存）`, false);
    return;
  }
  tb.querySelectorAll("tr.loc-hit").forEach((tr) =>
    tr.classList.remove("loc-hit"));
  row.classList.add("loc-hit");
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  toast(`已定位到已保存刷卡记录的第 ${line} 行（只读定位，未修改数据）`);
}

async function runPeakAudit() {
  const status = $("peak-status");
  const body = {
    window_minutes: Number($("peak-window").value),
    min_alert: Number($("peak-min-alert").value),
  };
  if (!Number.isFinite(body.window_minutes) || !Number.isFinite(body.min_alert)) {
    status.textContent = "统计窗口与最低告警数必须填写为有效数字";
    status.className = "status bad";
    return;
  }
  status.textContent = "审计中…";
  status.className = "status";
  $("peak-errors").hidden = true;
  $("peak-result").hidden = true;
  try {
    const data = await api("/api/station-peak-audit", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderPeakAudit(data);
    status.textContent =
      `审计完成：${data.group_count} 个站点 / 方向组，${data.alert_count} 组达标` +
      "（只读，记录 / 配置 / 账单均未改动）";
    status.className = "status ok";
    toast("峰值审计完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderPeakErrors(errors);
      status.textContent =
        `参数不合法，未生成审计结果（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "峰值审计请求失败");
    }
    status.className = "status bad";
    toast("无法执行峰值审计", false);
  }
}

/* ---------------- 11. 外部刷卡批次合并预览（只读） ---------------- */

let lastMergedText = "";

function renderMergeErrors(errors) {
  const ul = $("merge-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("merge-errors").hidden = false;
  $("merge-result").hidden = true;
}

function mergeDeltaText(sec) {
  const sign = sec > 0 ? "+" : sec < 0 ? "−" : "±";
  return `${sign}${Math.abs(sec)} 秒`;
}

function renderMergeRecordRow(r, lineKey) {
  return `<td>${escapeHtml(r.card)}</td><td>${r.time}</td>` +
    `<td>${escapeHtml(r.station)}</td><td>${actionText(r.action)}</td>` +
    `<td class="num">第 ${r[lineKey]} 行</td>`;
}

function renderMergePreview(data) {
  $("merge-errors").hidden = true;
  $("merge-result").hidden = false;

  const pairs = data.pairs || [];
  const baseOnly = data.base_only || [];
  const importOnly = data.import_only || [];
  lastMergedText = data.merged_text || "";

  $("merge-summary").innerHTML =
    `容差 <strong>${data.tolerance_seconds}</strong> 秒 · ` +
    `基准 <strong>${data.base_count}</strong> 条 · 待导入 <strong>${data.import_count}</strong> 条：` +
    `重复配对 <strong>${data.pair_count}</strong> 对` +
    `（总时间差 <strong>${data.total_abs_diff_seconds}</strong> 秒），` +
    `仅基准 <strong>${baseOnly.length}</strong> 条，仅导入 <strong>${importOnly.length}</strong> 条，` +
    `合并后共 <strong>${data.merged_count}</strong> 条。<br>` +
    `<span class="sub">匹配先最大化配对数，再最小化总时间差，同分时按基准行号、导入行号稳定破同分；` +
    `预览全程只读：待导入记录未保存，已保存记录与账单均未改动。</span>`;

  // 重复配对
  const pBlock = $("merge-pairs-block");
  pBlock.hidden = pairs.length === 0;
  const ptb = $("merge-pairs-table").querySelector("tbody");
  ptb.innerHTML = "";
  for (const p of pairs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p.card)}</td>
      <td>${escapeHtml(p.station)}</td>
      <td>${actionText(p.action)}</td>
      <td>${p.base_time}<span class="sub">（第 ${p.base.line} 行）</span></td>
      <td>${p.import_time}<span class="sub">（第 ${p.import.line} 行）</span></td>
      <td class="num ${p.abs_diff_seconds === 0 ? "delta-zero" : ""}">${p.abs_diff_seconds}</td>
      <td class="num ${p.delta_seconds > 0 ? "delta-up" : p.delta_seconds < 0 ? "delta-down" : "delta-zero"}">${mergeDeltaText(p.delta_seconds)}</td>`;
    ptb.appendChild(tr);
  }

  // 仅基准
  const bBlock = $("merge-base-only-block");
  bBlock.hidden = baseOnly.length === 0;
  const btb = $("merge-base-only-table").querySelector("tbody");
  btb.innerHTML = "";
  for (const r of baseOnly) {
    const tr = document.createElement("tr");
    tr.innerHTML = renderMergeRecordRow(r, "line");
    btb.appendChild(tr);
  }

  // 仅导入
  const iBlock = $("merge-import-only-block");
  iBlock.hidden = importOnly.length === 0;
  const itb = $("merge-import-only-table").querySelector("tbody");
  itb.innerHTML = "";
  for (const r of importOnly) {
    const tr = document.createElement("tr");
    tr.innerHTML = renderMergeRecordRow(r, "line");
    itb.appendChild(tr);
  }

  $("merge-merged-text").textContent = lastMergedText || "（合并后无记录）";
}

async function runMergePreview() {
  const status = $("merge-status");
  const tolerance = Number($("merge-tolerance").value);
  if (!Number.isFinite(tolerance)) {
    status.textContent = "时间容差必须填写为 0～120 的整数（秒）";
    status.className = "status bad";
    return;
  }
  status.textContent = "合并匹配中…";
  status.className = "status";
  $("merge-errors").hidden = true;
  $("merge-result").hidden = true;
  try {
    const data = await api("/api/external-merge-preview", {
      method: "POST",
      body: JSON.stringify({
        text: $("merge-text").value,
        tolerance_seconds: tolerance,
      }),
    });
    renderMergePreview(data);
    status.textContent =
      `预览完成：${data.pair_count} 对重复，仅基准 ${data.base_only_count} 条，` +
      `仅导入 ${data.import_only_count} 条（只读，记录与账单均未改动）`;
    status.className = "status ok";
    toast("合并预览已生成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderMergeErrors(errors);
      status.textContent =
        errors[0] && errors[0].code === "CONFIG_BAD"
          ? "容差参数不合法，未生成预览"
          : `待导入记录存在 ${errors.length} 个解析错误，未生成预览（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "合并预览请求失败");
    }
    status.className = "status bad";
    toast("无法生成合并预览", false);
  }
}

function copyMergedText() {
  if (!lastMergedText) {
    toast("暂无可复制的合并文本", false);
    return;
  }
  copyText(lastMergedText,
    `已复制 ${lastMergedText.split("\n").length} 行合并文本（仍未保存，可粘贴到记录区核对）`);
}

/* ---------------- 12. 匿名客流 OD 发布（只读） ---------------- */

let lastOdRows = [];
let lastOdMeta = null;

function renderOdErrors(errors) {
  const ul = $("od-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("od-errors").hidden = false;
  $("od-result").hidden = true;
  $("btn-od-csv").hidden = true;
}

function renderOdPublish(data) {
  $("od-errors").hidden = true;
  $("od-result").hidden = false;
  $("btn-od-csv").hidden = data.row_count === 0;
  lastOdRows = data.rows || [];
  lastOdMeta = data;

  const suppressNote = data.suppressed_bucket_count
    ? `因合并后仍不足 k 且已无相邻非空桶，共抑制 <strong>${data.suppressed_bucket_count}</strong> ` +
      `个初始非空桶（涉及 <strong>${data.suppressed_trip_count}</strong> 笔行程，` +
      `仅公布数量，不含任何明细）。`
    : "没有区间被抑制。";
  $("od-summary").innerHTML =
    `运营日 <strong>${data.start_day} ～ ${data.end_day}</strong>（分界 ${data.day_split_hour}:00）· ` +
    `初始桶 <strong>${data.bucket_minutes}</strong> 分钟 · 匿名阈值 ` +
    `<strong>k=${data.k}</strong>：范围内共 <strong>${data.trip_count}</strong> 笔行程 / ` +
    `<strong>${data.od_count}</strong> 个 OD / <strong>${data.day_count}</strong> 个运营日，` +
    `发布 <strong>${data.row_count}</strong> 个匿名区间。<br>` +
    `${suppressNote}<br>` +
    `<span class="sub">每个区间去重卡数 ≥ k；仅输出区间、行程数与去重卡数，` +
    `不包含卡号、原始行号或单条时刻；发布全程只读，记录 / 配置 / 账单均未改动。</span>`;

  const tb = $("od-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const r of lastOdRows) {
    const tr = document.createElement("tr");
    const merged = r.merged_buckets > 1
      ? `<span class="od-merged-tag">${r.merged_buckets} 桶合并</span>`
      : "1";
    tr.innerHTML = `
      <td>${r.operating_day}</td>
      <td>${escapeHtml(r.entry_station)}</td>
      <td>${escapeHtml(r.exit_station)}</td>
      <td>${r.start_time}</td>
      <td>${r.end_time}</td>
      <td class="num">${r.span_minutes}</td>
      <td class="num">${merged}</td>
      <td class="num charge">${r.trip_count}</td>
      <td class="num">${r.card_count}</td>`;
    tb.appendChild(tr);
  }
  if (!lastOdRows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td colspan="9" class="od-empty">所选运营日范围内没有可发布的行程（或全部区间已被抑制）。</td>`;
    tb.appendChild(tr);
  }
}

async function runOdPublish() {
  const status = $("od-status");
  const body = {
    start_day: $("od-start").value,
    end_day: $("od-end").value,
    bucket_minutes: Number($("od-bucket").value),
    k: Number($("od-k").value),
  };
  if (!body.start_day || !body.end_day) {
    status.textContent = "请先选择运营日起止日期";
    status.className = "status bad";
    return;
  }
  if (!Number.isFinite(body.k)) {
    status.textContent = "匿名阈值 k 必须填写为 2～50 的整数";
    status.className = "status bad";
    return;
  }
  status.textContent = "聚合发布中…";
  status.className = "status";
  $("od-errors").hidden = true;
  $("od-result").hidden = true;
  $("btn-od-csv").hidden = true;
  try {
    const data = await api("/api/od-flow-publish", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderOdPublish(data);
    status.textContent =
      `发布完成：${data.row_count} 个匿名区间，抑制 ${data.suppressed_bucket_count} 桶` +
      "（只读，记录 / 配置 / 账单均未改动）";
    status.className = "status ok";
    toast("匿名 OD 发布已生成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderOdErrors(errors);
      status.textContent =
        errors[0] && errors[0].code === "CONFIG_BAD"
          ? "参数不合法，未生成发布结果"
          : `存在 ${errors.length} 个配对 / 时间类错误，未生成发布结果（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "OD 发布请求失败");
    }
    status.className = "status bad";
    toast("无法生成匿名 OD 发布", false);
  }
}

function odCsvCell(v) {
  let s = String(v);
  // 防止表格软件把以 = + - @ 开头的值解析为公式（CSV 注入）
  if (/^[=+\-@]/.test(s)) s = "\t" + s;
  if (/[",\n\r]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function buildOdCsv(data) {
  const header = [
    "运营日", "进站站点", "出站站点", "区间开始", "区间结束(不含)",
    "区间跨度(分)", "初始桶(分)", "合并桶数", "行程数", "去重卡数",
  ];
  const lines = [header.map(odCsvCell).join(",")];
  for (const r of data.rows || []) {
    lines.push([
      r.operating_day, r.entry_station, r.exit_station,
      r.start_time, r.end_time, r.span_minutes, r.bucket_minutes,
      r.merged_buckets, r.trip_count, r.card_count,
    ].map(odCsvCell).join(","));
  }
  // 报表头行只汇总抑制数量，不含任何卡号 / 行号 / 单条时刻明细
  lines.push("");
  lines.push(`# 运营日范围,${odCsvCell(`${data.start_day} ～ ${data.end_day}`)}`);
  lines.push(`# 初始桶(分钟),${data.bucket_minutes}`);
  lines.push(`# 匿名阈值k,${data.k}`);
  lines.push(`# 运营日分界(点),${data.day_split_hour}`);
  lines.push(`# 范围内行程数,${data.trip_count}`);
  lines.push(`# 发布区间数,${data.row_count}`);
  lines.push(`# 被抑制初始非空桶数(仅数量),${data.suppressed_bucket_count}`);
  lines.push(`# 被抑制行程数(仅数量),${data.suppressed_trip_count}`);
  return lines.join("\r\n");
}

function exportOdCsv() {
  if (!lastOdMeta || !lastOdRows.length) {
    toast("暂无可导出的发布结果", false);
    return;
  }
  const csv = buildOdCsv(lastOdMeta);
  // UTF-8 BOM，便于 Excel 直接识别中文编码
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `od_flow_${lastOdMeta.start_day}_${lastOdMeta.end_day}_` +
    `${lastOdMeta.bucket_minutes}min_k${lastOdMeta.k}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`已导出 ${lastOdRows.length} 个发布区间（CSV 仅含聚合数据）`);
}

/* ---------------- 13. 闸机容量排队推演（只读） ---------------- */

let lastGqData = null;

function renderGqErrors(errors) {
  const ul = $("gq-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("gq-errors").hidden = false;
  $("gq-result").hidden = true;
  $("btn-gq-csv").hidden = true;
}

function gqGroupHeadHtml(g) {
  const dirTag = g.action === "IN" ? "tag-in" : "tag-out";
  const dirText = g.action === "IN" ? "进 ▲" : "出 ▼";
  let statusTag;
  if (!g.cleared) {
    statusTag = `<span class="tag tag-alert">⚠ 未清空（剩 ${g.remaining_backlog}）</span>`;
  } else if (g.first_overload_time) {
    statusTag = `<span class="tag tag-ok">✅ 已清空</span>`;
  } else {
    statusTag = `<span class="tag tag-ok">✅ 全程无积压</span>`;
  }
  const firstOver = g.first_overload_time
    ? g.first_overload_time
    : "无（每桶需求均 ≤ 容量）";
  const clearAt = !g.cleared
    ? `未清空（续算 ${g.extra_buckets_used} 桶后仍剩 <strong>${g.remaining_backlog}</strong>）`
    : (g.clear_time
      ? `<strong>${g.clear_time}</strong>（积压最后一次清空的桶结束时刻）`
      : "—（全程未产生积压）");
  const peakAt = g.peak_backlog > 0
    ? `<strong>${g.peak_backlog}</strong> 人次 <span class="sub">（首次出现于 ${g.peak_time} 起的桶）</span>`
    : "0 人次";
  return `<h3>${statusTag} ` +
    `<span class="gq-station">${escapeHtml(g.station)}</span> ` +
    `<span class="tag ${dirTag}">${dirText}</span></h3>` +
    `<div class="gq-group-meta">` +
    `累计到达 <strong>${g.total_arrivals}</strong> 人次 · 累计处理 <strong>${g.processed_total}</strong> 人次 · ` +
    `峰值期末积压 ${peakAt} · 首次超载：${firstOver} · 清空时刻：${clearAt}` +
    `</div>`;
}

function renderGateQueue(data) {
  $("gq-errors").hidden = true;
  $("gq-result").hidden = false;
  $("btn-gq-csv").hidden = data.group_count === 0;
  lastGqData = data;

  const groups = data.groups || [];
  const overGroups = groups.filter((g) => g.first_overload_time).length;
  if (!groups.length) {
    $("gq-summary").innerHTML =
      `运营日 <strong>${data.operating_day}</strong>` +
      `（窗口 <strong>${data.day_start}</strong> ～ <strong>${data.day_end}</strong>，` +
      `分界 ${data.day_split_hour}:00）内没有任何已保存刷卡记录。<br>` +
      `<span class="sub">推演全程只读：未修改记录、配置或账单。</span>`;
  } else {
    $("gq-summary").innerHTML =
      `运营日 <strong>${data.operating_day}</strong>` +
      `（窗口 <strong>${data.day_start}</strong> ～ <strong>${data.day_end}</strong>，` +
      `分界 ${data.day_split_hour}:00）· 桶 <strong>${data.bucket_minutes}</strong> 分钟 · ` +
      `每桶通行能力 <strong>${data.capacity}</strong> 人次 · ` +
      `最多续算 <strong>${data.max_extra_buckets}</strong> 个空桶：` +
      `窗口内刷卡 <strong>${data.record_count}</strong> 条，` +
      `<strong>${data.group_count}</strong> 个「站点 × 方向」组，` +
      `其中 <strong>${overGroups}</strong> 组出现超载，` +
      `<strong>${data.uncleared_count}</strong> 组续算后<strong>仍未清空</strong>。<br>` +
      `<span class="sub">桶从运营日分界时刻对齐，半开区间 [开始, 结束)；` +
      `处理量 = min(期初积压 + 到达量, 容量)，未处理量结转下一桶；` +
      `推演全程只读，记录 / 配置 / 账单均未改动。</span>`;
  }

  const box = $("gq-groups");
  box.innerHTML = "";
  for (const g of groups) {
    const div = document.createElement("div");
    div.className = "gq-group" + (g.cleared ? "" : " gq-group-uncleared");
    div.innerHTML = gqGroupHeadHtml(g);

    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    const table = document.createElement("table");
    table.className = "gq-table";
    table.innerHTML = `
      <thead>
        <tr>
          <th>桶序号</th><th>桶开始</th><th>桶结束（不含）</th><th>类型</th>
          <th>到达量</th><th>期初积压</th><th>处理量</th><th>期末积压</th><th>超载</th>
        </tr>
      </thead>
      <tbody></tbody>`;
    const tb = table.querySelector("tbody");
    for (const b of g.buckets) {
      const tr = document.createElement("tr");
      if (b.continuation) tr.classList.add("gq-cont");
      if (b.overloaded) tr.classList.add("gq-over");
      if (g.peak_backlog > 0 && b.end_backlog === g.peak_backlog
          && b.start_time === g.peak_time) {
        tr.classList.add("gq-peak");
      }
      const endCell = b.end_backlog > 0
        ? `<td class="num gq-backlog">${b.end_backlog}</td>`
        : `<td class="num">0</td>`;
      tr.innerHTML = `
        <td class="num">${b.bucket_index}</td>
        <td>${b.start_time}</td>
        <td>${b.end_time}</td>
        <td>${b.continuation ? '<span class="gq-cont-tag">续算空桶</span>' : "运营日桶"}</td>
        <td class="num">${b.arrival_count}</td>
        <td class="num">${b.begin_backlog}</td>
        <td class="num">${b.processed_count}</td>
        ${endCell}
        <td>${b.overloaded ? '<span class="tag tag-alert">超载</span>' : "—"}</td>`;
      tb.appendChild(tr);
    }
    wrap.appendChild(table);
    div.appendChild(wrap);
    box.appendChild(div);
  }
}

async function runGateQueue() {
  const status = $("gq-status");
  const body = {
    operating_day: $("gq-day").value,
    bucket_minutes: Number($("gq-bucket").value),
    capacity: Number($("gq-capacity").value),
    max_extra_buckets: Number($("gq-extra").value),
  };
  if (!body.operating_day) {
    status.textContent = "请先选择运营日";
    status.className = "status bad";
    return;
  }
  if (!Number.isFinite(body.capacity) || !Number.isFinite(body.max_extra_buckets)) {
    status.textContent = "每桶通行能力与最多续算桶数必须填写为有效整数";
    status.className = "status bad";
    return;
  }
  status.textContent = "排队推演中…";
  status.className = "status";
  $("gq-errors").hidden = true;
  $("gq-result").hidden = true;
  $("btn-gq-csv").hidden = true;
  try {
    const data = await api("/api/gate-queue-sim", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderGateQueue(data);
    status.textContent =
      `推演完成：${data.group_count} 个站点 / 方向组，${data.uncleared_count} 组续算后未清空` +
      "（只读，记录 / 配置 / 账单均未改动）";
    status.className = "status ok";
    toast("闸机排队推演完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderGqErrors(errors);
      status.textContent = "参数不合法，未生成推演结果（数据均未改动）";
    } else {
      status.textContent = summaryError(e, "排队推演请求失败");
    }
    status.className = "status bad";
    toast("无法执行排队推演", false);
  }
}

function buildGqCsv(data) {
  const header = [
    "运营日", "站点", "方向", "桶序号", "桶开始", "桶结束(不含)", "桶类型",
    "到达量", "期初积压", "处理量", "期末积压", "是否超载",
  ];
  const lines = [header.map(odCsvCell).join(",")];
  for (const g of data.groups || []) {
    for (const b of g.buckets || []) {
      lines.push([
        data.operating_day, g.station,
        g.action === "IN" ? "进" : "出",
        b.bucket_index, b.start_time, b.end_time,
        b.continuation ? "续算空桶" : "运营日桶",
        b.arrival_count, b.begin_backlog, b.processed_count,
        b.end_backlog, b.overloaded ? "是" : "否",
      ].map(odCsvCell).join(","));
    }
  }
  // 汇总行：参数与每组峰值 / 首次超载 / 清空时刻（仅聚合，不含卡号 / 行号）
  lines.push("");
  lines.push(`# 运营日,${odCsvCell(data.operating_day)}`);
  lines.push(`# 运营日窗口,${odCsvCell(`${data.day_start} ～ ${data.day_end}`)}`);
  lines.push(`# 运营日分界(点),${data.day_split_hour}`);
  lines.push(`# 时间桶(分钟),${data.bucket_minutes}`);
  lines.push(`# 每桶通行能力,${data.capacity}`);
  lines.push(`# 最多续算空桶数,${data.max_extra_buckets}`);
  lines.push(`# 窗口内刷卡数,${data.record_count}`);
  lines.push(`# 站点方向组数,${data.group_count}`);
  lines.push(`# 续算后未清空组数,${data.uncleared_count}`);
  for (const g of data.groups || []) {
    lines.push([
      "# 组汇总", g.station, g.action === "IN" ? "进" : "出",
      `累计到达=${g.total_arrivals}`, `累计处理=${g.processed_total}`,
      `峰值积压=${g.peak_backlog}`, `峰值时刻=${g.peak_time || ""}`,
      `首次超载=${g.first_overload_time || ""}`,
      g.cleared
        ? `清空时刻=${g.clear_time || ""}`
        : `未清空剩余=${g.remaining_backlog}`,
      `续算桶数=${g.extra_buckets_used}`,
    ].map(odCsvCell).join(","));
  }
  return lines.join("\r\n");
}

function exportGqCsv() {
  if (!lastGqData || !lastGqData.group_count) {
    toast("暂无可导出的推演结果", false);
    return;
  }
  const csv = buildGqCsv(lastGqData);
  // UTF-8 BOM，便于 Excel 直接识别中文编码
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `gate_queue_${lastGqData.operating_day}_` +
    `${lastGqData.bucket_minutes}min_cap${lastGqData.capacity}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("已导出逐桶排队推演结果（CSV，UTF-8 BOM）");
}

/* ---------------- 14. 通票购买策略回测（只读） ---------------- */

function renderPassErrors(errors) {
  const ul = $("pass-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("pass-errors").hidden = false;
  $("pass-result").hidden = true;
}

function renderPassBacktest(data) {
  $("pass-errors").hidden = true;
  $("pass-result").hidden = false;

  const summary = $("pass-summary");
  if (!data.card_count) {
    summary.innerHTML =
      `运营日 <strong>${data.start_day} ～ ${data.end_day}</strong>` +
      `（${data.day_count} 天，账单核验时间 ${data.last_verified_at || "未知"}）` +
      `内没有任何行程账单，请调整运营日范围。<br>` +
      `<span class="sub">回测全程只读：未修改记录、配置或账单。</span>`;
  } else {
    summary.innerHTML =
      `运营日 <strong>${data.start_day} ～ ${data.end_day}</strong>` +
      `（${data.day_count} 天）· 账单核验时间 <strong>${data.last_verified_at}</strong> · ` +
      `范围内 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.card_count}</strong> 张卡：` +
      `基准支出 <strong>${yuan(data.baseline_total)}</strong> 元 → ` +
      `通票方案支出 <strong>${yuan(data.pass_total)}</strong> 元` +
      `（通票费 ${yuan(data.pass_cost_total)} ＋ 未覆盖按次 ` +
      `${yuan(data.uncovered_total)}），` +
      `<span class="delta-down">节省 ${yuan(data.saving_total)} 元</span>。<br>` +
      `<span class="sub">通票自购买运营日起连续覆盖其有效运营日内全部行程，不因中间无行程暂停；` +
      `同价时依次按通票更少、首次购买日更早、名称字典序更小破同分；` +
      `回测全程只读：未修改记录、配置或账单。</span>`;
  }

  // 按卡汇总
  const tb = $("pass-cards-table").querySelector("tbody");
  tb.innerHTML = "";
  const cards = data.cards || [];
  for (const c of cards) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(c.card)}</td>
      <td class="num">${c.trip_count}</td>
      <td class="num">${yuan(c.baseline_total)}</td>
      <td class="num">${yuan(c.pass_cost)}</td>
      <td class="num">${yuan(c.uncovered_total)}</td>
      <td class="num charge">${yuan(c.pass_total)}</td>
      <td class="num ${c.saving > 0 ? "delta-down" : "delta-zero"}">${yuan(c.saving)}</td>
      <td class="num">${c.purchase_count}</td>`;
    tb.appendChild(tr);
  }
  if (!cards.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8" class="od-empty">所选运营日范围内没有行程账单。</td>`;
    tb.appendChild(tr);
  }

  // 逐次购买明细（全部卡合并，按卡号 + 购买次序）
  const rows = [];
  for (const c of cards) {
    (c.purchases || []).forEach((p, idx) => {
      rows.push(Object.assign({ card: c.card, idx: idx + 1 }, p));
    });
  }
  const pBlock = $("pass-purchases-block");
  pBlock.hidden = rows.length === 0;
  const ptb = $("pass-purchases-table").querySelector("tbody");
  ptb.innerHTML = "";
  for (const p of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p.card)}</td>
      <td class="num">${p.idx}</td>
      <td>${p.day}</td>
      <td>${escapeHtml(p.name)}</td>
      <td class="num">${yuan(p.price)}</td>
      <td class="num">${p.valid_days}</td>
      <td>${p.cover_start} ～ ${p.cover_end}` +
      `<span class="sub">（${p.covered_days} 天）</span></td>
      <td class="num">${p.covered_trip_count}</td>
      <td class="num">${yuan(p.covered_baseline)}</td>`;
    ptb.appendChild(tr);
  }
}

async function runPassBacktest() {
  const status = $("pass-status");
  const body = {
    passes_text: $("pass-text").value,
    start_day: $("pass-start").value,
    end_day: $("pass-end").value,
  };
  if (!body.start_day || !body.end_day) {
    status.textContent = "请先选择运营日起止日期";
    status.className = "status bad";
    return;
  }
  status.textContent = "回测中…";
  status.className = "status";
  $("pass-errors").hidden = true;
  $("pass-result").hidden = true;
  try {
    const data = await api("/api/pass-backtest", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderPassBacktest(data);
    status.textContent = data.card_count
      ? `回测完成：${data.card_count} 张卡，节省合计 ${yuan(data.saving_total)} 元` +
        "（只读，数据均未改动）"
      : "回测完成：所选运营日范围内没有行程账单（只读，数据均未改动）";
    status.className = "status ok";
    toast("通票回测完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderPassErrors(errors);
      status.textContent = errors[0].code === "NO_BILLS"
        ? "没有可用的核验账单，请先在「7. 核验结果」成功运行核验"
        : `存在 ${errors.length} 个日期 / 产品错误，未生成回测结果（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "通票回测请求失败");
    }
    status.className = "status bad";
    toast("无法执行通票回测", false);
  }
}

/* ---------------- 15. 账单争议更正预演（只读） ---------------- */

let lastDisputeText = "";

function renderDisputeErrors(errors) {
  const ul = $("dispute-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("dispute-errors").hidden = false;
  $("dispute-result").hidden = true;
  $("btn-dispute-copy").hidden = true;
}

const DISPUTE_STATUS = {
  added: ["dispute-added", '<span class="tag tag-in">新增</span>'],
  removed: ["dispute-removed", '<span class="tag tag-del">移除</span>'],
  changed: ["dispute-changed", '<span class="tag tag-flip">变化</span>'],
  same: ["", '<span class="tag tag-same">不变</span>'],
};

function disputeTripHtml(b) {
  if (!b) return '<span class="sub">—（无此行程）</span>';
  return `${escapeHtml(b.entry_station)} → ${escapeHtml(b.exit_station)}` +
    `<div class="sub">${b.entry_time} ～ ${b.exit_time}</div>`;
}

function renderDispute(data) {
  $("dispute-errors").hidden = true;
  $("dispute-result").hidden = false;
  $("btn-dispute-copy").hidden = false;

  const s = data.summary;
  const kindHtml = s.kind === "surcharge"
    ? `<span class="delta-up">应补收 ${yuan(s.delta)} 元</span>`
    : s.kind === "refund"
      ? `<span class="delta-down">应退款 ${yuan(-s.delta)} 元</span>`
      : '<span class="delta-zero">差额为 0，无需补收或退款</span>';
  const editLines = (data.edits || [])
    .map((e) => `<div>· ${escapeHtml(e.detail)}</div>`).join("");
  $("dispute-summary").innerHTML =
    `卡号 <strong>${escapeHtml(data.card)}</strong> · 当前账单核验时间 ` +
    `<strong>${escapeHtml(data.last_verified_at || "未知")}</strong> · 本次更正 ` +
    `<strong>${(data.edits || []).length}</strong> 笔（仅内存预演）：` +
    `<div class="dispute-edit-lines">${editLines}</div>` +
    `行程对比：新增 <strong>${s.added_count}</strong> · 移除 ` +
    `<strong>${s.removed_count}</strong> · 变化 <strong>${s.changed_count}</strong> · ` +
    `不变 <strong>${s.same_count}</strong>` +
    `（当前 ${s.trip_count_current} 笔 → 更正后 ${s.trip_count_corrected} 笔）。<br>` +
    `实扣合计 <strong>${yuan(s.current_total)}</strong> 元 → ` +
    `<strong>${yuan(s.corrected_total)}</strong> 元，` +
    `差额 <span class="${deltaClass(s.delta)}">${deltaText(s.delta)} 元</span> → ${kindHtml}；` +
    `优惠合计 ${yuan(s.current_discount_total)} → ${yuan(s.corrected_discount_total)} 元` +
    `（差额 ${deltaText(s.discount_delta)} 元）。<br>` +
    `<span class="sub">行程以「进站行号 → 出站行号」对标识；差额 = 更正后 − 当前，` +
    `正数为补收、负数为退款；预演全程只读，未写回记录、配置或账单。</span>`;

  const tb = $("dispute-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const t of data.trips || []) {
    const [rowCls, tag] = DISPUTE_STATUS[t.status] || DISPUTE_STATUS.same;
    const cur = t.current;
    const cor = t.corrected;
    const fields = t.changed_fields || [];
    const tripHl = (fields.includes("time") || fields.includes("station"))
      ? "cell-changed" : "";
    const dayHl = t.day_changed ? "cell-changed" : "";
    const discHl = fields.includes("discount") ? "cell-changed" : "";
    const chargeHl = fields.includes("charged_fare") ? "cell-changed" : "";
    const tr = document.createElement("tr");
    if (rowCls) tr.className = rowCls;
    tr.innerHTML = `
      <td class="num">行 ${t.entry_line} → 行 ${t.exit_line}</td>
      <td>${tag}</td>
      <td class="trip ${tripHl}">${disputeTripHtml(cur)}</td>
      <td class="trip ${tripHl}">${disputeTripHtml(cor)}</td>
      <td class="${dayHl}">${cur ? cur.operating_day : "—"}</td>
      <td class="${dayHl}">${cor ? cor.operating_day : "—"}</td>
      <td class="num discount ${discHl}">${cur ? "-" + yuan(cur.discount) : "—"}</td>
      <td class="num discount ${discHl}">${cor ? "-" + yuan(cor.discount) : "—"}</td>
      <td class="num ${chargeHl}">${cur ? yuan(cur.charged_fare) : "—"}</td>
      <td class="num charge ${chargeHl}">${cor ? yuan(cor.charged_fare) : "—"}</td>
      <td class="num ${deltaClass(t.charge_diff)}">${deltaText(t.charge_diff)}</td>`;
    tb.appendChild(tr);
  }
  lastDisputeText = buildDisputeText(data);
}

function buildDisputeText(data) {
  const s = data.summary;
  const label = { added: "新增", removed: "移除", changed: "变化", same: "不变" };
  const seg = (b) => b
    ? `${b.entry_station}→${b.exit_station} ${b.entry_time}～${b.exit_time}` +
      ` 实扣${yuan(b.charged_fare)}元 优惠${yuan(b.discount)}元 运营日${b.operating_day}`
    : "（无此行程）";
  const lines = [];
  lines.push("【账单争议更正预演摘要】（只读预演，未写回记录 / 配置 / 账单）");
  lines.push(`卡号：${data.card}`);
  lines.push(`当前账单核验时间：${data.last_verified_at || "未知"}`);
  lines.push("更正内容：");
  for (const e of data.edits || []) lines.push(`- ${e.detail}`);
  lines.push(`行程对比（以进出站原始行号对标识）：新增 ${s.added_count} 笔 / ` +
    `移除 ${s.removed_count} 笔 / 变化 ${s.changed_count} 笔 / 不变 ${s.same_count} 笔`);
  for (const t of data.trips || []) {
    if (t.status === "same") continue;
    lines.push(`- [${label[t.status]}] 行程 行${t.entry_line}→行${t.exit_line}：` +
      `当前 ${seg(t.current)} ｜ 更正后 ${seg(t.corrected)} ｜ ` +
      `实扣差额 ${deltaText(t.charge_diff)} 元`);
  }
  lines.push(`实扣合计：当前 ${yuan(s.current_total)} 元 → 更正后 ` +
    `${yuan(s.corrected_total)} 元，差额 ${deltaText(s.delta)} 元`);
  lines.push(`优惠合计：当前 ${yuan(s.current_discount_total)} 元 → 更正后 ` +
    `${yuan(s.corrected_discount_total)} 元，差额 ${deltaText(s.discount_delta)} 元`);
  if (s.kind === "surcharge") {
    lines.push(`结论：该卡应补收 ${yuan(s.delta)} 元`);
  } else if (s.kind === "refund") {
    lines.push(`结论：该卡应退款 ${yuan(-s.delta)} 元`);
  } else {
    lines.push("结论：差额为 0，无需补收或退款");
  }
  return lines.join("\n");
}

async function runDisputePreview() {
  const status = $("dispute-status");
  const edits = [];
  for (const i of [1, 2]) {
    const lineRaw = $(`dispute-line-${i}`).value.trim();
    const timeRaw = $(`dispute-time-${i}`).value.trim();
    const stationRaw = $(`dispute-station-${i}`).value.trim();
    if (!lineRaw && !timeRaw && !stationRaw) continue;  // 整行留空视为未使用
    if (!lineRaw) {
      status.textContent = `更正 ${i}：请填写原始行号（第 5 区「来源行」）`;
      status.className = "status bad";
      return;
    }
    const edit = { line: Number(lineRaw) };
    if (timeRaw) edit.time = timeRaw;
    if (stationRaw) edit.station = stationRaw;
    edits.push(edit);
  }
  if (!edits.length) {
    status.textContent = "请至少填写一笔更正：原始行号 + 新时刻或新站点";
    status.className = "status bad";
    return;
  }
  status.textContent = "预演中…";
  status.className = "status";
  $("dispute-errors").hidden = true;
  $("dispute-result").hidden = true;
  $("btn-dispute-copy").hidden = true;
  try {
    const data = await api("/api/dispute-preview", {
      method: "POST",
      body: JSON.stringify({ edits }),
    });
    renderDispute(data);
    const s = data.summary;
    status.textContent = s.kind === "surcharge"
      ? `预演完成：该卡应补收 ${yuan(s.delta)} 元（只读，未写回任何数据）`
      : s.kind === "refund"
        ? `预演完成：该卡应退款 ${yuan(-s.delta)} 元（只读，未写回任何数据）`
        : "预演完成：差额为 0，无需补收或退款（只读，未写回任何数据）";
    status.className = "status ok";
    toast("更正预演完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderDisputeErrors(errors);
      status.textContent = errors[0].code === "NO_BILLS"
        ? "没有可用的核验账单，请先在「7. 核验结果」成功运行核验"
        : `存在 ${errors.length} 个错误，未产出差额（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "更正预演请求失败");
    }
    status.className = "status bad";
    toast("无法生成更正预演", false);
  }
}

function copyDisputeSummary() {
  if (!lastDisputeText) {
    toast("暂无可复制的争议摘要", false);
    return;
  }
  copyText(lastDisputeText, "已复制争议摘要（预演为只读，未写回任何数据）");
}

/* ---------------- 16. 服务中断补偿预览（只读） ---------------- */

function renderCompErrors(errors) {
  const ul = $("comp-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("comp-errors").hidden = false;
  $("comp-result").hidden = true;
}

function compCapTag(c) {
  if (!c.capped) return '<span class="tag tag-same">未封顶</span>';
  return c.cap_reason === "cap"
    ? '<span class="tag tag-flip">已封顶（单卡上限）</span>'
    : '<span class="tag tag-flip">已封顶（实扣额）</span>';
}

function renderCompensation(data) {
  $("comp-errors").hidden = true;
  $("comp-result").hidden = false;

  const summary = $("comp-summary");
  if (!data.card_count) {
    summary.innerHTML =
      `站点 <strong>${escapeHtml(data.station)}</strong> · 区间 ` +
      `<strong>${data.start_time} ～ ${data.end_time}</strong>（闭区间）内` +
      `没有匹配的账单行程（可能尚未核验或该时段无进出站刷卡）。<br>` +
      `预算 <strong>${yuan(data.budget)}</strong> 元全额未分配 ` +
      `（未分配 <strong>${yuan(data.unallocated)}</strong> 元）。<br>` +
      `<span class="sub">预览全程只读：未修改记录、配置或账单。</span>`;
  } else {
    summary.innerHTML =
      `站点 <strong>${escapeHtml(data.station)}</strong> · 区间 ` +
      `<strong>${data.start_time} ～ ${data.end_time}</strong>（闭区间）· ` +
      `账单核验时间 <strong>${escapeHtml(data.last_verified_at || "未知")}</strong>：` +
      `匹配 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.card_count}</strong> 张卡，实扣合计 ` +
      `<strong>${yuan(data.charged_total)}</strong> 元；预算 ` +
      `<strong>${yuan(data.budget)}</strong> 元（单卡上限 ` +
      `${yuan(data.per_card_cap)} 元）经 <strong>${data.round_count}</strong> 轮分配，` +
      `补偿合计 <strong>${yuan(data.compensated_total)}</strong> 元，` +
      `未分配 <strong>${yuan(data.unallocated)}</strong> 元。<br>` +
      `<span class="sub">按各卡实扣额（整数分）为权重比例分配，每轮最大余数补齐整数分` +
      `（余数相同按卡号排序），超过 min(单卡上限, 实扣额) 的份额封顶并向未封顶卡重新分配；` +
      `预览全程只读：未修改记录、配置或账单。</span>`;
  }

  const tb = $("comp-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const c of data.cards || []) {
    const tr = document.createElement("tr");
    if (c.capped) tr.classList.add("comp-capped-row");
    tr.innerHTML = `
      <td>${escapeHtml(c.card)}</td>
      <td class="num">${c.trip_count}</td>
      <td class="num">${yuan(c.charged_total)}</td>
      <td class="num charge">${yuan(c.compensation)}</td>
      <td class="num">${yuan(c.uncompensated)}</td>
      <td class="num">${yuan(c.effective_cap)}</td>
      <td>${compCapTag(c)}</td>`;
    tb.appendChild(tr);
  }
  if (!(data.cards || []).length) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td colspan="7" class="od-empty">无匹配账单行程，补偿预算全额未分配。</td>`;
    tb.appendChild(tr);
  }
}

async function runCompensationPreview() {
  const status = $("comp-status");
  const body = {
    station: $("comp-station").value.trim(),
    start_time: $("comp-start").value.trim(),
    end_time: $("comp-end").value.trim(),
    budget: Number($("comp-budget").value),
    per_card_cap: Number($("comp-cap").value),
  };
  if (!body.station || !body.start_time || !body.end_time) {
    status.textContent = "请填写中断站点与起止时刻";
    status.className = "status bad";
    return;
  }
  if (!Number.isFinite(body.budget) || !Number.isFinite(body.per_card_cap)) {
    status.textContent = "补偿预算与单卡上限必须填写为有效数字（元）";
    status.className = "status bad";
    return;
  }
  status.textContent = "分配试算中…";
  status.className = "status";
  $("comp-errors").hidden = true;
  $("comp-result").hidden = true;
  try {
    const data = await api("/api/compensation-preview", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderCompensation(data);
    status.textContent = data.card_count
      ? `预览完成：${data.card_count} 张卡补偿合计 ${yuan(data.compensated_total)} 元，` +
        `未分配 ${yuan(data.unallocated)} 元（只读，数据均未改动）`
      : "预览完成：无匹配账单行程，预算全额未分配（只读，数据均未改动）";
    status.className = "status ok";
    toast("补偿预览已生成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderCompErrors(errors);
      status.textContent = `存在 ${errors.length} 个参数错误，未生成预览（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "补偿预览请求失败");
    }
    status.className = "status bad";
    toast("无法生成补偿预览", false);
  }
}

/* ---------------- 演示数据 ---------------- */

const DEMO = {
  config: { day_split_hour: 4, transfer_minutes: 90,
            transfer_discount: 1, daily_cap: 10 },
  stations: `科技园, 1
中心公园, 2
大学城, 3`,
  fares: `1, 1, 2.00
1, 2, 3.00
1, 3, 4.00
2, 1, 3.00
2, 2, 2.00
2, 3, 3.00
3, 1, 4.00
3, 2, 3.00
3, 3, 2.00`,
  records: `A001, 2026-09-18 08:00:00, 科技园, 进
A001, 2026-09-18 08:25:00, 中心公园, 出
A001, 2026-09-18 09:10:00, 中心公园, 进
A001, 2026-09-18 09:50:00, 大学城, 出
A001, 2026-09-18 17:30:00, 大学城, 进
A001, 2026-09-18 18:20:00, 科技园, 出
A001, 2026-09-18 19:30:00, 科技园, 进
A001, 2026-09-18 20:00:00, 中心公园, 出
B002, 2026-09-19 03:20:00, 科技园, 进
B002, 2026-09-19 03:45:00, 中心公园, 出
# —— 以下用于「10. 站点闸机峰值审计」演示跨整点早高峰（科技园进站）——
C003, 2026-09-18 07:56:00, 科技园, 进
D004, 2026-09-18 07:58:00, 科技园, 进
E005, 2026-09-18 08:02:00, 科技园, 进
C003, 2026-09-18 08:05:00, 大学城, 出
D004, 2026-09-18 08:09:00, 中心公园, 出
E005, 2026-09-18 08:12:00, 中心公园, 出
F006, 2026-09-18 08:04:00, 科技园, 进
F006, 2026-09-18 08:30:00, 中心公园, 出
G007, 2026-09-18 08:07:00, 科技园, 进
G007, 2026-09-18 08:40:00, 大学城, 出`,
  passes: `一日票, 6.00, 1
三日票, 15.00, 3`,
  pass_start: "2026-09-18",
  pass_end: "2026-09-18",
};

async function loadDemo() {
  if (!confirm("将用演示数据覆盖当前参数 / 站点 / 票价 / 记录，是否继续？")) return;
  $("cfg-split").value = DEMO.config.day_split_hour;
  $("cfg-minutes").value = DEMO.config.transfer_minutes;
  $("cfg-discount").value = DEMO.config.transfer_discount;
  $("cfg-cap").value = DEMO.config.daily_cap;
  $("stations-text").value = DEMO.stations;
  $("fares-text").value = DEMO.fares;
  $("records-text").value = DEMO.records;
  $("pass-text").value = DEMO.passes;
  $("pass-start").value = DEMO.pass_start;
  $("pass-end").value = DEMO.pass_end;
  // 顺带填入一笔争议更正示例：B002 出站（第 10 行）站点改为大学城，
  // 预演后该行程基础票价 3.00 → 4.00，该卡应补收 1.00 元
  $("dispute-line-1").value = "10";
  $("dispute-time-1").value = "";
  $("dispute-station-1").value = "大学城";
  // 顺带填入服务中断补偿示例：科技园 07:56～08:10 中断，预算 12 元、
  // 单卡上限 2 元 —— 命中 A001/C003/D004/E005/F006/G007 共 6 卡进站
  // （07:56 与 08:00 边界时刻均计入，闭区间）；第一轮 C003/G007 按比例
  // 应得 2.4 元被封顶 2 元，溢出 0.8 元第二轮平分给其余 4 卡，
  // 最终 6 卡各 2 元、预算用尽
  $("comp-station").value = "科技园";
  $("comp-start").value = "2026-09-18 07:56:00";
  $("comp-end").value = "2026-09-18 08:10:00";
  $("comp-budget").value = "12.00";
  $("comp-cap").value = "2.00";

  await saveConfig();
  await putText("/api/stations", DEMO.stations);
  await putText("/api/fares", DEMO.fares);
  await putText("/api/records", DEMO.records);

  renderStations((await api("/api/stations")).items);
  renderFares((await api("/api/fares")).items);
  renderRecords((await api("/api/records")).items);
  toast("演示数据载入完成，开始核验…");
  await runVerify();
}

/* ---------------- 工具 ---------------- */

function summaryError(e, fallback) {
  const errs = e.payload && e.payload.errors;
  if (errs && errs.length) {
    const first = errs[0];
    const line = first.location && first.location.line;
    return `${fallback}：${line ? "第 " + line + " 行 " : ""}${first.message}`;
  }
  if (e.payload && e.payload.detail) return e.payload.detail;
  return fallback;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- 初始化 ---------------- */

function bind() {
  $("btn-save-config").addEventListener("click", saveConfig);

  $("btn-run-trial").addEventListener("click", runTrialCompare);
  $("btn-trial-copy").addEventListener("click", copyCurrentToTrial);

  $("btn-save-stations").addEventListener("click", () =>
    saveTextSection("站点", "/api/stations", $("stations-text").value, renderStations));
  $("btn-save-fares").addEventListener("click", () =>
    saveTextSection("票价", "/api/fares", $("fares-text").value, renderFares));
  $("btn-save-records").addEventListener("click", () =>
    saveTextSection("记录", "/api/records", $("records-text").value, renderRecords));

  $("btn-load-stations").addEventListener("click", () => loadBack("stations"));
  $("btn-load-fares").addEventListener("click", () => loadBack("fares"));
  $("btn-load-records").addEventListener("click", () => loadBack("records"));

  $("btn-gen-fares").addEventListener("click", genFareTemplate);
  $("btn-repair").addEventListener("click", runRepairPreview);
  $("btn-fare-audit").addEventListener("click", runFareAudit);
  $("btn-duration-audit").addEventListener("click", runDurationAudit);
  $("btn-peak-audit").addEventListener("click", runPeakAudit);
  $("btn-copy-suggests").addEventListener("click", copyAllSuggests);
  $("btn-merge").addEventListener("click", runMergePreview);
  $("btn-merge-copy").addEventListener("click", copyMergedText);
  $("btn-od-publish").addEventListener("click", runOdPublish);
  $("btn-od-csv").addEventListener("click", exportOdCsv);
  $("btn-gate-queue").addEventListener("click", runGateQueue);
  $("btn-gq-csv").addEventListener("click", exportGqCsv);
  $("btn-pass").addEventListener("click", runPassBacktest);
  $("btn-dispute").addEventListener("click", runDisputePreview);
  $("btn-dispute-copy").addEventListener("click", copyDisputeSummary);
  $("btn-comp").addEventListener("click", runCompensationPreview);
  $("btn-verify").addEventListener("click", runVerify);
  $("btn-verify-top").addEventListener("click", runVerify);
  $("btn-demo").addEventListener("click", loadDemo);
}

async function init() {
  bind();
  // 默认把运营日起止填为今天（本地时区），用户可自行调整
  const today = new Date();
  const yyyy = today.getFullYear();
  const mm = String(today.getMonth() + 1).padStart(2, "0");
  const dd = String(today.getDate()).padStart(2, "0");
  $("od-start").value = `${yyyy}-${mm}-${dd}`;
  $("od-end").value = `${yyyy}-${mm}-${dd}`;
  $("gq-day").value = `${yyyy}-${mm}-${dd}`;
  $("pass-start").value = `${yyyy}-${mm}-${dd}`;
  $("pass-end").value = `${yyyy}-${mm}-${dd}`;
  try {
    const cfg = await loadConfig();
    fillTrialInputs(cfg);
    renderStations((await api("/api/stations")).items);
    renderFares((await api("/api/fares")).items);
    renderRecords((await api("/api/records")).items);
    const bills = await api("/api/bills");
    if (bills.count > 0) {
      renderBills(bills.items);
      $("bills-panel").hidden = false;
      $("verify-status").textContent =
        `已加载上次核验结果（${bills.count} 笔，${bills.last_verified_at || ""}），修改数据后请重新核验`;
      $("verify-status").className = "status";
    }
  } catch (e) {
    toast("初始化加载数据失败：" + e.message, false);
  }
}

document.addEventListener("DOMContentLoaded", init);
