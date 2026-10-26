<template>
  <div>
    <!-- 仓库卡片列表 -->
    <div class="card" v-if="!viewingArtifacts">
      <h3>{{ $t('registry.title') }}
        <span class="badge" style="margin-left:8px;font-size:11px;font-weight:400">{{ typeBadgeText }}</span>
        <span class="refresh-bar">
          <span :style="{ fontSize: '12px', color: syncUnreachable ? '#e74c3c' : '#888', marginRight: '10px' }">{{ lastSyncText }}</span>
          <select v-model="syncInterval" @change="onSyncIntervalChange" style="width:auto;margin:0;padding:3px 8px;font-size:12px">
            <option :value="0">{{ $t('registry.syncInterval.off') }}</option>
            <option :value="15">{{ $t('registry.syncInterval.minutes', { n: 15 }) }}</option>
            <option :value="30">{{ $t('registry.syncInterval.minutes', { n: 30 }) }}</option>
            <option :value="60">{{ $t('registry.syncInterval.hours', { n: 1 }) }}</option>
            <option :value="360">{{ $t('registry.syncInterval.hours', { n: 6 }) }}</option>
            <option :value="720">{{ $t('registry.syncInterval.hours', { n: 12 }) }}</option>
            <option :value="1440">{{ $t('registry.syncInterval.hours', { n: 24 }) }}</option>
          </select>
          <button class="btn btn-sm" @click="syncAll" :title="$t('registry.syncNow')">🔄</button>
        </span>
      </h3>
      <div class="repo-grid" v-if="!loading">
        <div v-if="repos.length === 0" style="color:#888;text-align:center;padding:40px;grid-column:1/-1">
          {{ $t('registry.noProjects') }}
          <button class="btn btn-sm btn-green" style="margin-left:8px" @click="syncAll">{{ $t('registry.syncAll') }}</button>
        </div>
        <div v-for="repo in repos" :key="repo.id" class="repo-card" @click="viewArtifacts(repo)">
          <div class="repo-card-icon">🐳</div>
          <div class="repo-card-body">
            <div class="repo-card-path">
              <span class="repo-card-project">{{ repo.project }}</span> /
              <span class="repo-card-repo">{{ repo.repo.split('/').pop() }}</span>
            </div>
            <div class="repo-card-full">{{ repo.repo }}</div>
          </div>
          <div class="repo-card-stats">
            <span class="repo-stat"><span class="repo-stat-num">{{ repo.tag_count }}</span> Tags</span>
            <span class="repo-stat"><span class="repo-stat-time">{{ formatTime(repo.latest_push) }}</span></span>
          </div>
          <div class="repo-card-arrow">→</div>
        </div>
      </div>
      <div v-else style="color:#888;text-align:center;padding:40px">{{ $t('common.loading') }}</div>
    </div>

    <!-- Artifact / Tag 列表 -->
    <div class="card" v-else>
      <h3 style="margin:0 0 12px 0">
        📦 {{ artifactRepoName }}
        <button class="btn btn-sm" style="margin-left:8px" @click="backToRepos">{{ $t('registry.backToRepos') }}</button>
        <button class="btn btn-sm" style="margin-left:8px" @click="syncCurrentRepo">{{ $t('registry.syncThisRepo') }}</button>
      </h3>
      <table>
        <thead><tr><th v-for="col in artifactColumns" :key="col.key">{{ col.label }}</th></tr></thead>
        <tbody>
          <tr v-if="artifactLoading"><td :colspan="artifactColspan" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
          <tr v-else v-for="a in artifacts" :key="a.tag">
            <td><code style="font-size:13px;font-weight:600;color:var(--accent)">{{ a.tag }}</code></td>
            <td><code v-if="a.digest" :title="a.digest" style="font-size:12px;color:#666;cursor:help">{{ shortDigest(a.digest) }}</code><span v-else style="color:#888">-</span></td>
            <td v-if="caps.size">{{ a.size_mb }} MB</td>
            <td v-if="caps.push_time" style="font-size:12px;white-space:nowrap">{{ formatTime(a.push_time) }}</td>
            <td v-if="caps.scan"><span class="severity-badge" :class="severityClass(a)">{{ severityText(a) }}</span></td>
            <td v-if="caps.scan">
              <button class="btn btn-blue btn-sm" @click="showScanReport(a)">{{ $t('registry.report') }}</button>
              <button class="btn btn-orange btn-sm" @click="triggerScan(a)">{{ $t('registry.scan') }}</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="artifactTotalPages > 1" style="display:flex;justify-content:center;align-items:center;gap:8px;margin-top:12px;font-size:13px">
        <span style="color:#888">{{ $t('logs.totalInfo', { total: artifactTotal, pages: artifactTotalPages }) }}</span>
        <button class="btn btn-sm" :disabled="artifactPage <= 1" @click="loadArtifacts(artifactPage - 1)">◀ {{ $t('common.prev') }}</button>
        <button v-for="i in artifactPageRange" :key="i" class="btn btn-sm" :class="{ 'btn-active': i === artifactPage }" @click="loadArtifacts(i)">{{ i }}</button>
        <button class="btn btn-sm" :disabled="artifactPage >= artifactTotalPages" @click="loadArtifacts(artifactPage + 1)">{{ $t('common.next') }} ▶</button>
      </div>
    </div>

    <!-- 扫描报告弹窗 -->
    <div v-if="scanDialog.show" class="modal-overlay" @click.self="scanDialog.show = false">
      <div class="modal-box-scan">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <h3 style="margin:0">{{ $t('registry.scanDialog.title', { tag: scanDialog.tag }) }}</h3>
          <button class="btn" @click="scanDialog.show = false">✕</button>
        </div>
        <div v-if="scanDialog.loading" style="color:#888;text-align:center;padding:20px">{{ $t('common.loading') }}</div>
        <div v-else-if="scanDialog.error" style="color:var(--red)">{{ scanDialog.error }}</div>
        <div v-else v-html="scanDialog.content"></div>
      </div>
    </div>

    <!-- 删除 Tag 功能已整体移除:Harbor / 标准 registry:2 均不提供跨类型安全的删除契约 -->
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'

const auth = useAuth()
const { t, te } = useI18n()
const { toast } = useToast()
const { showError } = useError()

const repos = ref([])
const loading = ref(true)
const lastSyncText = ref('加载中…')
const syncUnreachable = ref(false)
const syncInterval = ref(0)
let _syncing = false

const viewingArtifacts = ref(false)
const artifactRepoId = ref(0)
const artifactRepoName = ref('')
const artifacts = ref([])
const artifactLoading = ref(false)
const artifactPage = ref(1)
const artifactTotal = ref(0)
const artifactTotalPages = ref(1)
const artifactPageSize = 20

const artifactPageRange = computed(() => {
  const p = artifactPage.value, t = artifactTotalPages.value
  const start = Math.max(1, p - 3), end = Math.min(t, p + 3)
  const arr = []; for (let i = start; i <= end; i++) arr.push(i); return arr
})

const scanDialog = reactive({ show: false, tag: '', loading: false, content: '', error: '' })

// 镜像仓库类型与能力开关(GET /api/registry/config 下发;标准 registry:2 无扫描/大小/推送时间)
// type 为契约原样值:harbor / registry / ''(未取到,如实显示「未知」,不默认成 harbor)
const registryMeta = reactive({ type: '', capabilities: { scan: true, size: true, push_time: true } })
const caps = computed(() => registryMeta.capabilities)
const typeBadgeText = computed(() =>
  registryMeta.type ? t('registry.typeBadge', { type: registryMeta.type }) : t('registry.typeUnknown')
)
const artifactColumns = computed(() => {
  const cols = [{ key: 'tag', label: t('registry.tag') }]
  // digest 由 Glue tags 端点详情对象提供(Harbor v2 / registry:2 manifest 解析),两种类型都展示
  cols.push({ key: 'digest', label: t('registry.digest') })
  if (caps.value.size) cols.push({ key: 'size', label: t('registry.size') })
  if (caps.value.push_time) cols.push({ key: 'pushTime', label: t('registry.pushTime') })
  if (caps.value.scan) cols.push({ key: 'scan', label: t('registry.scanResult') }, { key: 'action', label: t('common.action') })
  return cols
})
const artifactColspan = computed(() => artifactColumns.value.length)

function shortDigest(d) {
  const s = (d || '') + ''
  return s.length > 19 ? s.slice(0, 19) + '…' : s
}

function formatTime(t) {
  if (!t) return '-'
  try {
    const normalized = (t + '').replace(' ', 'T') + ((t + '').includes('Z') ? '' : 'Z')
    const d = new Date(normalized)
    if (isNaN(d.getTime())) return (t + '').slice(0, 16)
    const pad = n => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch (e) { return (t + '').slice(0, 16) }
}

function severityClass(a) {
  const sev = a.scan_severity || ''
  if (sev === 'Critical') return 'sev-critical'
  if (sev === 'High') return 'sev-high'
  if (sev === 'Medium') return 'sev-medium'
  if (sev === 'Low') return 'sev-low'
  const st = (a.scan_status || '').toLowerCase()
  const done = ['success', 'finished', 'complete', 'done'].includes(st)
  if (done) return 'sev-none'
  return 'sev-none'
}

function severityText(a) {
  const sev = a.scan_severity || ''
  const hasVuln = (a.vuln_critical || 0) + (a.vuln_high || 0) + (a.vuln_medium || 0) + (a.vuln_low || 0) > 0
  if (sev && sev !== 'None' && hasVuln) {
    const emoji = { Critical: '🔴', High: '🟠', Medium: '🟡', Low: '🔵' }
    return `${emoji[sev] || '⚪'} ${sev} C:${a.vuln_critical} H:${a.vuln_high} M:${a.vuln_medium}`
  }
  const st = (a.scan_status || '').toLowerCase()
  const done = ['success', 'finished', 'complete', 'done'].includes(st)
  if (done) return `<span class="severity-badge sev-none">${t('registry.noVuln')}</span>`
  return t('registry.notScanned')
}

async function loadRepos() {
  loading.value = true
  try {
    const r = await fetch('/api/registry/repositories', { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) { console.error('registry/repositories 返回非 JSON'); return }
    const data = await r.json()
    repos.value = Array.isArray(data.repositories) ? data.repositories : []
    lastSyncText.value = syncUnreachable.value
      ? t('registry.syncUnreachable')
      : (data.last_sync ? t('registry.lastSync', { time: formatTime(data.last_sync) }) : t('registry.notSynced'))
  } catch (e) { console.error('加载仓库列表失败:', e) } finally { loading.value = false }
}

async function loadSyncConfig() {
  try {
    const r = await fetch('/api/registry/config', { headers: auth.A() })
    if (auth.handle401(r)) return
    const d = await r.json()
    syncInterval.value = parseInt(d.interval || 0)
    registryMeta.type = d.registry_type || ''
    registryMeta.capabilities = d.capabilities || { scan: true, size: true, push_time: true }
  } catch (e) {}
}

async function syncAll() {
  if (_syncing) return toast('⏳ ' + t('registry.syncing'), false)
  _syncing = true
  lastSyncText.value = t('registry.syncing')
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 120000)
  try {
    const r = await fetch('/api/registry/sync', { method: 'POST', headers: auth.A(), signal: ctrl.signal })
    clearTimeout(timer)
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) {
      toast(t('registry.syncError'), false)
      lastSyncText.value = t('registry.syncFail')
      loadRepos(); return
    }
    const d = await r.json()
    if (d.ok) {
      syncUnreachable.value = false
      lastSyncText.value = d.last_sync ? t('registry.lastSync', { time: formatTime(d.last_sync) }) : t('registry.syncComplete', { total: 0, repos: 0 })
      toast(t('registry.syncComplete', { total: d.total, repos: d.repos }), true)
    } else if (d.error_key === 'errors.harbor_unavailable') {
      syncUnreachable.value = true
      lastSyncText.value = t('registry.syncUnreachable')
      showError(d)
    } else if (d.error_key === 'errors.registry_permission_denied') {
      // 仓库账号无查询权限:状态栏与提示都明确写清原因,不再笼统显示"同步失败"
      syncUnreachable.value = false
      lastSyncText.value = t('errors.registry_permission_denied')
      showError(d)
    } else {
      syncUnreachable.value = false
      lastSyncText.value = t('registry.syncFail')
      showError(d)
    }
    loadRepos()
  } catch (e) {
    clearTimeout(timer)
    if (e.name === 'AbortError') {
      toast(t('registry.syncTimeout'), false)
      lastSyncText.value = t('registry.syncTimeout')
    } else {
      toast(t('registry.syncFail') + ': ' + e.message, false)
      lastSyncText.value = t('registry.syncFail')
    }
    loadRepos()
  } finally { _syncing = false }
}

async function onSyncIntervalChange() {
  const interval = parseInt(syncInterval.value) || 0
  try {
    const r = await fetch('/api/registry/config', {
      method: 'PUT', headers: { 'Content-Type': 'application/json', ...auth.A() },
      body: JSON.stringify({ interval })
    })
    if (auth.handle401(r)) return
    const d = await r.json()
    if (d.ok) {
      const intervalStr = interval >= 60 ? t('registry.syncInterval.hours', { n: interval / 60 }) : t('registry.syncInterval.minutes', { n: interval })
      toast(interval <= 0 ? t('registry.syncInterval.disabled') : t('registry.syncInterval.set', { interval: intervalStr }), true)
    } else {
      showError(d)
      loadSyncConfig()
    }
  } catch (e) {
    toast('❌ ' + t('registry.syncInterval.fail') + ': ' + e.message, false)
    loadSyncConfig()
  }
}

function viewArtifacts(repo) {
  viewingArtifacts.value = true
  artifactRepoId.value = repo.id
  artifactRepoName.value = repo.repo
  artifactPage.value = 1
  loadArtifacts(1)
}

async function loadArtifacts(page) {
  artifactLoading.value = true
  artifactPage.value = page
  try {
    const r = await fetch(`/api/registry/artifacts/${artifactRepoId.value}?page=${page}&page_size=${artifactPageSize}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) { console.error('registry/artifacts 返回非 JSON'); return }
    const data = await r.json()
    artifacts.value = Array.isArray(data.items) ? data.items : []
    artifactTotal.value = data.total || 0
    artifactTotalPages.value = data.total_pages || 1
  } catch (e) { console.error('加载 artifacts 失败:', e) } finally { artifactLoading.value = false }
}

function backToRepos() {
  viewingArtifacts.value = false
  loadRepos()
}

async function syncCurrentRepo() {
  if (_syncing) return toast('⏳ ' + t('registry.syncing'), false)
  _syncing = true
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 120000)
  try {
    const r = await fetch(`/api/registry/sync?project=${encodeURIComponent(artifactRepoName.value.split('/')[0])}`, { method: 'POST', headers: auth.A(), signal: ctrl.signal })
    clearTimeout(timer)
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) {
      toast(t('registry.syncError'), false)
      return
    }
    const d = await r.json()
    if (d.ok) {
      syncUnreachable.value = false
      toast('✅ ' + t('registry.syncComplete', { total: 0, repos: 0 }).split('：')[0] + t('common.success'), true)
      if (artifactRepoId.value) loadArtifacts(artifactPage.value)
    } else {
      syncUnreachable.value = d.error_key === 'errors.harbor_unavailable'
      showError(d)
    }
  } catch (e) {
    clearTimeout(timer)
    if (e.name === 'AbortError') {
      toast(t('registry.syncTimeout'), false)
    } else {
      toast(t('registry.syncFail') + ': ' + e.message, false)
    }
  } finally { _syncing = false }
}

async function showScanReport(a) {
  scanDialog.show = true
  scanDialog.tag = a.tag
  scanDialog.loading = true
  scanDialog.content = ''
  scanDialog.error = ''
  try {
    const r = await fetch(`/api/registry/scan/report/${artifactRepoId.value}/${encodeURIComponent(a.tag)}`, { headers: auth.A() })
    if (auth.handle401(r)) { scanDialog.show = false; return }
    const ct = r.headers.get('content-type') || ''
    let d = null
    if (ct.includes('application/json')) { try { d = await r.json() } catch { d = null } }
    if (r.ok) {
      renderScanReport(d, a.tag)
    } else {
      // 报告取不到(未扫描 / Tag 不存在 / 该仓库类型不支持扫描 / 无查询权限等)如实提示,
      // 不再统一显示成"未发现漏洞"
      const key = d?.error_key
      scanDialog.error = (key && te(key)) ? t(key) : (d?.error || d?.detail || t('common.error'))
    }
  } catch (e) {
    scanDialog.error = '加载失败: ' + e.message
  } finally { scanDialog.loading = false }
}

function renderScanReport(data, tag) {
  let vulns = []
  let overview = {}
  if (data.summary || data.scan_status) { vulns = data.vulnerabilities || []; overview = data.summary || {} }
  else if (Array.isArray(data) && data.length > 0 && data[0].vulnerabilities) { vulns = data[0].vulnerabilities || [] }
  else if (data.vulnerabilities) { vulns = data.vulnerabilities || [] }
  else {
    const rptKey = Object.keys(data).find(k => k.includes('vulnerability.report') || k.includes('scanner.adapter'))
    if (rptKey) { const rpt = data[rptKey]; vulns = rpt.vulnerabilities || []; overview = rpt.summary || {} }
  }
  const c = overview.critical || 0, h = overview.high || 0, m = overview.medium || 0, l = overview.low || 0
  const total = overview.total || (c + h + m + l) || 0
  let html = `<div style="margin-bottom:16px;padding:12px 16px;background:rgba(0,0,0,.25);border-radius:8px;font-size:13px;line-height:1.8">
    <div style="font-weight:600;font-size:15px;margin-bottom:4px">${t('registry.scanDialog.totalVulns', { total })}</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">`
  if (c) html += `<span style="color:#e74c3c">${t('registry.scanDialog.critical', { n: c })}</span>`
  if (h) html += `<span style="color:#e67e22">${t('registry.scanDialog.high', { n: h })}</span>`
  if (m) html += `<span style="color:#f1c40f">${t('registry.scanDialog.medium', { n: m })}</span>`
  if (l) html += `<span style="color:#3498db">${t('registry.scanDialog.low', { n: l })}</span>`
  html += `</div></div>`
  if (!vulns.length) {
    html += total ? `<div style="color:#888;text-align:center;padding:20px">${t('registry.scanDialog.noDetail')}</div>` : `<div style="color:#888;text-align:center;padding:20px">${t('registry.scanDialog.noVulnFound')}</div>`
  } else {
    const sevOrder = { Critical: 0, High: 1, Medium: 2, Low: 3 }
    vulns.sort((a, b) => (sevOrder[a.severity] || 9) - (sevOrder[b.severity] || 9))
    html += `<div class="table-wrap" style="max-height:420px;overflow:auto"><table class="data-table" style="font-size:12px">
      <thead><tr><th>缺陷码</th><th style="width:70px">严重度</th><th>组件</th><th>当前版本</th><th>修复版本</th></tr></thead><tbody>`
    html += vulns.map(v => {
      const sev = v.severity || ''
      const sevClass = { Critical: 'sev-critical', High: 'sev-high', Medium: 'sev-medium', Low: 'sev-low' }
      const sevLabel = { Critical: '危急', High: '严重', Medium: '中等', Low: '其他' }
      return `<tr><td style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(v.id)}">${esc(v.id)}</td>
        <td><span class="severity-badge ${sevClass[sev] || 'sev-none'}">${sevLabel[sev] || esc(sev)}</span></td>
        <td style="font-size:11px">${esc(v.package)}</td>
        <td style="font-size:11px"><code>${esc(v.version)}</code></td>
        <td style="font-size:11px;color:var(--accent)"><code>${esc(v.fix_version || v.fixed_version || '-')}</code></td></tr>`
    }).join('')
    html += `</tbody></table></div>`
  }
  if (data.harbor_url) html += `<div style="margin-top:16px;text-align:right"><a href="${esc(data.harbor_url)}" target="_blank" class="btn btn-blue btn-sm">${t('registry.scanDialog.viewInHarbor')}</a></div>`
  scanDialog.content = html
}

function esc(s) { if (!s) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') }

async function triggerScan(a) {
  try {
    const r = await fetch(`/api/registry/scan/trigger/${artifactRepoId.value}/${encodeURIComponent(a.tag)}`, { method: 'POST', headers: auth.A() })
    if (auth.handle401(r)) return
    const d = await r.json()
    const failMsg = d.error_key && te(d.error_key) ? t(d.error_key) : (d.error || t('registry.syncTriggerFail'))
    toast(d.ok ? t('registry.syncTriggered') : '❌ ' + failMsg, d.ok)
  } catch (e) { toast(t('registry.syncTriggerFail') + ': ' + e.message, false) }
}

onMounted(() => { loadRepos(); loadSyncConfig() })
</script>
