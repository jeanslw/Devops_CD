// CD Service Dashboard — 前端逻辑

let token = sessionStorage.getItem("cd_token") || "";

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
  const allParents = document.querySelectorAll(".item-parent");
  for (const p of allParents) {
    if (p.textContent.includes("资源监控") && p.textContent.includes("▸")) {
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
  if (n.startsWith("monitor-")) expandMonitorSubmenu();
  if (n === "ci") loadCI();
  if (n === "servers") loadServers();
  if (n === "ssh") loadSshForm();
  if (n === "deploy") loadDeployForm();
  if (n === "k8s") loadK8sForm();
  if (n === "shell") loadShellServers();
  if (n === "logs") loadLogs();
  if (n === "monitor-system") loadMonitorSystem();
  if (n === "monitor-app") loadMonitorApp();
  if (n === "bots") loadBots();
}

function toast(msg, ok) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast toast-" + (ok ? "ok" : "err") + " show";
  setTimeout(() => el.classList.remove("show"), 3000);
}

// ── CI 项目列表 ──

async function loadCI() {
  const r = await fetch("/api/projects");
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
            <select onchange="quickDeploySelect(this,'${p.job_name}','${p.latest_tag}')" style="width:auto;display:inline;margin:0;padding:3px 6px">
              <option value="">部署到…</option>
              <option value="ssh">单机</option>
              <option value="deploy">Docker</option>
              <option value="k8s">K8S</option>
            </select>
            <button class="btn btn-blue btn-sm" onclick="viewPipelineRow(this,'${p.job_name}')">CI状态</button>
          </td>
        </tr>`
    )
    .join("");
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
    const r = await fetch(`/api/projects/${encodeURIComponent(project)}/pipeline`);
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
  _setTags("d-tag", []); _setTags("s-tag", []); _setTags("k-tag", []);
  document.getElementById("s-tag").innerHTML = '<option value="">加载中…</option>';
  document.getElementById("d-tag").innerHTML = '<option value="">加载中…</option>';
  document.getElementById("k-tag").innerHTML = '<option value="">加载中…</option>';

  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(project)}/pipeline`);
    const d = await r.json();
    if (seq !== _vpSeq) return;
    const tag = d.latest_tag || "";
    const iid = d.pipeline?.iid;
    const created = d.pipeline?.created_at || "";
    _setCI("", tag, iid, created);
    _setCI("ssh-", tag, iid, created);
    _setCI("k-", tag, iid, created);
  } catch(e) {}

  try {
    const tr = await fetch(`/api/projects/${encodeURIComponent(project)}/tags`);
    const tags = await tr.json();
    if (seq !== _vpSeq) return;
    _setTags("d-tag", tags);
    _setTags("s-tag", tags);
    _setTags("k-tag", tags);
  } catch(e) {}
}

// ── 服务器管理 ──

async function loadServers() {
  const r = await fetch("/api/servers", { headers: A() });
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
  // 部署面板多选
  const list = document.getElementById("d-server-list");
  if (list) {
    list.innerHTML = d
      .map(
        (s) =>
          `<div class="multi-select-item" onclick="toggleServerItem('d', this)">
            <input type="checkbox" value="${s.id}" data-tags="${(s.tags || '').toLowerCase()}" onchange="updateServerSelection('d')">
            <label>${s.name} (${s.host})</label>
          </div>`
      )
      .join("");
    // 填充标签栏
    populateTags('d', d);
    updateServerSelection('d');
  }
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

async function addServer() {
  const editId = document.getElementById("sv-edit-id").value;
  const n = document.getElementById("sv-name").value.trim();
  const h = document.getElementById("sv-host").value.trim();
  const u = document.getElementById("sv-user").value.trim() || "root";
  const p = document.getElementById("sv-pass").value;
  const t = document.getElementById("sv-tags").value.trim();
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
      password: p,
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
  const r = await fetch("/api/servers", { headers: A() });
  if (handle401(r)) return;
  const servers = await r.json();
  const s = servers.find(srv => srv.id === id);
  if (!s) return toast("找不到该服务器", false);

  document.getElementById("sv-edit-id").value = s.id;
  document.getElementById("sv-name").value = s.name;
  document.getElementById("sv-host").value = s.host + ":" + s.port;
  document.getElementById("sv-user").value = s.user;
  document.getElementById("sv-pass").value = s.password || "";
  document.getElementById("sv-tags").value = s.tags || "";
  document.getElementById("sv-type").value = s.type || "ssh";

  document.getElementById("sv-save-btn").textContent = "💾 保存";
  document.getElementById("sv-cancel-btn").style.display = "inline-block";
}

function cancelEdit() {
  document.getElementById("sv-edit-id").value = "";
  document.getElementById("sv-name").value = "";
  document.getElementById("sv-host").value = "";
  document.getElementById("sv-user").value = "root";
  document.getElementById("sv-pass").value = "";
  document.getElementById("sv-tags").value = "";
  document.getElementById("sv-type").value = "ssh";
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

// ── 部署 ──

let _deployFormReady = false;

async function loadDeployForm() {
  const r = await fetch("/api/projects");
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
  loadServers();
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

// ── SSH 单机部署 ──

function toggleSshMode() {
  const m = document.getElementById("s-mode").value;
  document.getElementById("s-cmd-wrap").style.display = m === "commands" ? "block" : "none";
  document.getElementById("s-path-wrap").style.display = m === "ansible" ? "block" : "none";
  document.getElementById("s-inv-wrap").style.display = m === "ansible" ? "block" : "none";
}

async function loadSshForm() {
  const r = await fetch("/api/projects");
  const d = await r.json();
  const sel = document.getElementById("s-project");
  sel.innerHTML = d.map(p => `<option value="${p.job_name}">${p.job_name}</option>`).join("");
  sel.onchange = () => { loadSshTags(sel.value); viewPipeline(sel.value); };

  const sr = await fetch("/api/servers", { headers: A() });
  if (!handle401(sr)) {
    const srv = await sr.json();
    const filtered = srv.filter(s => ["ssh","docker"].includes(s.type));
    const list = document.getElementById("s-server-list");
    if (list) {
      list.innerHTML = filtered
        .map((s) =>
          `<div class="multi-select-item" onclick="toggleServerItem('s', this)">
            <input type="checkbox" value="${s.id}" data-tags="${(s.tags || '').toLowerCase()}" onchange="updateServerSelection('s')">
            <label>${s.name} (${s.host})</label>
          </div>`
        )
        .join("");
      populateTags('s', filtered);
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

async function loadSshTags(project) {
  const sel = document.getElementById("s-tag"); sel.innerHTML = '<option value="">加载中…</option>';
  try { const r = await fetch(`/api/projects/${encodeURIComponent(project)}/tags`); const tags = await r.json();
    sel.innerHTML = tags.length ? tags.map(t => `<option value="${t.tag}">${t.tag}</option>`).join("") : '<option value="">无可用 Tag</option>';
    if (tags.length) sel.value = tags[0].tag;
  } catch(e) { sel.innerHTML = '<option value="">无可用 Tag</option>'; }
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
  const r = await fetch("/api/projects");
  const d = await r.json();
  const sel = document.getElementById("k-project");
  sel.innerHTML = d.map(p => `<option value="${p.job_name}">${p.job_name}</option>`).join("");
  sel.onchange = () => { loadK8sTags(sel.value); viewPipeline(sel.value); };

  // 集群列表（过滤 K8s 相关类型）
  const sr = await fetch("/api/servers", { headers: A() });
  if (handle401(sr)) return;
  const srv = await sr.json();
  const k8sServers = srv.filter(s => ["k8s", "argocd", "fluxcd"].includes(s.type));
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

async function loadK8sTags(project) {
  const sel = document.getElementById("k-tag");
  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(project)}/tags`);
    const tags = await r.json();
    if (tags.length) {
      sel.innerHTML = tags.map(t => `<option value="${t.tag}">${t.tag}</option>`).join("");
      sel.value = tags[0].tag;
    } else {
      sel.innerHTML = '<option value="">无可用 Tag</option>';
    }
  } catch(e) { sel.innerHTML = '<option value="">无可用 Tag</option>'; }
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
  fetch("/api/servers", { headers: A() }).then(r => r.json()).then(d => {
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
  shellWs = new WebSocket(`${proto}://${location.host}/ws/terminal/${sid}`);

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
