<template>
  <div>
    <div class="card" v-if="disabled">
      <h3>{{ $t('monitor.appTitle') }}</h3>
      <p style="color:#888;padding:20px 0">{{ $t('monitor.disabled') }}</p>
    </div>
    <template v-else>
      <div class="card" style="margin-bottom:12px">
        <h3>{{ $t('monitor.selectServer') }}</h3>
        <table>
          <thead><tr><th>{{ $t('monitor.name') }}</th><th>{{ $t('monitor.host') }}</th><th>{{ $t('monitor.type') }}</th><th>{{ $t('monitor.capability') }}</th><th>{{ $t('monitor.status') }}</th><th>{{ $t('common.action') }}</th></tr></thead>
          <tbody>
            <tr v-if="loading"><td colspan="6" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
            <tr v-for="s in servers" :key="s.id">
              <td><strong>{{ s.name }}</strong></td>
              <td>{{ s.host }}:{{ s.port }}</td>
              <td>{{ typeLabel(s) }}</td>
              <td v-html="capHtml(s)"></td>
              <td><span class="badge" :class="s.status==='available'?'badge-ok':s.status==='unavailable'?'badge-pend':'badge-err'">{{ s.status === 'available' ? $t('monitor.available') : s.status === 'unavailable' ? $t('monitor.unavailable') : $t('monitor.error') }}</span></td>
              <td>
                <button v-if="s.status==='available'" class="btn btn-blue btn-sm" @click="viewDetail(s)">{{ $t('monitor.viewApp') }}</button>
                <button v-else class="btn btn-sm" disabled style="opacity:0.4">{{ $t('monitor.unavailable') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- K8S App Detail -->
      <template v-if="detailServer && currentType === 'k8s'">
        <div class="card" style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h3 style="margin:0">{{ $t('monitor.nodeTitle') }}</h3>
            <span style="color:#81c784;font-size:13px">{{ $t('monitor.cluster', { name: detailName }) }}</span>
          </div>
          <div v-if="nodesLoading" style="margin-top:8px;color:#888">{{ $t('monitor.loading') }}</div>
          <div v-else-if="nodes.length" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;margin-top:8px">
            <div v-for="n in nodes" :key="n.name" style="padding:12px;background:#1a1a2e;border-radius:6px;border:1px solid #333">
              <div style="font-weight:600;margin-bottom:8px;color:#64b5f6">🖥️ {{ n.name }}</div>
              <div style="font-size:12px;color:#aaa;margin-bottom:4px">{{ $t('monitor.cpu') }}: <span style="color:#ffab40">{{ n.cpu }}</span> / {{ n.capacity_cpu }}</div>
              <div style="font-size:12px;color:#aaa;margin-bottom:4px">{{ $t('monitor.memory') }}: <span style="color:#81c784">{{ n.memory }}</span> / {{ n.capacity_memory }}</div>
              <div style="font-size:11px;color:#666">{{ $t('monitor.maxPods') }}: {{ n.max_pods }}</div>
            </div>
          </div>
          <div v-else-if="nodesError" style="color:#e57373;padding:8px;background:#2a1a1a;border-radius:4px">{{ nodesError }}</div>
        </div>
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px">
            <h3 style="margin:0">{{ $t('monitor.podTitle') }}</h3>
            <div style="display:flex;gap:8px;align-items:center">
              <select v-model="nsFilter" @change="loadPods" style="width:auto;margin:0;padding:3px 8px">
                <option value="">{{ $t('monitor.allNamespaces') }}</option>
                <option v-for="ns in namespaces" :key="ns" :value="ns">{{ ns }}</option>
              </select>
              <input v-model="podSearch" :placeholder="$t('monitor.searchPod')" style="width:140px;margin:0;padding:3px 8px;font-size:12px">
            </div>
          </div>
          <div style="max-height:400px;overflow-y:auto">
            <table>
              <thead><tr><th>{{ $t('monitor.namespace') }}</th><th>{{ $t('monitor.pod') }}</th><th>{{ $t('monitor.cpu') }}</th><th>{{ $t('monitor.memory') }}</th><th>{{ $t('monitor.status') }}</th><th>{{ $t('monitor.restarts') }}</th><th>{{ $t('monitor.node') }}</th></tr></thead>
              <tbody>
                <tr v-if="podsLoading"><td colspan="7" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
                <tr v-else v-for="p in filteredPods" :key="p.name" style="cursor:pointer" @click="showPodDetail(p)" title="点击查看详情">
                  <td>{{ p.namespace }}</td>
                  <td><strong>{{ p.name }}</strong></td>
                  <td style="color:#ffab40">{{ p.cpu || '?' }}</td>
                  <td style="color:#81c784">{{ p.memory || '?' }}</td>
                  <td><span class="badge" :class="p.status==='Running'?'badge-ok':p.status==='Pending'?'badge-pend':'badge-err'">{{ p.status }}</span></td>
                  <td>{{ p.restarts || '0' }}</td>
                  <td style="font-size:11px;color:#888">{{ p.node || '?' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>

      <!-- Docker App Detail -->
      <div v-if="detailServer && currentType === 'docker'" class="card">
        <div v-if="dockerLoading" style="color:#888">{{ $t('monitor.loading') }}</div>
        <template v-else-if="dockerContainers.length">
          <h3 style="margin:0 0 8px 0">🐳 {{ $t('monitor.podTitle') }}</h3>
          <div style="max-height:400px;overflow-y:auto">
            <table>
              <thead><tr><th>{{ $t('monitor.containerName') }}</th><th>{{ $t('monitor.cpu') }}</th><th>{{ $t('monitor.memory') }}</th><th>{{ $t('monitor.memPercent') }}</th><th>{{ $t('monitor.netIO') }}</th><th>{{ $t('monitor.diskIO') }}</th></tr></thead>
              <tbody>
                <tr v-for="c in dockerContainers" :key="c.name">
                  <td><strong>{{ c.name }}</strong></td>
                  <td style="color:#ffab40">{{ c.cpu }}</td>
                  <td style="color:#81c784">{{ c.memory }}</td>
                  <td>{{ c.memory_percent }}</td>
                  <td style="font-size:11px;color:#888">{{ c.net_io }}</td>
                  <td style="font-size:11px;color:#888">{{ c.block_io }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <div v-else style="color:#888;padding:8px 0">{{ $t('monitor.noContainers') }}</div>
      </div>

      <!-- SSH App -->
      <div v-if="detailServer && currentType === 'ssh'" class="card">
        <div style="color:#888;padding:20px 0;text-align:center">{{ $t('monitor.sshNoAppMonitor') }}</div>
      </div>
    </template>

    <!-- Pod Detail Modal -->
    <div v-if="podModal.show" class="modal-overlay" @click.self="podModal.show = false">
      <div style="background:#1a1a2e;border-radius:8px;padding:20px;max-width:700px;width:90%;max-height:80vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <h3 style="margin:0">{{ $t('monitor.podDetailTitle', { ns: podModal.ns, name: podModal.name }) }}</h3>
          <button style="background:none;border:none;color:#e57373;font-size:18px;cursor:pointer" @click="podModal.show = false">✕</button>
        </div>
        <div v-if="podModal.loading" style="color:#888;font-size:12px">{{ $t('monitor.loading') }}</div>
        <div v-else>
          <div style="margin-bottom:10px"><strong>{{ $t('monitor.resourceUsage') }}:</strong><pre style="background:#111;color:#00ff00;padding:8px;border-radius:4px;margin:4px 0;font-size:12px">{{ podModal.top || $t('monitor.noData') }}</pre></div>
          <div style="margin-bottom:10px"><strong>{{ $t('monitor.recentLogs') }}:</strong><pre style="background:#111;color:#ccc;padding:8px;border-radius:4px;margin:4px 0;font-size:11px;max-height:200px;overflow-y:auto;white-space:pre-wrap">{{ podModal.logs || $t('common.noData') }}</pre></div>
          <div><strong>{{ $t('monitor.describe') }}:</strong><pre style="background:#111;color:#aaa;padding:8px;border-radius:4px;margin:4px 0;font-size:11px;max-height:200px;overflow-y:auto;white-space:pre-wrap">{{ podModal.describe || $t('common.noData') }}</pre></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'

const route = useRoute()
const auth = useAuth()
const { t } = useI18n()

const disabled = ref(false)
const loading = ref(true)
const servers = ref([])
const detailServer = ref(0)
const detailName = ref('')
const currentType = ref('')

const TYPE_LABELS = { k8s: '☸️ K8S', docker: '🐳 Docker', ssh: '🖥️ Linux' }

// K8S
const nodesLoading = ref(false)
const nodes = ref([])
const nodesError = ref('')
const podsLoading = ref(false)
const pods = ref([])
const namespaces = ref([])
const nsFilter = ref('')
const podSearch = ref('')

// Docker
const dockerLoading = ref(false)
const dockerContainers = ref([])

const filteredPods = computed(() => {
  const q = podSearch.value.toLowerCase()
  return q ? pods.value.filter(p => p.name.toLowerCase().includes(q)) : pods.value
})

const podModal = reactive({ show: false, name: '', ns: '', loading: false, top: '', logs: '', describe: '' })

function typeLabel(s) { return TYPE_LABELS[s.monitor_type || 'ssh'] || s.type }

function capHtml(s) {
  const mt = s.monitor_type || 'unknown'
  if (mt === 'k8s') {
    const parts = []
    if (s.has_metrics_server) parts.push(t('monitor.metricsServer'))
    if (s.has_prometheus) parts.push(t('monitor.prometheus'))
    if (parts.length >= 1) return `<span style="color:#81c784;font-size:12px">✅ ${parts.join(' + ')}</span>`
    if (s.status === 'error') return `<span style="color:#e57373;font-size:12px">${t('monitor.connectionFailed')}</span>`
    return `<span style="color:#e57373;font-size:12px">${t('monitor.noMonitorComps')}</span>`
  }
  if (mt === 'docker') {
    if (s.status === 'available') return `<span style="color:#81c784;font-size:12px">${t('monitor.dockerStats')}</span>`
    if (s.status === 'error') return `<span style="color:#e57373;font-size:12px">${t('monitor.connectionFailed')}</span>`
    return `<span style="color:#e57373;font-size:12px">${t('monitor.dockerUnavailable')}</span>`
  }
  return `<span style="color:#667;font-size:12px">${t('monitor.seeSystemResources')}</span>`
}

async function checkEnabled() {
  try {
    const r = await fetch('/api/monitor/status')
    if (!r.ok) { disabled.value = true; return false }
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) { disabled.value = true; return false }
    const d = await r.json()
    disabled.value = !d.enabled
    return d.enabled
  } catch (e) { disabled.value = true; return false }
}

async function loadServers() {
  if (disabled.value) return
  loading.value = true
  try {
    const r = await fetch('/api/monitor/servers', { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) { console.error('monitor/servers 返回非 JSON'); return }
    const d = await r.json()
    servers.value = Array.isArray(d.servers) ? d.servers : []
    // Auto-select if coming from K8s deploy
    if (route.query.clusterId && servers.value.length) {
      const sid = parseInt(route.query.clusterId)
      const server = servers.value.find(s => s.id === sid)
      if (server && server.status === 'available') {
        setTimeout(() => viewDetail(server), 500)
      }
    }
  } catch (e) {} finally { loading.value = false }
}

function viewDetail(s) {
  detailServer.value = s.id
  detailName.value = s.name
  currentType.value = s.monitor_type || 'ssh'
  nodesLoading.value = false; nodes.value = []; nodesError.value = ''
  podsLoading.value = false; pods.value = []
  dockerContainers.value = []
  dockerLoading.value = false

  if (currentType.value === 'k8s') {
    loadNodes(s.id)
    loadPodsData(s.id)
  } else if (currentType.value === 'docker') {
    loadDocker(s.id)
  }
}

async function loadNodes(sid) {
  nodesLoading.value = true
  nodesError.value = ''
  try {
    const r = await fetch(`/api/monitor/nodes/${sid}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) { nodesError.value = t('monitor.apiError'); return }
    const d = await r.json()
    if (d.has_metrics) nodes.value = d.nodes || []
    else nodesError.value = t('monitor.nodeError') + (d.hint ? '<br><small>' + d.hint + '</small>' : '')
  } catch (e) { nodesError.value = t('monitor.loadFailed', { msg: e.message }) } finally { nodesLoading.value = false }
}

async function loadPodsData(sid) { await loadPods() }

async function loadPods() {
  if (!detailServer.value) return
  podsLoading.value = true
  try {
    const ns = nsFilter.value ? `?namespace=${encodeURIComponent(nsFilter.value)}` : ''
    const r = await fetch(`/api/monitor/pods/${detailServer.value}${ns}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) return
    const d = await r.json()
    pods.value = Array.isArray(d.pods) ? d.pods : []
    if (d.namespaces) namespaces.value = d.namespaces
  } catch (e) {} finally { podsLoading.value = false }
}

async function loadDocker(sid) {
  dockerLoading.value = true
  try {
    const r = await fetch(`/api/monitor/docker/${sid}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) return
    const d = await r.json()
    if (d.success) dockerContainers.value = Array.isArray(d.containers) ? d.containers : []
  } catch (e) {} finally { dockerLoading.value = false }
}

async function showPodDetail(p) {
  podModal.show = true
  podModal.name = p.name
  podModal.ns = p.namespace
  podModal.loading = true
  podModal.top = ''; podModal.logs = ''; podModal.describe = ''
  try {
    const r = await fetch(`/api/monitor/pod-detail/${detailServer.value}?namespace=${encodeURIComponent(p.namespace)}&name=${encodeURIComponent(p.name)}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) { podModal.top = t('monitor.apiError'); return }
    const d = await r.json()
    podModal.top = d.top || t('monitor.noData')
    podModal.logs = d.logs || t('common.noData')
    podModal.describe = d.describe || t('common.noData')
  } catch (e) { podModal.top = t('common.failed') } finally { podModal.loading = false }
}

onMounted(async () => {
  const enabled = await checkEnabled()
  if (enabled) {
    loadServers()
  }
})
</script>
