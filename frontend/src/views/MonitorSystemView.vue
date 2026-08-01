<template>
  <div>
    <div class="card" v-if="disabled">
      <h3>{{ $t('monitor.sysTitle') }}</h3>
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
                <button v-if="s.status==='available'||s.status==='unavailable'" class="btn btn-blue btn-sm" @click="viewDetail(s.id)">{{ $t('monitor.viewSys') }}</button>
                <button v-else class="btn btn-sm" disabled style="opacity:0.4">{{ $t('monitor.unavailable') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="detailServerId" class="card">
        <h3 style="margin:0 0 8px 0">{{ $t('monitor.sysTitle') }}</h3>
        <div v-if="detailLoading" style="color:#888">{{ $t('monitor.loading') }}</div>
        <div v-else-if="detailError" style="color:#e57373">{{ detailError }}</div>
        <template v-else-if="detail">
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-bottom:12px">
            <div style="padding:14px;background:#1a1a2e;border-radius:6px;border:1px solid #333">
              <div style="font-weight:600;margin-bottom:8px;color:#64b5f6">{{ $t('monitor.systemInfo') }}</div>
              <div style="font-size:12px;color:#aaa;line-height:1.8">
                <div>{{ $t('monitor.os') }}: {{ detail.os || '?' }}</div>
                <div>{{ $t('monitor.cpuCores') }}: {{ detail.cpu_cores }}</div>
                <div>{{ $t('monitor.uptime') }}: {{ fmtUptime(detail.uptime_seconds) }}</div>
                <div>{{ $t('monitor.bootTime') }}: {{ detail.uptime_since || '?' }}</div>
              </div>
            </div>
            <div style="padding:14px;background:#1a1a2e;border-radius:6px;border:1px solid #333">
              <div style="font-weight:600;margin-bottom:8px;color:#64b5f6">{{ $t('monitor.load') }}</div>
              <div style="font-size:12px;color:#aaa;line-height:1.8">
                <div>Load: <span style="color:#ffab40">{{ detail.load || '?' }}</span></div>
                <div>{{ $t('monitor.memoryUsed') }}: <span :style="{color:memColor}">{{ detail.memory_used || '?' }} / {{ detail.memory_total || '?' }} ({{ detail.memory_percent || '?' }}%)</span></div>
                <div>{{ $t('monitor.diskUsed') }}: <span style="color:#ffab40">{{ detail.disk_used || '?' }} / {{ detail.disk_total || '?' }} ({{ detail.disk_percent || '?' }})</span></div>
              </div>
            </div>
          </div>
          <div v-if="detail.top_processes && detail.top_processes.length">
            <h4 style="margin:0 0 6px 0;font-size:13px;color:#888">{{ $t('monitor.topProcesses') }}</h4>
            <table>
              <thead><tr><th>{{ $t('monitor.pid') }}</th><th>CPU%</th><th>MEM%</th><th>{{ $t('monitor.cmd') }}</th></tr></thead>
              <tbody>
                <tr v-for="p in detail.top_processes" :key="p.pid">
                  <td>{{ p.pid }}</td>
                  <td style="color:#ffab40">{{ p.cpu }}</td>
                  <td style="color:#81c784">{{ p.mem }}</td>
                  <td style="font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ p.cmd }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'

const auth = useAuth()
const { t } = useI18n()

const disabled = ref(false)
const loading = ref(true)
const servers = ref([])
const detailServerId = ref(0)
const detailLoading = ref(false)
const detailError = ref('')
const detail = ref(null)

const TYPE_LABELS = { k8s: '☸️ K8S', docker: '🐳 Docker', ssh: '🖥️ Linux' }

const memColor = computed(() => {
  const p = parseFloat(detail.value?.memory_percent) || 0
  return p > 90 ? '#e57373' : p > 70 ? '#ffab40' : '#81c784'
})

function typeLabel(s) { return TYPE_LABELS[s.monitor_type || 'ssh'] || s.type }

function fmtUptime(secs) {
  if (!secs || secs <= 0) return '?'
  const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60)
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
}

function capHtml(s) {
  if (s.status === 'error') return `<span style="color:#e57373;font-size:12px">${t('monitor.connectionFailed')}</span>`
  if (s.status === 'unavailable') return `<span style="color:#ffab40;font-size:12px">${t('monitor.baseCmdMissing')}</span>`
  return `<span style="color:#81c784;font-size:12px">${t('monitor.sshOk')}</span>`
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
    if (!ct.includes('application/json')) return
    const d = await r.json()
    servers.value = Array.isArray(d.servers) ? d.servers : []
  } catch (e) {} finally { loading.value = false }
}

async function viewDetail(sid) {
  detailServerId.value = sid
  detailLoading.value = true
  detailError.value = ''
  detail.value = null
  try {
    const r = await fetch(`/api/monitor/system/${sid}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) { detailError.value = t('monitor.apiError'); return }
    const d = await r.json()
    if (d.success) detail.value = d.system
    else detailError.value = t('monitor.fetchFailed')
  } catch (e) { detailError.value = t('monitor.loadFailed', { msg: e.message }) } finally { detailLoading.value = false }
}

onMounted(async () => {
  const enabled = await checkEnabled()
  if (enabled) {
    loadServers()
  }
})
</script>
