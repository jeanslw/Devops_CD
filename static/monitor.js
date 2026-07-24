// CD Service Dashboard — 资源监控（系统资源 + 应用资源）
// 依赖 app.js 中的：A(), handle401(), showPanel()

// ── 共享状态 ──

let _monitorServers = [];
let _monitorPods = [];
let _currentSystemServerId = 0;
let _currentAppServerId = 0;
let _currentAppType = "";
let _lastDeployedClusterId = 0;

const TYPE_LABELS = { k8s: "☸️ K8S", docker: "🐳 Docker", ssh: "🖥️ Linux" };

// ── 工具函数 ──

function fmtUptime(secs) {
  if (!secs || secs <= 0) return "?";
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

async function checkMonitorEnabled() {
  try {
    const sr = await fetch("/api/monitor/status");
    const sd = await sr.json();
    return sd.enabled;
  } catch(e) { return false; }
}

function _buildCapHtml(s, viewType) {
  // 根据视图类型动态生成"监控能力"标签
  const mType = s.monitor_type || "unknown";
  const fullError = (s.error || s.hint || "").replace(/"/g, "&quot;");

  if (viewType === "system") {
    // 系统资源视图：展示 SSH 连接状态（所有类型通用）
    if (s.status === "error") {
      return `<span style="color:#e57373;font-size:12px" title="SSH 连接失败: ${fullError}">❌ 连接失败</span>`;
    }
    if (s.status === "unavailable") {
      return `<span style="color:#ffab40;font-size:12px" title="${fullError}">⚠️ ${s.hint || "基础命令缺失"}</span>`;
    }
    return `<span style="color:#81c784;font-size:12px">✅ SSH 正常</span>`;
  }

  // 应用资源视图：按类型展示专业监控能力
  switch (mType) {
    case "k8s": {
      const parts = [];
      if (s.has_metrics_server) parts.push("Metrics-Server");
      if (s.has_prometheus) parts.push("Prometheus");
      if (parts.length === 2) return `<span style="color:#81c784;font-size:12px">✅ ${parts.join(" + ")}</span>`;
      if (parts.length === 1) {
        const isMetrics = parts[0] === "Metrics-Server";
        return isMetrics
          ? `<span style="color:#81c784;font-size:12px">✅ ${parts[0]}</span>`
          : `<span style="color:#ffab40;font-size:12px" title="仅有 Prometheus，无法获取 kubectl top 数据">⚠️ Prometheus（无 top）</span>`;
      }
      // 都无
      if (s.status === "error") return `<span style="color:#e57373;font-size:12px" title="${fullError}">❌ 连接失败</span>`;
      return `<span style="color:#e57373;font-size:12px" title="未安装 metrics-server 或 Prometheus，kubectl top 不可用">❌ 无监控组件</span>`;
    }
    case "docker": {
      if (s.status === "available") return `<span style="color:#81c784;font-size:12px">✅ Docker Stats</span>`;
      if (s.status === "error") return `<span style="color:#e57373;font-size:12px" title="${fullError}">❌ 连接失败</span>`;
      return `<span style="color:#e57373;font-size:12px" title="${fullError}">❌ Docker 不可用</span>`;
    }
    case "ssh":
      return `<span style="color:#667;font-size:12px">— 请查看系统资源</span>`;
    default: {
      if (s.status === "error") return `<span style="color:#e57373;font-size:12px" title="${fullError}">❌ 连接失败</span>`;
      return `<span style="color:#667;font-size:12px">未知</span>`;
    }
  }
}

async function loadMonitorServers(tbodyId, btnLabel, viewType) {
  try {
    const r = await fetch("/api/monitor/servers", { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    _monitorServers = d.servers || [];

    const tbody = document.getElementById(tbodyId);
    if (!_monitorServers.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888">没有服务器，请在「服务器管理」中添加</td></tr>';
      return;
    }

    tbody.innerHTML = _monitorServers.map(s => {
      const mType = s.monitor_type || "unknown";
      const typeLabel = TYPE_LABELS[mType] || s.type;
      const capHtml = _buildCapHtml(s, viewType);
      const canUse = viewType === "system"
        ? (s.status === "available" || s.status === "unavailable")  // 系统资源：SSH 通了就能用
        : (s.status === "available");  // 应用资源：需要对应组件
      const statusBadge = canUse
        ? '<span class="badge badge-ok">可用</span>'
        : (s.status === "error" ? `<span class="badge badge-err" title="${(s.error||'').replace(/"/g,'&quot;')}">错误</span>` : '<span class="badge badge-pend">不可用</span>');
      const btnText = btnLabel.includes("System") ? "查看系统资源" : "查看应用资源";
      return `<tr>
        <td><strong>${s.name}</strong></td>
        <td>${s.host}:${s.port}</td>
        <td>${typeLabel}</td>
        <td>${capHtml}</td>
        <td>${statusBadge}</td>
        <td>${canUse ? `<button class="btn btn-blue btn-sm" onclick="${btnLabel}(${s.id},'${mType}','${(s.name||'').replace(/'/g,"\\'")}')">${btnText}</button>` : '<button class="btn btn-sm" disabled style="opacity:0.4">不可用</button>'}</td>
      </tr>`;
    }).join("");
  } catch(e) {
    document.getElementById(tbodyId).innerHTML = '<tr><td colspan="6" style="text-align:center;color:#e57373">加载失败: ' + e.message + '</td></tr>';
  }
}

// ═══════════════════════════════════════
//  🖥️ 系统资源
// ═══════════════════════════════════════

async function loadMonitorSystem() {
  const enabled = await checkMonitorEnabled();
  document.getElementById("monitor-system-disabled-card").style.display = enabled ? "none" : "block";
  document.getElementById("monitor-system-enabled").style.display = enabled ? "block" : "none";
  if (!enabled) return;

  document.getElementById("monitor-system-detail").style.display = "none";
  await loadMonitorServers("monitor-system-server-tbody", "viewSystemDetail", "system");
}

function viewSystemDetail(sid) {
  _currentSystemServerId = sid;
  document.getElementById("monitor-system-detail").style.display = "block";
  document.getElementById("monitor-system-detail").scrollIntoView({ behavior: "smooth" });
  loadSystemInfo(sid);
}

// ═══════════════════════════════════════
//  📦 应用资源
// ═══════════════════════════════════════

async function loadMonitorApp() {
  const enabled = await checkMonitorEnabled();
  document.getElementById("monitor-app-disabled-card").style.display = enabled ? "none" : "block";
  document.getElementById("monitor-app-enabled").style.display = enabled ? "block" : "none";
  if (!enabled) return;

  document.getElementById("monitor-app-detail").style.display = "none";
  await loadMonitorServers("monitor-app-server-tbody", "viewAppDetail", "app");
}

function viewAppDetail(sid, mtype, name) {
  _currentAppServerId = sid;
  _currentAppType = mtype;

  document.getElementById("monitor-app-detail").style.display = "block";

  document.getElementById("monitor-k8s-app").style.display = "none";
  document.getElementById("monitor-docker-app").style.display = "none";
  document.getElementById("monitor-ssh-app").style.display = "none";
  document.getElementById("monitor-app-section").style.display = "none";

  if (mtype === "k8s") {
    document.getElementById("monitor-app-section").style.display = "block";
    document.getElementById("monitor-k8s-app").style.display = "block";
    document.getElementById("monitor-cluster-label").textContent = "集群: " + name;
    loadK8sPods(sid);
    loadMonitorNodes(sid);
  } else if (mtype === "docker") {
    document.getElementById("monitor-app-section").style.display = "block";
    document.getElementById("monitor-docker-app").style.display = "block";
    loadDockerInfo(sid);
  } else if (mtype === "ssh") {
    document.getElementById("monitor-app-section").style.display = "block";
    document.getElementById("monitor-ssh-app").style.display = "block";
  }
  document.getElementById("monitor-app-detail").scrollIntoView({ behavior: "smooth" });
}

// ── K8S 应用资源 ──

async function loadMonitorNodes(sid) {
  const container = document.getElementById("monitor-nodes");
  container.innerHTML = '<span style="color:#888">加载中…</span>';
  try {
    const r = await fetch(`/api/monitor/nodes/${sid}`, { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();

    if (!d.has_metrics || !d.nodes.length) {
      container.innerHTML = '<div style="color:#e57373;padding:8px;background:#2a1a1a;border-radius:4px">⚠️ 无法获取 Node 资源数据。' + (d.hint ? '<br><small>' + d.hint + '</small>' : '') + '</div>';
      return;
    }

    container.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px">` +
      d.nodes.map(n => {
      return `<div style="padding:12px;background:#1a1a2e;border-radius:6px;border:1px solid #333">
        <div style="font-weight:600;margin-bottom:8px;color:#64b5f6">🖥️ ${n.name}</div>
        <div style="font-size:12px;color:#aaa;margin-bottom:4px">CPU: <span style="color:#ffab40">${n.cpu}</span> / ${n.capacity_cpu}</div>
        <div style="font-size:12px;color:#aaa;margin-bottom:4px">内存: <span style="color:#81c784">${n.memory}</span> / ${n.capacity_memory}</div>
        <div style="font-size:11px;color:#666">最大 Pods: ${n.max_pods}</div>
      </div>`;
    }).join("") + `</div>`;
  } catch(e) {
    container.innerHTML = '<div style="color:#e57373">加载失败: ' + e.message + '</div>';
  }
}

async function loadK8sPods(sid) {
  try {
    const r = await fetch(`/api/monitor/pods/${sid}`, { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    _monitorPods = d.pods || [];
    const nsSel = document.getElementById("monitor-ns-filter");
    nsSel.innerHTML = '<option value="">全部命名空间</option>' +
      (d.namespaces || []).map(ns => `<option value="${ns}">${ns}</option>`).join("");
    renderMonitorPods(_monitorPods);
  } catch(e) {
    document.getElementById("monitor-pod-tbody").innerHTML = '<tr><td colspan="7" style="text-align:center;color:#e57373">加载失败</td></tr>';
  }
}

async function loadMonitorPods() {
  if (!_currentAppServerId) return;
  const ns = document.getElementById("monitor-ns-filter").value;
  try {
    const r = await fetch(`/api/monitor/pods/${_currentAppServerId}?namespace=${encodeURIComponent(ns)}`, { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    _monitorPods = d.pods || [];
    renderMonitorPods(_monitorPods);
  } catch(e) {
    document.getElementById("monitor-pod-tbody").innerHTML = '<tr><td colspan="7" style="text-align:center;color:#e57373">加载失败</td></tr>';
  }
}

function filterMonitorPods() {
  const query = (document.getElementById("monitor-pod-search").value || "").toLowerCase();
  const filtered = query ? _monitorPods.filter(p => p.name.toLowerCase().includes(query)) : _monitorPods;
  renderMonitorPods(filtered);
}

function renderMonitorPods(pods) {
  const tbody = document.getElementById("monitor-pod-tbody");
  if (!pods.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#888">' +
      (_monitorPods.length ? '无匹配 Pod' : '未获取到 Pod 资源数据，请确认 metrics-server 已安装') + '</td></tr>';
    return;
  }
  tbody.innerHTML = pods.map(p => {
    const statusClass = p.status === "Running" ? "badge-ok" : (p.status === "Pending" ? "badge-pend" : "badge-err");
    return `<tr style="cursor:pointer" onclick="viewPodDetail(${_currentAppServerId},'${p.namespace}','${p.name}')" title="点击查看详情">
      <td>${p.namespace}</td><td><strong>${p.name}</strong></td>
      <td style="color:#ffab40">${p.cpu || "?"}</td><td style="color:#81c784">${p.memory || "?"}</td>
      <td><span class="badge ${statusClass}">${p.status}</span></td>
      <td>${p.restarts || "0"}</td><td style="font-size:11px;color:#888">${p.node || "?"}</td>
    </tr>`;
  }).join("");
}

async function viewPodDetail(sid, ns, name) {
  const existing = document.getElementById("pod-detail-modal");
  if (existing) existing.remove();

  const modal = document.createElement("div");
  modal.id = "pod-detail-modal";
  modal.style.cssText = "position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;z-index:9999";
  modal.innerHTML = `<div style="background:#1a1a2e;border-radius:8px;padding:20px;max-width:700px;width:90%;max-height:80vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0">📦 ${ns}/${name}</h3>
      <button style="background:none;border:none;color:#e57373;font-size:18px;cursor:pointer" onclick="this.closest('#pod-detail-modal').remove()">✕</button>
    </div>
    <div id="pod-detail-content" style="color:#888;font-size:12px">加载中…</div>
  </div>`;
  modal.onclick = function(e) { if (e.target === modal) modal.remove(); };
  document.body.appendChild(modal);

  try {
    const r = await fetch(`/api/monitor/pod-detail/${sid}?namespace=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`, { headers: A() });
    const d = await r.json();
    document.getElementById("pod-detail-content").innerHTML = `
      <div style="margin-bottom:10px"><strong>资源使用:</strong> <pre style="background:#111;color:#00ff00;padding:8px;border-radius:4px;margin:4px 0;font-size:12px">${d.top || "无法获取"}</pre></div>
      <div style="margin-bottom:10px"><strong>最近日志:</strong> <pre style="background:#111;color:#ccc;padding:8px;border-radius:4px;margin:4px 0;font-size:11px;max-height:200px;overflow-y:auto;white-space:pre-wrap">${d.logs || "无"}</pre></div>
      <div><strong>Describe:</strong> <pre style="background:#111;color:#aaa;padding:8px;border-radius:4px;margin:4px 0;font-size:11px;max-height:200px;overflow-y:auto;white-space:pre-wrap">${d.describe || "无"}</pre></div>`;
  } catch(e) {
    document.getElementById("pod-detail-content").innerHTML = '<span style="color:#e57373">加载失败: ' + e.message + '</span>';
  }
}

// ── 系统资源（所有类型通用）──

async function loadSystemInfo(sid) {
  const container = document.getElementById("monitor-system-content");
  container.innerHTML = '<span style="color:#888">加载中…</span>';
  try {
    const r = await fetch(`/api/monitor/system/${sid}`, { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    if (!d.success) { container.innerHTML = '<div style="color:#e57373">获取失败</div>'; return; }

    const s = d.system;
    const memUsed = parseFloat(s.memory_percent) || 0;
    const memColor = memUsed > 90 ? "#e57373" : memUsed > 70 ? "#ffab40" : "#81c784";

    container.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-bottom:12px">
        <div style="padding:14px;background:#1a1a2e;border-radius:6px;border:1px solid #333">
          <div style="font-weight:600;margin-bottom:8px;color:#64b5f6">🖥️ 系统信息</div>
          <div style="font-size:12px;color:#aaa;line-height:1.8">
            <div>系统: ${s.os || "?"}</div>
            <div>CPU 核心: ${s.cpu_cores}</div>
            <div>运行时间: ${fmtUptime(s.uptime_seconds)}</div>
            <div>启动时间: ${s.uptime_since || "?"}</div>
          </div>
        </div>
        <div style="padding:14px;background:#1a1a2e;border-radius:6px;border:1px solid #333">
          <div style="font-weight:600;margin-bottom:8px;color:#64b5f6">📈 负载</div>
          <div style="font-size:12px;color:#aaa;line-height:1.8">
            <div>Load: <span style="color:#ffab40">${s.load || "?"}</span></div>
            <div>内存: <span style="color:${memColor}">${s.memory_used || "?"} / ${s.memory_total || "?"} (${s.memory_percent || "?"}%)</span></div>
            <div>磁盘 /: <span style="color:#ffab40">${s.disk_used || "?"} / ${s.disk_total || "?"} (${s.disk_percent || "?"})</span></div>
          </div>
        </div>
      </div>
      ${d.top_processes.length ? `
      <h4 style="margin:0 0 6px 0;font-size:13px;color:#888">🔝 CPU 占用 Top 5</h4>
      <table style="margin-bottom:0"><thead><tr><th>PID</th><th>CPU%</th><th>MEM%</th><th>命令</th></tr></thead>
      <tbody>${d.top_processes.map(p => `<tr><td>${p.pid}</td><td style="color:#ffab40">${p.cpu}</td><td style="color:#81c784">${p.mem}</td><td style="font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.cmd}</td></tr>`).join("")}</tbody></table>
      ` : ""}
    `;
  } catch(e) {
    container.innerHTML = '<div style="color:#e57373">加载失败: ' + e.message + '</div>';
  }
}

// ── Docker 应用资源 ──

async function loadDockerInfo(sid) {
  const container = document.getElementById("monitor-docker-content");
  container.innerHTML = '<span style="color:#888">加载中…</span>';
  try {
    const r = await fetch(`/api/monitor/docker/${sid}`, { headers: A() });
    if (handle401(r)) return;
    const d = await r.json();
    if (!d.success) { container.innerHTML = '<div style="color:#e57373">获取失败</div>'; return; }

    if (!d.containers.length) {
      container.innerHTML = '<div style="color:#888;padding:8px 0">无运行中的容器</div>';
      return;
    }

    container.innerHTML = `
      <h3 style="margin:0 0 8px 0">🐳 容器资源</h3>
      <div style="max-height:400px;overflow-y:auto">
      <table><thead><tr><th>容器名</th><th>CPU</th><th>内存</th><th>内存%</th><th>网络 IO</th><th>磁盘 IO</th></tr></thead>
      <tbody>${d.containers.map(c => `<tr>
        <td><strong>${c.name}</strong></td>
        <td style="color:#ffab40">${c.cpu}</td>
        <td style="color:#81c784">${c.memory}</td>
        <td>${c.memory_percent}</td>
        <td style="font-size:11px;color:#888">${c.net_io}</td>
        <td style="font-size:11px;color:#888">${c.block_io}</td>
      </tr>`).join("")}</tbody></table></div>`;
  } catch(e) {
    container.innerHTML = '<div style="color:#e57373">加载失败: ' + e.message + '</div>';
  }
}

// ── 部署跳转（K8S 部署完成后跳转到应用资源）──

function jumpToAppMonitor() {
  expandMonitorSubmenu();
  showPanel("monitor-app");
  document.getElementById("k8s-monitor-btn").style.display = "none";
  if (_lastDeployedClusterId) {
    setTimeout(() => {
      const server = _monitorServers.find(s => s.id === _lastDeployedClusterId);
      if (server && server.status === "available") {
        viewAppDetail(server.id, server.monitor_type || "k8s", server.name);
      }
    }, 800);
  }
}
