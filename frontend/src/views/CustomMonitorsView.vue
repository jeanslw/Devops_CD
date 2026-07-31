<template>
  <div class="card">
    <h3 style="margin-top:0">{{ $t('customMonitor.title') }}</h3>

    <!-- 新建/编辑表单 -->
    <div style="margin-bottom:12px">
      <input v-model="form.name" :placeholder="$t('customMonitor.name')" style="margin-bottom:8px">
      <div style="margin-bottom:8px">
        <input v-model="form.command" :placeholder="$t('customMonitor.command')" style="margin-bottom:8px">
      </div>
      <div class="grid2" style="gap:8px;margin-bottom:8px">
        <select v-model="form.output_format" style="margin-bottom:0">
          <option value="auto">{{ $t('customMonitor.formatAuto') }}</option>
          <option value="csv">{{ $t('customMonitor.formatCsv') }}</option>
          <option value="kv">{{ $t('customMonitor.formatKv') }}</option>
          <option value="json">{{ $t('customMonitor.formatJson') }}</option>
        </select>
        <input v-model="form.description" :placeholder="$t('customMonitor.description')" style="margin-bottom:0">
      </div>
      <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:2px">{{ $t('alerts.servers') }}</label>
      <MultiSelect v-model="selectedServerIds" :servers="allServers" style="margin-bottom:8px" />
      <div style="font-size:11px;color:#888;margin-bottom:8px">{{ $t('alerts.serverHint') }}</div>

      <!-- 指标定义 -->
      <div style="margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="font-size:13px;font-weight:600;color:var(--text)">{{ $t('customMonitor.metrics') }}</span>
          <button class="btn btn-sm" @click="addMetric">＋ {{ $t('customMonitor.addMetric') }}</button>
        </div>
        <div v-if="form.metrics.length === 0" style="font-size:11px;color:#888;padding:4px 0">{{ $t('customMonitor.noMetrics') }}</div>
        <div v-for="(m, i) in form.metrics" :key="i" class="grid3" style="gap:6px;margin-bottom:4px;align-items:center">
          <input v-model="m.name" :placeholder="$t('customMonitor.metricName')" style="margin-bottom:0;font-size:12px;padding:5px 8px">
          <input v-model="m.field_key" :placeholder="$t('customMonitor.fieldKey')" style="margin-bottom:0;font-size:12px;padding:5px 8px">
          <div style="display:flex;gap:4px;align-items:center">
            <input v-model="m.unit" :placeholder="$t('customMonitor.unitPlaceholder')" style="margin-bottom:0;font-size:12px;padding:5px 8px;flex:1">
            <button class="btn btn-red btn-sm" style="margin:0;padding:3px 8px;white-space:nowrap" @click="form.metrics.splice(i,1)">✕</button>
          </div>
        </div>
      </div>

      <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#888;margin-bottom:8px">
        <input type="checkbox" v-model="form.enabled"> {{ $t('alerts.enabled') }}
      </label>
      <div style="margin-bottom:12px">
        <button class="btn btn-green" @click="save">{{ editingId ? $t('customMonitor.update') : $t('customMonitor.create') }}</button>
        <button v-if="editingId" class="btn" @click="cancelEdit" style="margin-left:8px">{{ $t('alerts.cancel') }}</button>
      </div>
    </div>

    <!-- 监控项列表 -->
    <table>
      <thead>
        <tr>
          <th>{{ $t('customMonitor.name') }}</th>
          <th>{{ $t('customMonitor.command') }}</th>
          <th>{{ $t('customMonitor.outputFormat') }}</th>
          <th>{{ $t('customMonitor.metrics') }}</th>
          <th>{{ $t('alerts.enabled') }}</th>
          <th>{{ $t('common.action') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading"><td colspan="6" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
        <tr v-for="m in monitors" :key="m.id">
          <td><strong>{{ m.name }}</strong></td>
          <td><code style="font-size:12px;white-space:pre-wrap;word-break:break-all;max-width:280px;display:block">{{ m.command }}</code></td>
          <td>{{ m.output_format || 'auto' }}</td>
          <td>{{ (m.metrics || []).length || '—' }}</td>
          <td><span v-if="m.enabled" :style="{color:'var(--green)'}">ON</span><span v-else :style="{color:'var(--text-dim)'}">OFF</span></td>
          <td>
            <button class="btn btn-sm" @click="test(m.id)">🧪 {{ $t('customMonitor.test') }}</button>
            <button class="btn btn-sm" @click="editMonitor(m)">{{ $t('common.edit') }}</button>
            <button class="btn btn-red btn-sm" @click="del(m.id)">{{ $t('common.delete') }}</button>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 测试结果弹窗 -->
    <div v-if="testVisible" class="overlay" @click.self="testVisible=false">
      <div class="modal" style="max-width:900px;max-height:80vh;overflow-y:auto">
        <h4 style="margin-top:0">{{ $t('customMonitor.testResult') }}</h4>
        <div v-if="testLoading" style="color:#888;padding:20px;text-align:center">{{ $t('common.loading') }}</div>
        <div v-else v-for="r in testResults" :key="r.server_id" style="margin-bottom:12px;padding:8px;background:var(--bg-raised);border-radius:6px">
          <div style="font-size:13px;font-weight:600;margin-bottom:4px">
            <span :style="{color:'var(--accent)'}">{{ r.server_name }}</span>
            <span style="color:#888;font-size:11px;margin-left:4px">({{ r.host }})</span>
          </div>
          <div v-if="r.error" style="color:var(--red);font-size:12px">❌ {{ r.error }}</div>
          <template v-else>
            <!-- 诊断信息：解析结果为空但有原始输出 -->
            <div v-if="r.diagnostic" style="margin-bottom:8px;padding:10px;background:#fff3cd;border:1px solid #ffc107;border-radius:6px">
              <div style="font-size:13px;font-weight:600;color:#856404;margin-bottom:4px">🔍 {{ $t('customMonitor.diagnosticTitle') }}</div>
              <div v-if="r.diagnostic.hint" style="font-size:12px;color:#664d03;margin-bottom:6px;line-height:1.5">{{ r.diagnostic.hint }}</div>
              <div style="display:flex;gap:24px;font-size:11px;flex-wrap:wrap">
                <div>
                  <div style="font-weight:600;color:#856404;margin-bottom:2px">{{ $t('customMonitor.availableHeaders') }}</div>
                  <code v-for="h in r.diagnostic.available_headers" :key="h" style="display:inline-block;margin:2px 3px 2px 0;background:#fff;padding:2px 6px;border-radius:3px;font-size:11px">{{ h }}</code>
                </div>
                <div>
                  <div style="font-weight:600;color:#856404;margin-bottom:2px">{{ $t('customMonitor.configuredKeys') }}</div>
                  <code
                    v-for="k in r.diagnostic.configured_keys" :key="k"
                    :style="{display:'inline-block',margin:'2px 3px 2px 0',padding:'2px 6px',borderRadius:'3px',fontSize:'11px',
                      background: r.diagnostic.matched_headers.includes(k) ? '#d4edda' : r.diagnostic.unmatched_keys.includes(k) ? '#ffe0e0' : '#fff',
                      border: r.diagnostic.matched_headers.includes(k) ? '1px solid #28a745' : r.diagnostic.unmatched_keys.includes(k) ? '1px solid #dc3545' : ''}"
                  >{{ k }}</code>
                </div>
              </div>
            </div>
            <!-- 结构化解析结果 -->
            <div v-if="r.parsed && r.parsed.length" style="margin-bottom:6px">
              <div style="font-size:12px;color:#888;margin-bottom:4px">{{ $t('customMonitor.parsedResult') }} ({{ r.parsed.length }})</div>
              <table style="font-size:12px;margin:0">
                <thead>
                  <tr>
                    <th>{{ $t('customMonitor.metricName') }}</th>
                    <th>{{ $t('customMonitor.fieldKey') }}</th>
                    <th>{{ $t('customMonitor.value') }}</th>
                    <th>{{ $t('customMonitor.unit') }}</th>
                    <th>Entity</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(p, pi) in r.parsed" :key="pi">
                    <td>{{ p.metric_name }}</td>
                    <td>{{ p.field_key }}</td>
                    <td><code :style="{color:p.value !== null ? 'var(--green)' : 'var(--red)'}">{{ p.value !== null ? p.value : '—' }}</code></td>
                    <td>{{ p.unit || '—' }}</td>
                    <td style="color:#888;font-size:11px">{{ p.entity_label || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <!-- 原始输出 -->
            <div style="margin-top:6px">
              <div style="font-size:11px;color:#888;margin-bottom:2px;cursor:pointer" @click="r._rawOpen = !r._rawOpen">
                {{ $t('customMonitor.rawOutput') }} ▾
              </div>
              <pre v-if="r._rawOpen" style="margin:0;font-size:11px;padding:6px;background:#111;border-radius:4px;max-height:120px;overflow:auto">{{ r.output || '（空）' }}</pre>
            </div>
          </template>
        </div>
        <div style="text-align:right;margin-top:12px">
          <button class="btn" @click="testVisible=false">{{ $t('customMonitor.close') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'
import MultiSelect from '@/components/MultiSelect.vue'

const auth = useAuth()
const { t } = useI18n()
const { toast } = useToast()
const { showError } = useError()

const monitors = ref([])
const loading = ref(true)
const editingId = ref(0)
const testVisible = ref(false)
const testLoading = ref(false)
const testResults = ref([])
const selectedServerIds = ref([])
const allServers = ref([])

const form = reactive({
  name: '', command: '', output_format: 'auto', description: '', server_ids: '',
  enabled: true, metrics: []
})

function blankMetric() {
  return { name: '', field_key: '', unit: '', sort_order: 0 }
}

function addMetric() {
  form.metrics.push(blankMetric())
}

async function loadServers() {
  try {
    const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    allServers.value = await r.json()
  } catch (e) { console.error(e) }
}

async function loadData() {
  loading.value = true
  try {
    const r = await fetch('/api/custom-monitors', { headers: auth.A() })
    if (auth.handle401(r)) return
    monitors.value = await r.json()
  } catch (e) { console.error(e) }
  finally { loading.value = false }
}

async function save() {
  if (!form.name.trim() || !form.command.trim()) {
    return toast(t('customMonitor.fillRequired'), false)
  }
  const payload = {
    name: form.name.trim(),
    command: form.command.trim(),
    output_format: form.output_format,
    description: form.description.trim(),
    server_ids: selectedServerIds.value.join(','),
    enabled: form.enabled,
    metrics: form.metrics.filter(m => m.name.trim() && m.field_key.trim()).map((m, i) => ({
      ...m, sort_order: i, id: m.id || null
    }))
  }
  const url = editingId.value ? `/api/custom-monitors/${editingId.value}` : '/api/custom-monitors'
  const method = editingId.value ? 'PUT' : 'POST'
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify(payload)
  })
  if (auth.handle401(r)) return
  if (!r.ok) {
    return showError(r)
  }
  toast(editingId.value ? t('customMonitor.updated') : t('customMonitor.created'), true)
  cancelEdit()
  loadData()
}

function editMonitor(m) {
  editingId.value = m.id
  Object.assign(form, {
    name: m.name,
    command: m.command,
    output_format: m.output_format || 'auto',
    description: m.description || '',
    enabled: !!m.enabled
  })
  form.metrics = (m.metrics || []).map(mt => ({ ...mt }))
  selectedServerIds.value = (m.server_ids || '').split(',').filter(Boolean).map(Number)
  form.server_ids = m.server_ids || ''
}

function cancelEdit() {
  editingId.value = 0
  form.name = ''; form.command = ''; form.output_format = 'auto'; form.description = ''
  form.server_ids = ''; form.enabled = true; form.metrics = []
  selectedServerIds.value = []
}

async function del(id) {
  if (!confirm(t('customMonitor.confirmDelete'))) return
  const r = await fetch(`/api/custom-monitors/${id}`, { method: 'DELETE', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('customMonitor.deleted'), true)
  loadData()
}

async function test(id) {
  testVisible.value = true
  testLoading.value = true
  testResults.value = []
  try {
    const r = await fetch(`/api/custom-monitors/${id}/test`, { method: 'POST', headers: auth.A() })
    if (auth.handle401(r)) return
    const data = await r.json()
    testResults.value = (data.results || []).map(r => ({ ...r, _rawOpen: false }))
  } catch (e) { console.error(e) }
  finally { testLoading.value = false }
}

onMounted(() => { loadData(); loadServers() })
</script>
