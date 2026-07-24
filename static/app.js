// CD Service Dashboard — 前端逻辑

let token = sessionStorage.getItem("cd_token") || "";

// CI 面板自动刷新 — 间隔由用户通过面板上的下拉控件自由选择，localStorage 持久化
const DEFAULT_CI_INTERVAL = 30000;
let _currentPanel = "";
let _ciTimer = null;

const A = () => (token ? { Authorization: "Bearer " + token } : {});

function handle401(r) {
  if (r.status === 401) {
    token = "";
    sessionStorage.removeItem("cd_token");
    document.getElementById("login-page").style.display = "flex";
    document.getElementById("main-app").style.display = "none";
    return true;
  }
  return false;
}

// ── Auth ──

async function doLogin() {
  const u = document.getElementById("login-user").value.trim();
  const p = document.getElementById("login-pass").value;
  const e = document.getElementById("login-err");
  e.style.display = "none";
  if (!u || !p) {
    e.textContent = "请输入账号密码";
    e.style.display = "block";
    return;
  }
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: u, password: p }),
  });
  const d = await r.json();
  if (r.ok && d.token) {
    token = d.token;
    sessionStorage.setItem("cd_token", token);
    document.getElementById("login-page").style.display = "none";
    document.getElementById("main-app").style.display = "block";
    showPanel("ci");
  } else {
    e.textContent = d.detail || "登录失败";
    e.style.display = "block";
  }
}

function doLogout() {
  token = "";
  sessionStorage.removeItem("cd_token");
  document.getElementById("login-page").style.display = "flex";
  document.getElementById("main-app").style.display = "none";
}

// ── Navigation ──

function toggleSubmenu(el) {
  // 只找紧跟在当前父菜单后面的 item-sub（下一个兄弟直到非 item-sub 为止）
  const subs = [];
  let next = el.nextElementSibling;
  while (next && next.classList.contains("item-sub")) {
    subs.push(next);
    next = next.nextElementSibling;
  }
  const open = subs[0]?.style.display === "block";
  subs.forEach(s => s.style.display = open ? "none" : "block");
  el.textContent = open ? el.textContent.replace(" ▾", " ▸") : el.textContent.replace(" ▸", " ▾");
}

function expandMonitorSubmenu() {
  expandSubmenuByName("资源监控");
}

function expandSubmenuByName(name) {
  const allParents = document.querySelectorAll(".item-parent");
  for (const p of allParents) {
    if (p.textContent.includes(name) && p.textContent.includes("▸")) {
      toggleSubmenu(p);
      return;
    }
  }
}

function showPanel(n) {
  document.querySelectorAll(".sidebar .item").forEach((i) => i.classList.remove("active"));
  document.querySelector(`[data-panel="${n}"]`).classList.add("active");
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("show"));
  document.getElementById("panel-" + n).classList.add("show");
  _currentPanel = n;
  if (n === "ci") {
    _initRefreshDropdown();
    _startCIPolling();
  } else {
    _stopCIPolling();
  }

  if (n === "servers") expandSubmenuByName("服务器管理");
  if (n.startsWith("monitor-")) expandMonitorSubmenu();
  if (n === "ci") loadCI();
  if (n === "servers") { loadServers(); loadTagCheckboxes(); }
  if (n === "ssh") loadSshForm();
  if (n === "deploy") loadDeployForm();
  if (n === "k8s") loadK8sForm();
  if (n === "shell") loadShellServers();
  if (n === "logs") loadLogs();
  if (n === "monitor-system") loadMonitorSystem();
  if (n === "monitor-app") loadMonitorApp();
  if (n === "bots") loadBots();
  if (n === "registry") { loadRepositories(); loadSyncConfig(); }
}

function toast(msg, ok) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast toast-" + (ok ? "ok" : "err") + " show";
  setTimeout(() => el.classList.remove("show"), 3000);
}

// ── CI 项目列表 ──

async function loadCI() {
  const r = await fetch("/api/projects", { headers: A() });
  if (handle401(r)) return;
  const d = await r.json();
  document.getElementById("ci-tbody").innerHTML = d
    .map(
      (p) =>
        `<tr>
          <td><strong>${p.job_name}</strong></td>
          <td><span class="badge badge-${p.build_provider === "gitlab_ci" ? "gitlab" : "jenkins"}">${p.build_provider}</span></td>
          <td>${p.harbor_repository || "—"}</td>
          <td>${p.latest_tag || "—"}</td>
          <td>${p.latest_pipeline ? "#" + p.latest_pipeline : "—"}</td>
          <td>
            <button class="btn btn-blue btn-sm" onclick="viewPipelineRow(this,'${p.job_name}')" style="margin-right:6px">构建状态</button>
            <select onchange="quickDeploySelect(this,'${p.job_name}','${p.latest_tag}')" class="deploy-select">
              <option value="">部署到…</option>
              <option value="ssh">单机</option>
              <option value="deploy">Docker</option>
              <option value="k8s">K8S</option>
            </select>
          </td>
        </tr>`
    )
    .join("");
}

function _getCIInterval() {
  const v = localStorage.getItem("cd_refresh_ci");
  return v !== null ? parseInt(v) : DEFAULT_CI_INTERVAL;
}

function _initRefreshDropdown() {
  const sel = document.getElementById("refresh-ci");
  if (!sel) return;
  sel.value = _getCIInterval();
  _updateRefreshBtn();
}

function onRefreshChange() {
  const sel = document.getElementById("refresh-ci");
  const ms = parseInt(sel.value);
  localStorage.setItem("cd_refresh_ci", ms);
  _updateRefreshBtn();
  _startCIPolling();
}

function _updateRefreshBtn() {
  const btn = document.getElementById("refresh-ci-btn");
  if (!btn) return;
  const on = _getCIInterval() > 0 && _currentPanel === "ci";
  btn.className = "btn btn-sm " + (on ? "btn-auto-on" : "btn-auto-off");
  btn.title = on ? "自动刷新中" : "立即刷新";
}

function _startCIPolling() {
  _stopCIPolling();
  const ms = _getCIInterval();
  _updateRefreshBtn();
  if (!ms || ms <= 0) return;
  _ciTimer = setInterval(() => {
    if (_currentPanel === "ci") loadCI();
  }, ms);
}

function _stopCIPolling() {
  if (_ciTimer) { clearInterval(_ciTimer); _ciTimer = null; }
}


function quickDeploySelect(sel, project, tag) {
  const target = sel.value; if (!target) return;
  sel.value = "";
  const parent = document.querySelector(".item-parent");
  if (parent && parent.textContent.includes("▸")) toggleSubmenu(parent);
  showPanel(target);
  setTimeout(() => {
    const projId = target === "ssh" ? "s-project" : target === "k8s" ? "k-project" : "d-project";
    const el = document.getElementById(projId);
    if (el && el.options.length) { el.value = project; viewPipeline(project); }
  }, 300);
}

// ── CI Pipeline 状态 ──

let _vpSeq = 0;

function _setCI(prefix, latest_tag, pipeline_iid, created_at) {
  const card = document.getElementById(prefix + "pipeline-card");
  const stages = document.getElementById(prefix + "pipeline-stages");
  if (!stages) return;
  if (card) card.style.display = "block";
  if (latest_tag) {
    const ptext = pipeline_iid ? 'Pipeline <b>#' + pipeline_iid + '</b> · ' : '';
    stages.innerHTML =
      '<div style="padding:10px;background:#1b3a1b;border-radius:6px;border:1px solid #388e3c">' +
      '<span style="color:#81c784;font-weight:600">✅ CI 已完成</span>' +
      '<div style="margin-top:4px;font-size:12px;color:#999">' + ptext + 'Tag <b>' + latest_tag + '</b>' +
      (created_at ? ' · ' + created_at : '') + '</div></div>';
  } else {
    stages.innerHTML = '<span style="color:#888;font-size:12px">暂无 CI 数据</span>';
  }
}

// ── Tag 下拉分页 ──
const _tagState = {};  // { "s-": {project, page, total_pages}, ... }

async function _loadTagPage(prefix, project, delta) {
  if (!_tagState[prefix]) _tagState[prefix] = { project: "", page: 1, total_pages: 1 };
  const st = _tagState[prefix];

  if (project != null && project !== "") {
    if (st.project !== project) { st.project = project; st.page = 1; }
    if (delta) st.page = Math.max(1, st.page + delta);
  } else if (delta) {
    st.page = Math.max(1, Math.min(st.total_pages, st.page + delta));
  }
  if (!st.project) return;

  const sel = document.getElementById(prefix + "tag");
  const info = document.getElementById(prefix + "tag-info");
  const prev = document.getElementById(prefix + "tag-prev");
  const next = document.getElementById(prefix + "tag-next");
  if (!sel) return;

  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const r = await fetch(
      `/api/projects/${encodeURIComponent(st.project)}/tags?page=${st.page}&page_size=50`,
      { headers: A() }
    );
    const d = await r.json();
    const items = d.items || [];
    st.total_pages = d.total_pages || 1;
    st.page = d.page || 1;

    if (items.length) {
      sel.innerHTML = items.map(t => `<option value="${t.tag}">${t.tag}</option>`).join("");
      sel.value = items[0].tag;
    } else {
      sel.innerHTML = '<option value="">无可用 Tag</option>';
    }

    if (info) info.textContent = st.total_pages > 1 ? `(${st.page}/${st.total_pages} 共${d.total}条)` : "";
    if (prev) prev.disabled = st.page <= 1;
    if (next) next.disabled = st.page >= st.total_pages;

    // 翻页后自动重新展开下拉框
    try { sel.showPicker(); } catch (_) { sel.focus(); }
  } catch (e) { sel.innerHTML = '<option value="">无可用 Tag</option>'; }
}

function loadSshTags(project, delta) { _loadTagPage("s-", project, delta || 0); }
function loadK8sTags(project, delta) { _loadTagPage("k-", project, delta || 0); }
function loadDockerTags(project, delta) { _loadTagPage("d-", project, delta || 0); }

function _setTags(selId, tags) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  if (tags.length) {
    sel.innerHTML = tags.map(t => `<option value="${t.tag}">${t.tag}</option>`).join("");
    sel.value = tags[0].tag;
  } else {
    sel.innerHTML = '<option value="">无可用 Tag</option>';
  }
}

async function viewPipelineRow(btn, project) {
  const tr = btn.closest("tr");
  const existing = tr.nextElementSibling;
  if (existing && existing.classList.contains("ci-detail-row")) { existing.remove(); return; }

  const detail = document.createElement("tr");
  detail.className = "ci-detail-row";
  detail.innerHTML = '<td colspan="6"><div style="padding:10px;color:#888;font-size:12px">加载中…</div></td>';
  tr.parentNode.insertBefore(detail, tr.nextSibling);

  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(project)}/pipeline`, { headers: A() });
    if (handle401(r)) { detail.remove(); return; }
    const d = await r.json();
    if (d.latest_tag) {
      const p = d.pipeline || {};
      detail.innerHTML = '<td colspan="6"><div style="padding:10px;background:#1b3a1b;border-radius:4px;border:1px solid #388e3c">' +
        '<span style="color:#81c784;font-weight:600">✅ CI 已完成</span>' +
        '<span style="font-size:12px;color:#999;margin-left:8px">' +
        (p.iid ? 'Pipeline #' + p.iid + ' · ' : '') + 'Tag ' + d.latest_tag +
        (p.created_at ? ' · ' + p.created_at : '') + '</span></div></td>';
    } else {
      detail.innerHTML = '<td colspan="6"><div style="padding:10px;color:#888;font-size:12px">暂无 CI 数据</div></td>';
    }
  } catch(e) {
    detail.innerHTML = '<td colspan="6"><div style="padding:10px;color:#888;font-size:12px">暂无 CI 数据</div></td>';
  }
}

async function viewPipeline(project) {
  if (!project) return;
  const seq = ++_vpSeq;
  _setCI("", "", "", "", ""); _setCI("ssh-", "", "", "", ""); _setCI("k-", "", "", "", "");

  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(project)}/pipeline`, { headers: A() });
    const d = await r.json();
    if (seq !== _vpSeq) return;
    const tag = d.latest_tag || "";
    const iid = d.pipeline?.iid;
    const created = d.pipeline?.created_at || "";
    _setCI("", tag, iid, created);
    _setCI("ssh-", tag, iid, created);
    _setCI("k-", tag, iid, created);
  } catch(e) {}

  // Tag 下拉：分页加载，每面板独立
  _loadTagPage("d-", project, 0);
  _loadTagPage("s-", project, 0);
  _loadTagPage("k-", project, 0);
}

// ── 服务器管理 ──

async function loadServers() {
  const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: A() });
  if (handle401(r)) return;
  let d = await r.json();
  const filter = (document.getElementById("sv-filter")?.value || "").toLowerCase();
  if (filter) d = d.filter(s => (s.tags || "").toLowerCase().includes(filter));
  document.getElementById("sv-tbody").innerHTML = d
    .map(
      (s) =>
        `<tr><td>${s.name}</td><td>${s.host}:${s.port}</td><td>${s.type}</td>
         <td>${(s.tags||"").split(",").filter(Boolean).map(t=>`<span class="badge badge-gitlab" style="margin:1px">${t}</span>`).join("")}</td>
         <td><button class="btn btn-edit btn-sm" style="margin-right:4px" onclick="editServer(${s.id})">编辑</button><button class="btn btn-red btn-sm" onclick="delServer(${s.id})">删除</button></td></tr>`
    )
    .join("");
  // Docker 面板的服务器列表由 loadDeployForm() 单独管理（仅显示 Docker 类型）
}

// ── 服务器多选下拉 ──

function toggleServerDropdown(prefix) {
  const wrap = document.getElementById(prefix + "-server-wrap");
  if (wrap) wrap.classList.toggle("open");
}

function toggleServerItem(prefix, el) {
  const cb = el.querySelector("input[type='checkbox']");
  cb.checked = !cb.checked;
  updateServerSelection(prefix);
}

function toggleSelectAllServers(prefix, checked) {
  document.querySelectorAll(`#${prefix}-server-list input[type='checkbox']`).forEach((cb) => {
    cb.checked = checked;
  });
  updateServerSelection(prefix);
}

function updateServerSelection(prefix) {
  const cbs = document.querySelectorAll(`#${prefix}-server-list input[type='checkbox']:checked`);
  const ids = Array.from(cbs).map((cb) => cb.value);
  const allCbs = document.querySelectorAll(`#${prefix}-server-list input[type='checkbox']`);
  const allCheck = document.getElementById(prefix + "-server-all");
  const text = document.getElementById(prefix + "-server-text");
  if (!text) return;

  if (allCheck) allCheck.checked = allCbs.length > 0 && cbs.length === allCbs.length;

  if (ids.length === 0) {
    text.textContent = "— 请选择 —";
    text.classList.remove("has-selection");
  } else if (ids.length === allCbs.length) {
    text.textContent = `已选全部 (${ids.length})`;
    text.classList.add("has-selection");
  } else {
    const names = [];
    cbs.forEach((cb) => {
      const label = cb.parentElement.querySelector("label");
      if (label) names.push(label.textContent.split(" (")[0]);
    });
    text.textContent = names.length <= 3 ? names.join(", ") : `${names.slice(0, 3).join(", ")} +${names.length - 3}`;
    text.classList.add("has-selection");
  }
}

function getSelectedServerIds(prefix) {
  const cbs = document.querySelectorAll(`#${prefix}-server-list input[type='checkbox']:checked`);
  const ids = Array.from(cbs).map((cb) => cb.value);
  return ids.length > 0 ? ids.join(",") : "";
}

function populateTags(prefix, servers) {
  const tagsEl = document.getElementById(prefix + "-server-tags");
  if (!tagsEl) return;
  const tagSet = new Set();
  servers.forEach((s) => {
    (s.tags || "").split(",").filter(Boolean).forEach((t) => tagSet.add(t.trim().toLowerCase()));
  });
  if (tagSet.size === 0) { tagsEl.innerHTML = ""; return; }
  const tags = Array.from(tagSet).sort();
  tagsEl.innerHTML = tags
    .map((t) => `<span class="multi-select-tag" onclick="selectByTag('${prefix}', '${t}', this)" data-tag="${t}">${t}</span>`)
    .join("");
}

function selectByTag(prefix, tag, el) {
  el.classList.toggle("active");
  const active = el.classList.contains("active");
  const cbs = document.querySelectorAll(`#${prefix}-server-list input[type='checkbox']`);
  cbs.forEach((cb) => {
    const tags = cb.dataset.tags || "";
    if (tags.split(",").map((t) => t.trim()).includes(tag)) {
      cb.checked = active;
    }
  });
  updateServerSelection(prefix);
}

// 点击外部关闭下拉
document.addEventListener("click", function (e) {
  ["d-server-wrap", "s-server-wrap"].forEach(function(id) {
    const wrap = document.getElementById(id);
    if (wrap && !wrap.contains(e.target)) {
      wrap.classList.remove("open");
    }
  });
});

function showServerForm() {
  cancelEdit();
  document.getElementById("sv-form-card").style.display = "block";
  document.getElementById("sv-form-title").textContent = "➕ 添加服务器";
}

async function addServer() {
  const editId = document.getElementById("sv-edit-id").value;
  const n = document.getElementById("sv-name").value.trim();
  const h = document.getElementById("sv-host").value.trim();
  const u = document.getElementById("sv-user").value.trim() || "root";
  const at = document.getElementById("sv-auth-type").value;
  const p = at === "password" ? document.getElementById("sv-pass").value : "";
  const k = at === "key" ? document.getElementById("sv-key").value.trim() : "";
  const t = getSelectedTags();
  const tp = document.getElementById("sv-type").value;
  if (!n || !h) return toast("填名称和主机", false);

  const isEdit = !!editId;
  const url = isEdit ? `/api/servers/${editId}` : "/api/servers";
  const method = isEdit ? "PUT" : "POST";
  const r = await fetch(url, {
    method: method,
    headers: Object.assign({ "Content-Type": "application/json" }, A()),
    body: JSON.stringify({
      name: n,
      host: h.split(":")[0],
      port: parseInt(h.split(":")[1] || "22"),
      user: u,
      auth_type: at,
      password: p,
      ssh_key: k,
      tags: t,
      type: tp,
    }),
  });
  if (handle401(r)) return;
  const d = await r.json();
  toast(d.success ? (isEdit ? "已更新" : "已添加") : "失败", d.success);
  if (d.success) { cancelEdit(); loadServers(); }
}

async function editServer(id) {
  // 从当前表格中找到对应行数据，重新 fetch 获取完整信息
  const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: A() });
  if (handle401(r)) return;
  const servers = await r.json();
  const s = servers.find(srv => srv.id === id);
  if (!s) return toast("找不到该服务器", false);

  document.getElementById("sv-form-card").style.display = "block";
  document.getElementById("sv-form-title").textContent = "✏️ 编辑服务器";
  document.getElementById("sv-edit-id").value = s.id;
  document.getElementById("sv-name").value = s.name;
  document.getElementById("sv-host").value = s.host + ":" + s.port;
  document.getElementById("sv-user").value = s.user;
  document.getElementById("sv-auth-type").value = s.auth_type || "password";
  document.getElementById("sv-pass").value = s.password || "";
  document.getElementById("sv-key").value = s.ssh_key || "";
  document.getElementById("sv-type").value = s.type || "ssh";
  toggleAuthType(); // 切换显示密码/密钥输入框
  // 标签复选框 + 自定义标签
  loadTagCheckboxes().then(() => {
    clearCustomTags();
    const tags = (s.tags || "").split(",").filter(Boolean).map(t => t.trim());
    document.querySelectorAll("#sv-tag-checkboxes input").forEach(cb => {
      cb.checked = tags.includes(cb.value);
    });
    // 不在已有复选框里的标签，放到自定义区
    const existing = Array.from(document.querySelectorAll("#sv-tag-checkboxes input")).map(cb => cb.value);
    tags.forEach(t => {
      if (!existing.includes(t)) {
        const label = document.createElement("label");
        label.className = "tag-checkbox-label";
        label.innerHTML = `<input type="checkbox" value="${t}" checked><span>${t}</span>`;
        document.getElementById("sv-custom-tags").appendChild(label);
      }
    });
  });

  document.getElementById("sv-save-btn").textContent = "💾 保存";
  document.getElementById("sv-cancel-btn").style.display = "inline-block";
}

function cancelEdit() {
  document.getElementById("sv-form-card").style.display = "none";
  document.getElementById("sv-edit-id").value = "";
  document.getElementById("sv-name").value = "";
  document.getElementById("sv-host").value = "";
  document.getElementById("sv-user").value = "root";
  document.getElementById("sv-auth-type").value = "password";
  document.getElementById("sv-pass").value = "";
  document.getElementById("sv-key").value = "";
  document.getElementById("sv-type").value = "ssh";
  toggleAuthType();
  // 清空标签复选框
  document.querySelectorAll("#sv-tag-checkboxes input").forEach(cb => cb.checked = false);
  clearCustomTags();
  document.getElementById("sv-save-btn").textContent = "＋ 添加";
  document.getElementById("sv-cancel-btn").style.display = "none";
}

async function delServer(id) {
  if (!confirm("确定删除?")) return;
  const r = await fetch(`/api/servers/${id}`, { method: "DELETE", headers: A() });
  if (handle401(r)) return;
  toast("已删除", true);
  loadServers();
}

// ── SSH 认证方式切换 ──

function toggleAuthType() {
  const at = document.getElementById("sv-auth-type").value;
  document.getElementById("sv-pass").style.display = at === "password" ? "block" : "none";
  document.getElementById("sv-key").style.display = at === "key" ? "block" : "none";
}

// ── 标签复选框 ──

function getSelectedTags() {
  const cbs = document.querySelectorAll("#sv-tag-checkboxes input:checked, #sv-custom-tags input:checked");
  return Array.from(cbs).map(cb => cb.value).join(",");
}

async function loadTagCheckboxes() {
  try {
    const r = await fetch("/api/tags", { headers: A() });
    if (handle401(r)) return;
    const tags = await r.json();
    const container = document.getElementById("sv-tag-checkboxes");
    if (!container) return;
    container.innerHTML = tags.length
      ? tags.map(t => `<label class="tag-checkbox-label"><input type="checkbox" value="${t.name}"><span>${t.name}</span></label>`).join("")
      : '<span style="color:#667;font-size:12px">暂无标签，给服务器添加标签后会自动出现</span>';
  } catch(e) {}
}

function addCustomTag() {
  const input = document.getElementById("sv-tag-input");
  const name = input.value.trim();
  if (!name) return;
  const wrap = document.getElementById("sv-custom-tags");
  const exists = Array.from(wrap.querySelectorAll("input")).some(cb => cb.value === name);
  if (exists) { input.value = ""; return; }
  const label = document.createElement("label");
  label.className = "tag-checkbox-label";
  label.innerHTML = `<input type="checkbox" value="${name}" checked><span>${name}</span>`;
  wrap.appendChild(label);
  input.value = "";
}

function clearCustomTags() {
  document.getElementById("sv-custom-tags").innerHTML = "";
  document.getElementById("sv-tag-input").value = "";
}

// ── 部署 ──

let _deployFormReady = false;

async function loadDeployForm() {
  const r = await fetch("/api/projects", { headers: A() });
  if (handle401(r)) return;
  const d = await r.json();
  window._projects = d;
  const sel = document.getElementById("d-project");
  sel.innerHTML = d.map(p => `<option value="${p.job_name}">${p.job_name}</option>`).join("");
  const currentVal = sel.dataset.last || sel.value;
  if (currentVal && d.find(p => p.job_name === currentVal)) {
    sel.value = currentVal;
  } else {
    sel.value = d[0]?.job_name || "";
  }
  sel.dataset.last = sel.value;

  // Docker 部署面板：只显示 Docker 类型服务器
  const sr = await fetch(`/api/servers?_=${Date.now()}`, { headers: A() });
  if (!handle401(sr)) {
    const allSrv = await sr.json();
    const dockerSrv = allSrv.filter(s => s.type === "docker");
    const list = document.getElementById("d-server-list");
    if (list) {
      list.innerHTML = dockerSrv
        .map((s) =>
          `<div class="multi-select-item" onclick="toggleServerItem('d', this)">
            <input type="checkbox" value="${s.id}" data-tags="${(s.tags || '').toLowerCase()}" onchange="updateServerSelection('d')">
            <label>${s.name} (${s.host})</label>
          </div>`
        )
        .join("");
      populateTags('d', dockerSrv);
      updateServerSelection('d');
    }
  }

  loadBots();
  if (!_deployFormReady) {
    toggleDeployType();
    _deployFormReady = true;
  }
  viewPipeline(sel.value);
}

// 项目切换监听（去抖）
document.addEventListener("DOMContentLoaded", () => {
  let _timer;
  document.addEventListener("change", (e) => {
    if (e.target.id === "d-project" || e.target.id === "s-project" || e.target.id === "k-project") {
      clearTimeout(_timer);
      _timer = setTimeout(() => {
        e.target.dataset.last = e.target.value;
        viewPipeline(e.target.value);
      }, 100);
    }
  });
});

function quickDeploy(project, tag) {
  const parent = document.querySelector(".item-parent");
  if (parent && parent.textContent.includes("▸")) toggleSubmenu(parent);
  showPanel("ssh");
  setTimeout(() => {
    document.getElementById("s-project").value = project;
    loadSshTags(project);
  }, 100);
}

const MODE_OPTIONS = {
  compose: [
    { value: "remote", label: "docker-compose.yml" },
    { value: "commands", label: "自定义命令" },
  ],
};

const PATH_LABELS = {
  ssh_ansible: "Ansible Playbook 路径",
  compose_remote: "docker-compose.yml 路径",
  k8s_apply: "K8s YAML 路径",
};

function toggleDeployType() {
  const t = document.getElementById("d-type").value;
  const modeSel = document.getElementById("d-mode");
  const opts = MODE_OPTIONS[t] || [];
  modeSel.innerHTML = opts.map((o) => `<option value="${o.value}">${o.label}</option>`).join("");
  document.getElementById("d-mode-wrap").style.display = "block";
  toggleDeployMode();
}

function toggleDeployMode() {
  const t = document.getElementById("d-type").value;
  const m = document.getElementById("d-mode").value;
  const pathWrap = document.getElementById("d-path-wrap");
  const cmdWrap = document.getElementById("d-cmd-wrap");
  const pathLabel = document.getElementById("d-path-label");
  const pathInput = document.getElementById("d-path");

  // reset
  pathWrap.style.display = "none";
  cmdWrap.style.display = "none";
  document.getElementById("d-yaml-wrap").style.display = "none";
  pathInput.placeholder = "";

  if (m === "commands" || (t === "ssh" && m !== "ansible")) {
    cmdWrap.style.display = "block";
  }
  if (t === "ssh" && m === "ansible") {
    pathWrap.style.display = "block";
    pathLabel.textContent = PATH_LABELS["ssh_ansible"];
    pathInput.placeholder = "/opt/ansible/deploy.yml";
  } else if (t === "compose" && m === "remote") {
    pathWrap.style.display = "block";
    pathLabel.textContent = "应用路径";
    pathInput.placeholder = "/opt/app （不存在则自动创建）";
    document.getElementById("d-yaml-wrap").style.display = "block";
  } else if (t === "k8s" && m === "apply") {
    pathWrap.style.display = "block";
    pathLabel.textContent = PATH_LABELS["k8s_apply"];
    pathInput.placeholder = "/opt/k8s/deploy.yaml";
  } else if (t === "k8s" && m === "setimage") {
  }
  // docker / setimage: no extra fields needed
}

async function doDeploy() {
  const tag = document.getElementById("d-tag").value;
  if (!tag) return toast("没有可用的 Tag，请先运行 CI 构建", false);
  const sid = getSelectedServerIds('d');
  if (!sid) return toast("请选择至少一台服务器", false);
  const body = {
    project: document.getElementById("d-project").value,
    tag: tag,
    deploy_type: document.getElementById("d-type").value,
    server_ids: sid,
    target_path: document.getElementById("d-path").value,
    deploy_mode: document.getElementById("d-mode").value,
    commands: document.getElementById("d-cmds").value,
    yaml_content: document.getElementById("d-yaml").value,
    bot_id: parseInt(document.getElementById("d-bot").value) || 0,
  };
  const out = document.getElementById("deploy-out");
  out.textContent = "$ 正在部署...\n";

  try {
    const r = await fetch("/api/deploy-stream", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, A()),
      body: JSON.stringify(body),
    });
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      while (buffer.includes("\n\n")) {
        const idx = buffer.indexOf("\n\n");
        const line = buffer.substring(0, idx);
        buffer = buffer.substring(idx + 2);

        if (!line.startsWith("data: ")) continue;
        const data = line.substring(6);

        if (data.startsWith("ERROR:")) {
          out.textContent += "\n❌ " + data.substring(6);
          toast("❌ 部署失败", false);
          return;
        } else if (data.startsWith("END:")) {
          const parts = data.substring(4).split(":");
          const success = parts[1] === "true";
          toast(success ? "✅ 部署成功" : "❌ 部署失败", success);
          if (success) loadLogs();
          return;
        } else if (data === ".") {
          continue;
        } else {
          out.textContent += data + "\n";
          out.scrollTop = out.scrollHeight;
        }
      }
    }
  } catch (e) {
    out.textContent += "\n❌ " + e.message;
    toast("❌ 部署失败", false);
  }
}

async function doStop() {
  if (!confirm("确定停止？")) return;
  const body = {
    project: document.getElementById("d-project").value,
    deploy_type: document.getElementById("d-type").value,
    server_ids: getSelectedServerIds('d'),
    target_path: document.getElementById("d-path").value,
  };
  document.getElementById("deploy-out").textContent = "停止中…";
  const r = await fetch("/api/stop", {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, A()),
    body: JSON.stringify(body),
  });
  const d = await r.json();
  document.getElementById("deploy-out").textContent = JSON.stringify(d, null, 2);
  toast(d.success ? "✅ 已停止" : "❌ 停止失败", d.success);
}

// ── 部署记录 ──

let _logData = [];
let _logPage = 1;
let _logPageSize = 15;
let _logTotal = 0;
let _logTotalPages = 1;

async function loadLogs(page = 1) {
  try {
    document.getElementById("log-tbody").innerHTML = '<tr><td colspan="7" style="text-align:center;color:#888">加载中...</td></tr>';
    renderLogPager();
    const r = await fetch(`/api/deploy-logs?page=${page}&page_size=${_logPageSize}`, { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    _logData = d.items || [];
    _logPage = d.page || 1;
    _logTotal = d.total || 0;
    _logTotalPages = d.total_pages || 1;

    if (!_logData.length) {
      document.getElementById("log-tbody").innerHTML = '<tr><td colspan="7" style="text-align:center;color:#888">暂无部署记录</td></tr>';
      renderLogPager();
      return;
    }
    document.getElementById("log-tbody").innerHTML = _logData
      .map(
        (l, idx) =>
          `<tr style="cursor:pointer" data-log-idx="${idx}">
            <td><span style="color:#81c784;font-weight:bold">#${l.deploy_id || l.id}</span></td>
            <td>${l.created_at}</td><td>${l.project}</td><td>${l.tag}</td><td>${l.deploy_type}</td>
            <td><span class="badge badge-${l.status === "ok" ? "ok" : l.status === "failed" ? "err" : "pend"}">${l.status}</span></td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${l.output || ""}</td>
          </tr>`
      )
      .join("");

    renderLogPager();

    document.getElementById("log-tbody").onclick = function(e) {
      const tr = e.target.closest("tr");
      if (!tr || tr.dataset.logIdx === undefined) return;
      const existing = tr.nextElementSibling;
      if (existing && existing.classList.contains("log-detail")) { existing.remove(); return; }
      const output = _logData[parseInt(tr.dataset.logIdx)]?.output || "(无输出)";
      const escaped = output.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
      const detail = document.createElement("tr");
      detail.className = "log-detail";
      detail.innerHTML = `<td colspan="7"><pre style="margin:8px 0;font-size:12px;white-space:pre-wrap;max-height:300px;overflow-y:auto;background:#111;color:#00ff00;padding:10px;border-radius:4px;font-family:monospace">${escaped}</pre></td>`;
      tr.parentNode.insertBefore(detail, tr.nextSibling);
    };
  } catch(e) {
    console.error("加载部署记录失败:", e);
    document.getElementById("log-tbody").innerHTML = '<tr><td colspan="7" style="text-align:center;color:#888">加载失败</td></tr>';
    renderLogPager();
  }
}

function renderLogPager() {
  const container = document.getElementById("log-pager");
  if (!container) return;
  if (_logTotalPages <= 1) {
    container.innerHTML = "";
    return;
  }
  let html = `<span class="log-pager-info">共 ${_logTotal} 条 / ${_logTotalPages} 页</span>`;
  if (_logPage > 1) html += `<button class="btn btn-sm log-pager-btn" onclick="loadLogs(${_logPage - 1})">上一页</button>`;
  // 页码按钮
  const start = Math.max(1, _logPage - 2);
  const end = Math.min(_logTotalPages, _logPage + 2);
  for (let i = start; i <= end; i++) {
    html += `<button class="btn btn-sm log-pager-btn${i === _logPage ? ' active' : ''}" onclick="loadLogs(${i})">${i}</button>`;
  }
  if (_logPage < _logTotalPages) html += `<button class="btn btn-sm log-pager-btn" onclick="loadLogs(${_logPage + 1})">下一页</button>`;
  // 每页条数
  html += `<select class="log-pager-size" onchange="setLogPageSize(this.value)">
    <option value="10"${_logPageSize === 10 ? " selected" : ""}>10条/页</option>
    <option value="15"${_logPageSize === 15 ? " selected" : ""}>15条/页</option>
    <option value="30"${_logPageSize === 30 ? " selected" : ""}>30条/页</option>
    <option value="50"${_logPageSize === 50 ? " selected" : ""}>50条/页</option>
  </select>`;
  container.innerHTML = html;
}

function setLogPageSize(size) {
  _logPageSize = parseInt(size);
  loadLogs(1);
}

// ── 通知 BOT 管理 ──

async function loadBots() {
  const r = await fetch("/api/bots", { headers: A() });
  if (handle401(r)) return;
  const d = await r.json();
  // 列表
  const tbody = document.getElementById("bot-tbody");
  if (tbody) {
    tbody.innerHTML = d
      .map(
        (b) =>
          `<tr><td>${b.name}</td><td>${b.type}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${b.webhook_url}</td>
           <td><button class="btn btn-red btn-sm" onclick="delBot(${b.id})">删除</button></td></tr>`
      )
      .join("");
  }
  // 部署面板下拉
  const sel = document.getElementById("d-bot");
  if (sel) {
    sel.innerHTML = '<option value="0">— 不通知 —</option>' +
      d.map((b) => `<option value="${b.id}">${b.name} (${b.type})</option>`).join("");
  }
}

async function addBot() {
  const n = document.getElementById("bot-name").value.trim();
  const t = document.getElementById("bot-type").value;
  const u = document.getElementById("bot-url").value.trim();
  if (!n || !u) return toast("填名称和 Webhook URL", false);
  const r = await fetch("/api/bots", {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, A()),
    body: JSON.stringify({ name: n, type: t, webhook_url: u }),
  });
  if (handle401(r)) return;
  const d = await r.json();
  toast(d.success ? "已添加" : "失败", d.success);
  if (d.success) loadBots();
}

async function delBot(id) {
  if (!confirm("确定删除?")) return;
  const r = await fetch(`/api/bots/${id}`, { method: "DELETE", headers: A() });
  if (handle401(r)) return;
  toast("已删除", true);
  loadBots();
}

// ── 镜像仓库管理 ──

let _registryRepoId = 0;
let _registryRepoName = "";
let _artifactPage = 1;
let _artifactPageSize = 20;

function severityClass(sev) {
  const m = { Critical: "sev-critical", High: "sev-high", Medium: "sev-medium", Low: "sev-low" };
  return m[sev] || "sev-none";
}
function severityEmoji(sev) {
  const m = { Critical: "🔴", High: "🟠", Medium: "🟡", Low: "🔵" };
  return m[sev] || "⚪";
}
function formatTime(t) {
  if (!t) return "-";
  try {
    // 数据库统一存储 UTC 时间（无 Z 后缀），补 Z 后按 UTC 解析，输出本地时间
    const normalized = t.replace(" ", "T") + (t.includes("Z") ? "" : "Z");
    const d = new Date(normalized);
    if (isNaN(d.getTime())) return t.slice(0, 16);
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch (e) {
    return t.slice(0, 16);
  }
}

async function loadRepositories() {
  const grid = document.getElementById("registry-repo-grid");
  const syncLabel = document.getElementById("registry-last-sync");
  grid.innerHTML = '<div style="color:#888;text-align:center;padding:40px">加载中…</div>';
  try {
    const r = await fetch("/api/registry/repositories", { headers: A() });
    if (handle401(r)) return;
    const data = await r.json();
    const repos = data.repositories || [];
    const lastSync = data.last_sync || "";
    // 渲染最后同步时间
    if (syncLabel) {
      syncLabel.textContent = lastSync ? "上次同步：" + formatTime(lastSync) : "尚未同步";
    }
    if (!repos.length) {
      grid.innerHTML = '<div style="color:#888;text-align:center;padding:40px">暂无已配置镜像仓库的项目，请先同步数据 <button class="btn btn-sm btn-green" onclick="triggerRegistrySync()">立即同步</button></div>';
      return;
    }
    grid.innerHTML = repos.map(repo => {
      return `<div class="repo-card" onclick="viewArtifacts(${repo.id},'${escHtml(repo.repo)}')">
        <div class="repo-card-icon">🐳</div>
        <div class="repo-card-body">
          <div class="repo-card-path"><span class="repo-card-project">${escHtml(repo.project)}</span> / <span class="repo-card-repo">${escHtml(repo.repo.split("/").pop())}</span></div>
          <div class="repo-card-full">${escHtml(repo.repo)}</div>
        </div>
        <div class="repo-card-stats">
          <span class="repo-stat"><span class="repo-stat-num">${repo.tag_count}</span> Tags</span>
          <span class="repo-stat"><span class="repo-stat-time">${formatTime(repo.latest_push)}</span></span>
        </div>
        <div class="repo-card-arrow">→</div>
      </div>`;
    }).join("");
  } catch (e) {
    grid.innerHTML = `<div style="color:var(--err);text-align:center;padding:40px">加载失败: ${escHtml(e.message)}</div>`;
  }
}

async function viewArtifacts(repoId, repoName, page = 1) {
  _registryRepoId = repoId;
  _registryRepoName = repoName;
  _artifactPage = page;
  document.getElementById("registry-repo-card").style.display = "none";
  document.getElementById("registry-artifact-card").style.display = "block";
  document.getElementById("registry-artifact-repo-title").textContent = repoName;
  document.getElementById("registry-sync-current-btn").setAttribute("onclick", `syncCurrentRepo()`);

  const tbody = document.getElementById("registry-artifact-tbody");
  const pager = document.getElementById("registry-artifact-pager");
  tbody.innerHTML = '<tr><td colspan="5">加载中…</td></tr>';
  pager.innerHTML = "";
  try {
    const r = await fetch(`/api/registry/artifacts/${repoId}?page=${page}&page_size=${_artifactPageSize}`, { headers: A() });
    if (handle401(r)) return;
    const data = await r.json();
    const items = data.items || [];
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:#888;text-align:center">暂无 Tag</td></tr>';
      return;
    }
    const renderRow = a => {
      const sev = a.scan_severity || "";
      const st = (a.scan_status || "").toLowerCase();
      const done = ["success", "finished", "complete", "done"].includes(st);
      const hasVuln = (a.vuln_critical || 0) + (a.vuln_high || 0) + (a.vuln_medium || 0) + (a.vuln_low || 0) > 0;
      const sevBadge = sev && sev !== "None" && hasVuln
        ? `<span class="severity-badge ${severityClass(sev)}">${severityEmoji(sev)} ${sev} C:${a.vuln_critical} H:${a.vuln_high} M:${a.vuln_medium}</span>`
        : (done
          ? (hasVuln
            ? `<span class="severity-badge ${severityClass(sev || 'Unknown')}">${severityEmoji(sev || 'Unknown')} ${sev || 'Unknown'} C:${a.vuln_critical} H:${a.vuln_high} M:${a.vuln_medium}</span>`
            : '<span class="severity-badge sev-none">⚪ 无漏洞</span>')
          : '<span class="severity-badge sev-none">未扫描</span>');
      return `<tr>
        <td><code style="font-size:13px;font-weight:600;color:var(--accent)">${escHtml(a.tag)}</code></td>
        <td>${a.size_mb} MB</td>
        <td style="font-size:12px;white-space:nowrap">${formatTime(a.push_time)}</td>
        <td>${sevBadge}</td>
        <td>
          <button class="btn btn-blue btn-sm" onclick="event.stopPropagation();showScanReport(${a.id},'${escHtml(a.tag)}','${escHtml(_registryRepoName)}')" title="查看扫描报告">🔍 报告</button>
          <button class="btn btn-orange btn-sm" onclick="event.stopPropagation();triggerHarborScan(${a.id},'${escHtml(a.tag)}')" title="触发 Harbor 重新扫描">🔄 扫描</button>
          <button class="btn btn-red btn-sm" onclick="event.stopPropagation();confirmDeleteArtifact('${escHtml(a.tag)}')" title="删除 Tag">🗑</button>
        </td>
      </tr>`;
    };
    tbody.innerHTML = items.map(renderRow).join("");

    // 渲染分页控件
    const total = data.total || 0;
    const totalPages = data.total_pages || 1;
    if (totalPages > 1) {
      let html = `<span style="color:#888">共 ${total} 条 / ${totalPages} 页</span>`;
      html += `<button class="btn btn-sm" onclick="viewArtifacts(_registryRepoId,_registryRepoName,${page - 1})" ${page <= 1 ? "disabled" : ""}>◀ 上一页</button>`;
      // 页码按钮（最多显示 7 个）
      const maxBtns = 7;
      let start = Math.max(1, page - Math.floor(maxBtns / 2));
      let end = Math.min(totalPages, start + maxBtns - 1);
      if (end - start < maxBtns - 1) start = Math.max(1, end - maxBtns + 1);
      for (let i = start; i <= end; i++) {
        html += `<button class="btn btn-sm${i === page ? ' btn-active' : ''}" onclick="viewArtifacts(_registryRepoId,_registryRepoName,${i})">${i}</button>`;
      }
      html += `<button class="btn btn-sm" onclick="viewArtifacts(_registryRepoId,_registryRepoName,${page + 1})" ${page >= totalPages ? "disabled" : ""}>下一页 ▶</button>`;
      pager.innerHTML = html;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--err)">加载失败: ${escHtml(e.message)}</td></tr>`;
  }
}

function backToRepositories() {
  document.getElementById("registry-repo-card").style.display = "block";
  document.getElementById("registry-artifact-card").style.display = "none";
  _registryRepoId = 0;
  _registryRepoName = "";
}

let _syncing = false;

async function triggerRegistrySync() {
  if (_syncing) return toast("⏳ 正在同步中，请稍候…", false);
  _syncing = true;
  const grid = document.getElementById("registry-repo-grid");
  const syncLabel = document.getElementById("registry-last-sync");
  grid.innerHTML = '<div style="color:#888;text-align:center;padding:40px">🔄 正在从 Harbor 同步数据…</div>';
  try {
    const r = await fetch("/api/registry/sync", { method: "POST", headers: A() });
    if (handle401(r)) { _syncing = false; return; }
    const d = await r.json();
    if (d.ok) {
      toast(`✅ 同步完成：${d.total} artifacts, ${d.repos} 仓库`, true);
      if (syncLabel) syncLabel.textContent = "上次同步：同步中…";
    } else {
      toast("⚠️ 同步完成但有问题", false);
    }
    loadRepositories();
  } catch (e) {
    toast("❌ 同步失败: " + e.message, false);
    loadRepositories();
  } finally {
    _syncing = false;
  }
}

// ── 同步间隔配置 ──

async function loadSyncConfig() {
  const sel = document.getElementById("registry-sync-interval");
  if (!sel) return;
  try {
    const r = await fetch("/api/registry/config", { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    const val = String(d.interval || 0);
    // 如果当前值是预设值之一，直接选中；否则追加一项
    if ([...sel.options].some(o => o.value === val)) {
      sel.value = val;
    } else {
      const opt = document.createElement("option");
      opt.value = val;
      opt.textContent = val > 0 ? `${val}分钟（自定义）` : "已关闭";
      sel.appendChild(opt);
      sel.value = val;
    }
  } catch (e) {
    sel.value = "0";
  }
}

async function onSyncIntervalChange() {
  const sel = document.getElementById("registry-sync-interval");
  if (!sel) return;
  const interval = parseInt(sel.value) || 0;
  try {
    const r = await fetch("/api/registry/config", {
      method: "PUT",
      headers: { ...A(), "Content-Type": "application/json" },
      body: JSON.stringify({ interval }),
    });
    if (handle401(r)) return;
    const d = await r.json();
    if (d.ok) {
      if (interval <= 0) {
        toast("🔕 定时同步已关闭", true);
      } else {
        const label = interval >= 60 ? `${interval / 60}小时` : `${interval}分钟`;
        toast(`⏱️ 定时同步间隔已设为 ${label}`, true);
      }
    } else {
      toast("⚠️ " + (d.detail || "设置失败"), false);
      loadSyncConfig(); // 回滚显示
    }
  } catch (e) {
    toast("❌ 设置失败: " + e.message, false);
    loadSyncConfig(); // 回滚显示
  }
}

async function syncCurrentRepo() {
  if (_syncing) return toast("⏳ 正在同步中，请稍候…", false);
  if (!_registryRepoName) return;
  _syncing = true;
  const btn = document.getElementById("registry-sync-current-btn");
  const origText = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 同步中…"; }
  try {
    const r = await fetch(`/api/registry/sync?project=${encodeURIComponent(_registryRepoName.split("/")[0])}`, { method: "POST", headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    toast(d.ok ? "✅ 同步完成" : "⚠️ 同步失败", d.ok);
    if (_registryRepoId) viewArtifacts(_registryRepoId, _registryRepoName, _artifactPage);
  } catch (e) {
    toast("❌ 同步失败: " + e.message, false);
  } finally {
    _syncing = false;
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

// ── 扫描报告 ──

async function showScanReport(artifactId, tag, repoName) {
  document.getElementById("scan-dialog-tag").textContent = tag;
  const content = document.getElementById("scan-dialog-content");
  content.innerHTML = '<div style="color:#888;text-align:center;padding:20px">加载中…</div>';
  document.getElementById("registry-scan-dialog").style.display = "flex";
  try {
    const r = await fetch(`/api/registry/scan/report/${_registryRepoId}/${encodeURIComponent(tag)}`, { headers: A() });
    if (handle401(r)) return;
    if (r.status === 404) {
      content.innerHTML = '<div style="color:#888;text-align:center;padding:20px">该 Tag 暂无扫描报告</div>';
      return;
    }
    const d = await r.json();
    renderScanReport(content, d, tag, repoName);
  } catch (e) {
    content.innerHTML = `<div style="color:var(--err)">加载失败: ${escHtml(e.message)}</div>`;
  }
}

function severityLabel(sev) {
  const m = { Critical: "危急", High: "严重", Medium: "中等", Low: "其他", None: "无" };
  return m[sev] || sev;
}

function renderScanReport(container, data, tag, repoName) {
  // 支持 Harbor v1 / v2 / 后端构造的 report 格式
  let vulns = [];
  let overview = {};

  // 后端构造的 report 格式（优先）
  if (data.summary || data.scan_status) {
    vulns = data.vulnerabilities || [];
    overview = data.summary || {};
  }
  // v1 格式 (数组)
  else if (Array.isArray(data) && data.length > 0 && data[0].vulnerabilities) {
    vulns = data[0].vulnerabilities || [];
    overview = data[0].components?.summary || [];
  }
  // v1 格式 (单个对象)
  else if (data.vulnerabilities) {
    vulns = data.vulnerabilities || [];
    overview = data.components?.summary || [];
  }
  // v2 原始格式: application/vnd.security.vulnerability.report...
  else {
    const rptKey = Object.keys(data).find(k => k.includes("vulnerability.report") || k.includes("scanner.adapter"));
    if (rptKey) {
      const rpt = data[rptKey];
      vulns = rpt.vulnerabilities || [];
      overview = rpt.summary || {};
    }
  }

  const c = overview.critical || 0;
  const h = overview.high || 0;
  const m = overview.medium || 0;
  const l = overview.low || 0;
  const total = overview.total || (c + h + m + l) || 0;

  // 顶部统计
  let html = `<div style="margin-bottom:16px;padding:12px 16px;background:rgba(0,0,0,.25);border-radius:8px;font-size:13px;line-height:1.8">
    <div style="font-weight:600;font-size:15px;margin-bottom:4px">共有缺陷：<span style="color:var(--accent)">${total}</span> 个</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      ${c ? `<span style="color:#e74c3c">危急缺陷 ${c} 个</span>` : ""}
      ${h ? `<span style="color:#e67e22">严重缺陷 ${h} 个</span>` : ""}
      ${m ? `<span style="color:#f1c40f">中等缺陷 ${m} 个</span>` : ""}
      ${l ? `<span style="color:#3498db">其他 ${l} 个</span>` : ""}
    </div>
  </div>`;

  if (!vulns.length) {
    if (!total) {
      html += '<div style="color:#888;text-align:center;padding:20px">✅ 未发现漏洞</div>';
    } else {
      html += '<div style="color:#888;text-align:center;padding:20px">⚠️ 有漏洞概览，但未返回详细列表</div>';
    }
  } else {
    const severityOrder = { Critical: 0, High: 1, Medium: 2, Low: 3 };
    vulns.sort((a, b) => (severityOrder[a.severity] || 9) - (severityOrder[b.severity] || 9));

    html += `<div class="table-wrap" style="max-height:420px;overflow:auto">
      <table class="data-table" style="font-size:12px">
        <thead><tr>
          <th>缺陷码</th>
          <th style="width:70px">严重度</th>
          <th>组件</th>
          <th>当前版本</th>
          <th>修复版本</th>
        </tr></thead>
        <tbody>`;

    html += vulns.map(v => {
      const sev = v.severity || "";
      return `<tr>
        <td style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(v.id || "")}">${escHtml(v.id || "")}</td>
        <td><span class="severity-badge ${severityClass(sev)}">${severityLabel(sev)}</span></td>
        <td style="font-size:11px">${escHtml(v.package || "")}</td>
        <td style="font-size:11px"><code>${escHtml(v.version || "")}</code></td>
        <td style="font-size:11px;color:var(--accent)"><code>${escHtml(v.fix_version || v.fixed_version || "-")}</code></td>
      </tr>`;
    }).join("");

    html += `</tbody></table></div>`;
  }

  // 底部 Harbor 链接
  const harborUrl = data.harbor_url || "";
  if (harborUrl) {
    html += `<div style="margin-top:16px;text-align:right">
      <a href="${escHtml(harborUrl)}" target="_blank" rel="noopener" class="btn btn-blue btn-sm">查看详情 → Harbor</a>
    </div>`;
  }

  container.innerHTML = html;
}

function closeScanDialog() {
  document.getElementById("registry-scan-dialog").style.display = "none";
}

async function triggerHarborScan(artifactId, tag) {
  try {
    const r = await fetch(`/api/registry/scan/trigger/${_registryRepoId}/${encodeURIComponent(tag)}`, {
      method: "POST", headers: A()
    });
    if (handle401(r)) return;
    const d = await r.json();
    if (d.ok) {
      toast("🔄 " + (d.detail || "扫描已触发"), true);
    } else {
      toast("❌ " + (d.error || "触发失败"), false);
    }
  } catch (e) {
    toast("❌ 触发失败: " + e.message, false);
  }
}

// ── 删除 ──

let _delRepoId = 0, _delTag = "";

function confirmDeleteArtifact(tag) {
  _delRepoId = _registryRepoId;
  _delTag = tag;
  document.getElementById("reg-del-repo").textContent = _registryRepoName;
  document.getElementById("reg-del-tag").textContent = tag;
  document.getElementById("reg-del-input").value = "";
  document.getElementById("reg-del-confirm").disabled = true;
  document.getElementById("registry-delete-dialog").style.display = "flex";
}

function closeRegistryDeleteDialog() {
  document.getElementById("registry-delete-dialog").style.display = "none";
  _delRepoId = 0;
  _delTag = "";
}

function checkRegistryDeleteInput() {
  const val = document.getElementById("reg-del-input").value.trim();
  document.getElementById("reg-del-confirm").disabled = val !== _delTag;
}

async function confirmDeleteTag() {
  const val = document.getElementById("reg-del-input").value.trim();
  if (val !== _delTag || !_delRepoId) return;
  try {
    const r = await fetch(`/api/registry/artifacts/${_delRepoId}`, {
      method: "DELETE",
      headers: Object.assign({ "Content-Type": "application/json" }, A()),
      body: JSON.stringify({ repo_id: _delRepoId, tag: _delTag }),
    });
    const d = await r.json();
    if (r.ok) {
      toast(`✅ Tag '${_delTag}' 已删除`, true);
      closeRegistryDeleteDialog();
      if (_registryRepoId) viewArtifacts(_registryRepoId, _registryRepoName, _artifactPage);
    } else {
      toast(`❌ ${d.detail || "删除失败"}`, false);
    }
  } catch (e) {
    toast("❌ 请求失败", false);
  }
}

function escHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── SSH 单机部署 ──

function toggleSshMode() {
  const m = document.getElementById("s-mode").value;
  document.getElementById("s-cmd-wrap").style.display = m === "commands" ? "block" : "none";
  document.getElementById("s-path-wrap").style.display = m === "ansible" ? "block" : "none";
  document.getElementById("s-inv-wrap").style.display = m === "ansible" ? "block" : "none";
}

async function loadSshForm() {
  const r = await fetch("/api/projects", { headers: A() });
  if (handle401(r)) return;
  const d = await r.json();
  const sel = document.getElementById("s-project");
  sel.innerHTML = d.map(p => `<option value="${p.job_name}">${p.job_name}</option>`).join("");
  sel.onchange = () => { loadSshTags(sel.value); viewPipeline(sel.value); };

  const sr = await fetch(`/api/servers?_=${Date.now()}`, { headers: A() });
  if (!handle401(sr)) {
    const srv = await sr.json();
    // SSH 单机部署：可选所有类型服务器
    const list = document.getElementById("s-server-list");
    if (list) {
      list.innerHTML = srv
        .map((s) =>
          `<div class="multi-select-item" onclick="toggleServerItem('s', this)">
            <input type="checkbox" value="${s.id}" data-tags="${(s.tags || '').toLowerCase()}" onchange="updateServerSelection('s')">
            <label>${s.name} (${s.host})</label>
          </div>`
        )
        .join("");
      populateTags('s', srv);
      updateServerSelection('s');
    }
  }
  const br = await fetch("/api/bots", { headers: A() });
  if (br.ok) { const bots = await br.json(); const bsel = document.getElementById("s-bot"); bsel.innerHTML = '<option value="0">— 不通知 —</option>' + bots.map(b => `<option value="${b.id}">${b.name}</option>`).join(""); }
  toggleSshMode();
  const el = document.getElementById("s-project");
  const proj = el.value || (d[0]?.job_name);
  if (proj) { loadSshTags(proj); viewPipeline(proj); }
}



async function doSshDeploy() {
  const tag = document.getElementById("s-tag").value; if (!tag) return toast("没有可用的 Tag", false);
  const sid = getSelectedServerIds('s'); if (!sid) return toast("请选择至少一台服务器", false);
  const body = {
    project: document.getElementById("s-project").value, tag, deploy_type: "ssh",
    server_ids: sid, deploy_mode: document.getElementById("s-mode").value,
    target_path: document.getElementById("s-path").value,
    commands: document.getElementById("s-cmds").value
      + (document.getElementById("s-inv").value ? "|INV|" + document.getElementById("s-inv").value : "")
      + "|FILTER|" + (document.getElementById("s-filter").value || ""),
    bot_id: parseInt(document.getElementById("s-bot").value) || 0,
  };
  const out = document.getElementById("ssh-out"); out.textContent = "$ 正在部署...\n";

  try {
    const r = await fetch("/api/deploy-stream", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, A()),
      body: JSON.stringify(body),
    });
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      while (buffer.includes("\n\n")) {
        const idx = buffer.indexOf("\n\n");
        const line = buffer.substring(0, idx);
        buffer = buffer.substring(idx + 2);

        if (!line.startsWith("data: ")) continue;
        const data = line.substring(6);

        if (data.startsWith("ERROR:")) {
          out.textContent += "\n❌ " + data.substring(6);
          toast("❌ 部署失败", false);
          return;
        } else if (data.startsWith("END:")) {
          const parts = data.substring(4).split(":");
          const success = parts[1] === "true";
          toast(success ? "✅ 部署成功" : "❌ 部署失败", success);
          if (success) loadLogs();
          return;
        } else if (data === ".") {
          continue;
        } else {
          out.textContent += data + "\n";
          out.scrollTop = out.scrollHeight;
        }
      }
    }
  } catch (e) {
    out.textContent += "\n❌ " + e.message;
    toast("❌ 部署失败", false);
  }
}

// ── K8S 部署 ──

function toggleK8sType() {
  const t = document.getElementById("k-cdtype").value;
  document.getElementById("k-path-wrap").style.display = (t === "kubectl" || t === "helm") ? "block" : "none";
  document.getElementById("k-api-wrap").style.display = t === "argocd" ? "block" : "none";
}

async function loadK8sForm() {
  // 项目列表
  const r = await fetch("/api/projects", { headers: A() });
  if (handle401(r)) return;
  const d = await r.json();
  const sel = document.getElementById("k-project");
  sel.innerHTML = d.map(p => `<option value="${p.job_name}">${p.job_name}</option>`).join("");
  sel.onchange = () => { loadK8sTags(sel.value); viewPipeline(sel.value); };

  // 集群列表（过滤 K8s 相关类型）
  const sr = await fetch(`/api/servers?_=${Date.now()}`, { headers: A() });
  if (handle401(sr)) return;
  const srv = await sr.json();
  // K8S 面板：只显示 k8s 类型服务器（argocd/fluxcd 是部署模式，不是服务器类型）
  const k8sServers = srv.filter(s => s.type === "k8s");
  const csel = document.getElementById("k-cluster");
  csel.innerHTML = '<option value="0">— 选择 —</option>' +
    k8sServers.map(s => `<option value="${s.id}">${s.name} (${s.type})</option>`).join("");

  // BOT 列表
  const br = await fetch("/api/bots", { headers: A() });
  if (br.ok) {
    const bots = await br.json();
    const bsel = document.getElementById("k-bot");
    bsel.innerHTML = '<option value="0">— 不通知 —</option>' +
      bots.map(b => `<option value="${b.id}">${b.name}</option>`).join("");
  }

  toggleK8sType();
  const el = document.getElementById("k-project");
  const proj = el.value || (d[0]?.job_name);
  if (proj) { loadK8sTags(proj); viewPipeline(proj); }
}

async function doSshStop() {
  if (!confirm("确定停止？")) return;
  const sid = getSelectedServerIds('s'); if (!sid) return toast("请选择服务器", false);
  const body = { project: document.getElementById("s-project").value, deploy_type: "ssh", server_ids: sid, target_path: document.getElementById("s-path").value };
  document.getElementById("ssh-out").textContent = "停止中…";
  const r = await fetch("/api/stop", { method: "POST", headers: Object.assign({"Content-Type":"application/json"}, A()), body: JSON.stringify(body) });
  const d = await r.json(); document.getElementById("ssh-out").textContent = d.output || ""; toast(d.success ? "✅ 已停止" : "❌ 失败", d.success);
}

async function doK8sStop() {
  if (!confirm("确定停止？")) return;
  const sid = parseInt(document.getElementById("k-cluster").value) || 0; if (!sid) return toast("请选择集群", false);
  const body = { project: document.getElementById("k-project").value, deploy_type: "k8s", server_ids: String(sid), target_path: document.getElementById("k-path").value };
  document.getElementById("k8s-out").textContent = "停止中…";
  const r = await fetch("/api/stop-k8s", { method: "POST", headers: Object.assign({"Content-Type":"application/json"}, A()), body: JSON.stringify(body) });
  const d = await r.json(); document.getElementById("k8s-out").textContent = d.output || ""; toast(d.success ? "✅ 已停止" : "❌ 失败", d.success);
}



async function doK8sDeploy() {
  const tag = document.getElementById("k-tag").value;
  if (!tag) return toast("没有可用的 Tag", false);
  const cid = parseInt(document.getElementById("k-cluster").value) || 0;
  if (!cid) return toast("请选择目标集群", false);

  const body = {
    project: document.getElementById("k-project").value,
    tag: tag,
    cd_type: document.getElementById("k-cdtype").value,
    cluster_id: cid,
    path: document.getElementById("k-path").value,
    api_url: document.getElementById("k-api").value,
    bot_id: parseInt(document.getElementById("k-bot").value) || 0,
  };

  const out = document.getElementById("k8s-out");
  out.textContent = "";

  try {
    const r = await fetch("/api/deploy-k8s-stream", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, A()),
      body: JSON.stringify(body),
    });
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      while (buffer.includes("\n\n")) {
        const idx = buffer.indexOf("\n\n");
        const line = buffer.substring(0, idx);
        buffer = buffer.substring(idx + 2);

        if (!line.startsWith("data: ")) continue;
        const data = line.substring(6);

        if (data.startsWith("ERROR:")) {
          out.textContent += "\n❌ " + data.substring(6);
          toast("❌ 部署失败", false);
          return;
        } else if (data.startsWith("END:")) {
          const parts = data.substring(4).split(":");
          const success = parts[1] === "true";
          toast(success ? "✅ 部署成功" : "❌ 部署失败", success);
          if (success) {
            document.getElementById("k8s-monitor-btn").style.display = "inline-block";
            // 记住当前集群 ID，方便跳转
            _lastDeployedClusterId = parseInt(document.getElementById("k-cluster").value) || 0;
            const clusterName = document.getElementById("k-cluster").selectedOptions[0]?.text || "";
            document.getElementById("k8s-monitor-btn").textContent = "📊 查看 " + clusterName + " 资源占用";
          }
          return;
        } else if (data === ".") {
          continue;
        } else {
          out.textContent += data + "\n";
          out.scrollTop = out.scrollHeight;
        }
      }
    }
  } catch (e) {
    out.textContent += "\n❌ " + e.message;
    toast("❌ 部署失败", false);
  }
}

// ── Web Shell ──

let term = null, shellWs = null, _xtermLoaded = false;

function _loadXtermCSS() {
  if (!document.getElementById("xterm-css")) {
    const link = document.createElement("link");
    link.id = "xterm-css";
    link.rel = "stylesheet";
    link.href = "/static/vendor/xterm/xterm.min.css";
    document.head.appendChild(link);
  }
}

function _loadXtermJS() {
  return new Promise((resolve) => {
    if (window.Terminal) return resolve();
    const s = document.createElement("script");
    s.src = "/static/vendor/xterm/xterm.min.js";
    s.onload = resolve;
    document.head.appendChild(s);
  });
}

function loadShellServers() {
  _loadXtermCSS();
  _loadXtermJS();
  fetch(`/api/servers?_=${Date.now()}`, { headers: A() }).then(r => r.json()).then(d => {
    const sel = document.getElementById("shell-server");
    const cur = sel.value;
    sel.innerHTML = '<option value="0">— 选择服务器 —</option>' +
      d.map(s => `<option value="${s.id}">${s.name} (${s.host})</option>`).join("");
    if (cur) sel.value = cur;
  });
}

function connectShell() {
  const sid = document.getElementById("shell-server").value;
  if (!sid || sid === "0") return toast("请选择服务器", false);
  if (shellWs) disconnectShell();

  if (!term) {
    term = new Terminal({ cursorBlink: true, fontSize: 14, rows: 28, cols: 100, theme: { background: "#000" } });
    term.open(document.getElementById("terminal"));
  }
  term.clear();
  term.writeln("连接中...");

  const proto = location.protocol === "https:" ? "wss" : "ws";
  shellWs = new WebSocket(`${proto}://${location.host}/ws/terminal/${sid}?token=${encodeURIComponent(token)}`);

  shellWs.onopen = () => { term.clear(); term.focus(); shellWs.send(JSON.stringify({type:"resize",cols:term.cols,rows:term.rows})); };
  shellWs.onmessage = (e) => { if (e.data instanceof Blob) e.data.text().then(t => term.write(t)); else term.write(e.data); };
  shellWs.onclose = () => { term.writeln("\r\n🔌 已断开"); shellWs = null; };
  shellWs.onerror = () => { term.writeln("\r\n❌ 连接失败"); };

  term.onData(data => { if (shellWs && shellWs.readyState === WebSocket.OPEN) shellWs.send(data); });
  term.onResize(({cols, rows}) => { if (shellWs && shellWs.readyState === WebSocket.OPEN) shellWs.send(JSON.stringify({type:"resize",cols,rows})); });
}

function disconnectShell() {
  if (shellWs) { shellWs.close(); shellWs = null; }
}

async function uploadFile() {
  const sid = document.getElementById("shell-server").value;
  if (!sid || sid === "0") return toast("请选择服务器", false);
  const file = document.getElementById("scp-file").files[0];
  if (!file) return toast("请选择文件", false);
  const path = document.getElementById("scp-path").value || "/tmp";

  const form = new FormData();
  form.append("file", file);
  form.append("path", path);

  try {
    const r = await fetch(`/api/upload/${sid}`, { method: "POST", headers: A(), body: form });
    const d = await r.json();
    toast(d.success ? `✅ 已上传到 ${d.path}` : `❌ ${d.detail || "失败"}`, d.success);
  } catch(e) {
    toast("❌ 上传失败", false);
  }
}

// ── Init ──

if (token) {
  document.getElementById("login-page").style.display = "none";
  document.getElementById("main-app").style.display = "block";
  showPanel("ci");
}
document.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && document.getElementById("login-page").style.display !== "none") doLogin();
});
