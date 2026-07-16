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

function showPanel(n) {
  document.querySelectorAll(".sidebar .item").forEach((i) => i.classList.remove("active"));
  document.querySelector(`[data-panel="${n}"]`).classList.add("active");
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("show"));
  document.getElementById("panel-" + n).classList.add("show");
  if (n === "ci") loadCI();
  if (n === "servers") loadServers();
  if (n === "deploy") loadDeployForm();
  if (n === "k8s") loadK8sForm();
  if (n === "shell") loadShellServers();
  if (n === "logs") loadLogs();
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
            <button class="btn btn-green btn-sm" onclick="quickDeploy('${p.job_name}','${p.latest_tag}')">部署</button>
            <button class="btn btn-blue btn-sm" onclick="viewPipeline('${p.job_name}')">CI状态</button>
          </td>
        </tr>`
    )
    .join("");
}

// ── CI Pipeline 状态 ──

let _vpSeq = 0;

async function viewPipeline(project) {
  if (!project) return;
  const seq = ++_vpSeq;
  document.getElementById("pipeline-status-card").style.display = "block";

  const statusEl = document.getElementById("pipeline-stages");
  statusEl.innerHTML = '<span style="color:#888;font-size:12px">加载中…</span>';
  document.getElementById("d-tag").innerHTML = '<option value="">加载中…</option>';

  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(project)}/pipeline`);
    const d = await r.json();
    if (seq !== _vpSeq) return;
    if (r.ok && d.pipeline && d.latest_tag) {
      const p = d.pipeline;
      statusEl.innerHTML =
        '<div style="padding:12px;background:#1b3a1b;border-radius:6px;border:1px solid #388e3c">' +
        '<span style="color:#81c784;font-weight:600">✅ CI 已完成</span>' +
        '<div style="margin-top:6px;font-size:12px;color:#999">' +
        (p.iid ? 'Pipeline <b>#' + p.iid + '</b> · ' : '') +
        'Tag <b>' + d.latest_tag + '</b>' +
        (p.created_at ? ' · ' + p.created_at : '') +
        '</div></div>';
    } else {
      statusEl.innerHTML = '<span style="color:#888;font-size:12px">暂无 CI 数据</span>';
    }

    const tr = await fetch(`/api/projects/${encodeURIComponent(project)}/tags`);
    const tags = await tr.json();
    if (seq !== _vpSeq) return;
    const tagSel = document.getElementById("d-tag");
    if (tags.length) {
      tagSel.innerHTML = tags.map(t => `<option value="${t.tag}">${t.tag}</option>`).join("");
      tagSel.value = tags[0].tag;
    } else {
      tagSel.innerHTML = '<option value="">无可用 Tag</option>';
    }
  } catch(e) {
    if (seq === _vpSeq) {
      statusEl.innerHTML = '<span style="color:#888;font-size:12px">暂无 CI 数据</span>';
    }
  }
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
         <td><button class="btn btn-red btn-sm" onclick="delServer(${s.id})">删除</button></td></tr>`
    )
    .join("");
  // 部署面板多选
  const sel = document.getElementById("d-server");
  if (sel) {
    sel.innerHTML =
      '<option value="0">— 请选择 —</option>' +
      '<option value="*">🔄 全部服务器</option>' +
      d.map((s) => `<option value="${s.id}">${s.name} (${s.host})</option>`).join("");
  }
}

async function addServer() {
  const n = document.getElementById("sv-name").value.trim();
  const h = document.getElementById("sv-host").value.trim();
  const u = document.getElementById("sv-user").value.trim() || "root";
  const p = document.getElementById("sv-pass").value;
  const t = document.getElementById("sv-tags").value.trim();
  const tp = document.getElementById("sv-type").value;
  if (!n || !h) return toast("填名称和主机", false);
  const r = await fetch("/api/servers", {
    method: "POST",
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
  toast(d.success ? "已添加" : "失败", d.success);
  if (d.success) loadServers();
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
    if (e.target.id === "d-project") {
      clearTimeout(_timer);
      _timer = setTimeout(() => {
        document.getElementById("d-project").dataset.last = e.target.value;
        viewPipeline(e.target.value);
      }, 100);
    }
  });
});

function quickDeploy(project, tag) {
  showPanel("deploy");
  document.getElementById("d-project").value = project;
  viewPipeline(project);  // 自动加载 CI 状态 + tag 列表
}

const MODE_OPTIONS = {
  ssh: [
    { value: "commands", label: "自定义命令" },
    { value: "ansible", label: "Ansible Playbook" },
  ],
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
    pathLabel.textContent = "目录路径";
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
  const sid = document.getElementById("d-server").value;
  const body = {
    project: document.getElementById("d-project").value,
    tag: tag,
    deploy_type: document.getElementById("d-type").value,
    server_ids: sid === "*" ? "" : sid,   // * = 全部, 空字符串 = all
    target_path: document.getElementById("d-path").value,
    deploy_mode: document.getElementById("d-mode").value,
    commands: document.getElementById("d-cmds").value,
    yaml_content: document.getElementById("d-yaml").value,
    bot_id: parseInt(document.getElementById("d-bot").value) || 0,
  };
  const out = document.getElementById("deploy-out");
  out.textContent = "$ 正在部署...\n";
  const r = await fetch("/api/deploy", {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, A()),
    body: JSON.stringify(body),
  });
  const d = await r.json();
  const results = d.results || [d.result || d];
  let text = "";
  results.forEach((r, i) => {
    const host = r.host || r.server_id || "?";
    const icon = r.status === "ok" ? "✅" : "❌";
    text += `\n${icon} [${host}] ${r.status}\n`;
    if (r.output) {
      r.output.split("\n").forEach(line => {
        text += line.startsWith("[") ? `  ${line}\n` : `  > ${line}\n`;
      });
    }
  });
  out.textContent = text.trim();
  toast(d.success ? "✅ 部署成功" : "❌ 部署失败", d.success);
  if (d.success) loadLogs();
}

async function doStop() {
  if (!confirm("确定停止？")) return;
  const body = {
    project: document.getElementById("d-project").value,
    deploy_type: document.getElementById("d-type").value,
    server_ids: document.getElementById("d-server").value,
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

async function loadLogs() {
  const r = await fetch("/api/deploy-logs");
  const d = await r.json();
  document.getElementById("log-tbody").innerHTML = d
    .map(
      (l) =>
        `<tr style="cursor:pointer" onclick="showLogDetail(this, '${escape(l.output || '')}')">
          <td>${l.created_at}</td><td>${l.project}</td><td>${l.tag}</td><td>${l.deploy_type}</td>
          <td><span class="badge badge-${l.status === "ok" ? "ok" : l.status === "failed" ? "err" : "pend"}">${l.status}</span></td>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${l.output || ""}</td>
        </tr>`
    )
    .join("");
}

function showLogDetail(tr, output) {
  // 如果已展开，收起
  const existing = tr.nextElementSibling;
  if (existing && existing.classList.contains("log-detail")) {
    existing.remove();
    return;
  }
  const detail = document.createElement("tr");
  detail.className = "log-detail";
  detail.innerHTML = `<td colspan="6"><pre style="margin:8px 0;font-size:12px;white-space:pre-wrap;max-height:300px;overflow-y:auto;background:#111;color:#00ff00;padding:10px;border-radius:4px;font-family:monospace">${output || "(无输出)"}</pre></td>`;
  tr.parentNode.insertBefore(detail, tr.nextSibling);
}

function escape(s) {
  return s.replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/\n/g, "\\n").replace(/\r/g, "");
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

// ── K8S 部署 ──

function toggleK8sType() {
  const t = document.getElementById("k-cdtype").value;
  document.getElementById("k-path-wrap").style.display = t === "kubectl" ? "block" : "none";
  document.getElementById("k-api-wrap").style.display = t === "argocd" || t === "fluxcd" ? "block" : "none";
}

async function loadK8sForm() {
  // 项目列表
  const r = await fetch("/api/projects");
  const d = await r.json();
  const sel = document.getElementById("k-project");
  sel.innerHTML = d.map(p => `<option value="${p.job_name}">${p.job_name}</option>`).join("");
  sel.onchange = () => loadK8sTags(sel.value);

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
  if (d.length) loadK8sTags(d[0].job_name);
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
  out.textContent = "部署中…";
  const r = await fetch("/api/deploy-k8s", {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, A()),
    body: JSON.stringify(body),
  });
  const d = await r.json();
  out.textContent = d.output || JSON.stringify(d, null, 2);
  toast(d.success ? "✅ 部署成功" : "❌ 部署失败", d.success);
}

// ── Web Shell ──

let term = null, shellWs = null, _xtermLoaded = false;

function _loadXtermCSS() {
  if (!document.getElementById("xterm-css")) {
    const link = document.createElement("link");
    link.id = "xterm-css";
    link.rel = "stylesheet";
    link.href = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css";
    document.head.appendChild(link);
  }
}

function _loadXtermJS() {
  return new Promise((resolve) => {
    if (window.Terminal) return resolve();
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js";
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

  shellWs.onopen = () => { term.clear(); term.focus(); };
  shellWs.onmessage = (e) => { if (e.data instanceof Blob) e.data.text().then(t => term.write(t)); else term.write(e.data); };
  shellWs.onclose = () => { term.writeln("\r\n🔌 已断开"); shellWs = null; };
  shellWs.onerror = () => { term.writeln("\r\n❌ 连接失败"); };

  term.onData(data => { if (shellWs && shellWs.readyState === WebSocket.OPEN) shellWs.send(data); });
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
