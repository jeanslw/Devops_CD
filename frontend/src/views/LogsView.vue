<template>
  <div class="card">
    <h3>{{ $t('logs.title') }}</h3>
    <table>
      <thead><tr><th>{{ $t('logs.id') }}</th><th>{{ $t('logs.time') }}</th><th>{{ $t('logs.project') }}</th><th>{{ $t('logs.tag') }}</th><th>{{ $t('logs.method') }}</th><th>{{ $t('logs.status') }}</th><th>{{ $t('logs.operator') }}</th><th>{{ $t('logs.detail') }}</th></tr></thead>
      <tbody>
        <tr v-if="loading"><td colspan="8" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
        <template v-else-if="logs.length === 0">
          <tr><td colspan="8" style="text-align:center;color:#888">{{ $t('logs.noRecords') }}</td></tr>
        </template>
        <template v-else v-for="(l, idx) in logs" :key="l.id">
          <tr style="cursor:pointer" @click="toggleDetail(idx)">
            <td><span style="color:#81c784;font-weight:bold">#{{ l.deploy_id }}</span></td>
            <td>{{ l.created_at }}</td>
            <td>{{ l.project }}</td>
            <td>{{ l.tag }}</td>
            <td>{{ l.deploy_type }}</td>
            <td>
              <span class="badge" :class="'badge-' + (l.status === 'ok' ? 'ok' : l.status === 'failed' ? 'err' : 'pend')">{{ l.status }}</span>
            </td>
            <td>{{ l.triggered_by || '-' }}</td>
            <td class="output-preview">{{ l.output || '' }}</td>
          </tr>
          <tr v-if="expandedIdx === idx" class="log-detail">
            <td colspan="8">
              <div class="target-block">
                <pre class="output-code">{{ escapeHtml(l.output || $t('logs.noOutput')) }}</pre>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    <div v-if="totalPages > 1" class="log-pager">
      <span class="log-pager-info">{{ $t('logs.totalInfo', { total, pages: totalPages }) }}</span>
      <button v-if="page > 1" class="btn btn-sm log-pager-btn" @click="loadData(page - 1)">{{ $t('common.prev') }}</button>
      <button v-for="i in pageRange" :key="i" class="btn btn-sm log-pager-btn" :class="{ active: i === page }" @click="loadData(i)">{{ i }}</button>
      <button v-if="page < totalPages" class="btn btn-sm log-pager-btn" @click="loadData(page + 1)">{{ $t('common.next') }}</button>
      <select class="log-pager-size" v-model="pageSize" @change="loadData(1)">
        <option :value="10">{{ $t('logs.perPage', { n: 10 }) }}</option>
        <option :value="15" selected>{{ $t('logs.perPage', { n: 15 }) }}</option>
        <option :value="30">{{ $t('logs.perPage', { n: 30 }) }}</option>
        <option :value="50">{{ $t('logs.perPage', { n: 50 }) }}</option>
      </select>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useAuth } from '@/composables/useAuth'

const auth = useAuth()

const logs = ref([])
const loading = ref(true)
const page = ref(1)
const pageSize = ref(15)
const total = ref(0)
const totalPages = ref(1)
const expandedIdx = ref(-1)

const pageRange = computed(() => {
  const start = Math.max(1, page.value - 2)
  const end = Math.min(totalPages.value, page.value + 2)
  const arr = []
  for (let i = start; i <= end; i++) arr.push(i)
  return arr
})

function toggleDetail(idx) {
  expandedIdx.value = expandedIdx.value === idx ? -1 : idx
}

function escapeHtml(s) {
  if (!s) return ''
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

async function loadData(p = 1) {
  loading.value = true
  expandedIdx.value = -1
  try {
    const r = await fetch(`/api/deploy-logs?page=${p}&page_size=${pageSize.value}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) {
      console.error('API 返回非 JSON:', await r.text().catch(() => ''))
      return
    }
    const d = await r.json()
    logs.value = Array.isArray(d.items) ? d.items : []
    page.value = d.page || 1
    total.value = d.total || 0
    totalPages.value = d.total_pages || 1
  } catch (e) {
    console.error('加载部署记录失败:', e)
  } finally {
    loading.value = false
  }
}

onMounted(() => loadData())
</script>

<style scoped>
.output-preview {
  max-width: 280px;
  font-size: 11px;
  white-space: pre-wrap;
  line-height: 1.3;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.output-code {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  max-height: 300px;
  overflow-y: auto;
  background: #111;
  color: #00ff00;
  padding: 10px;
  border-radius: 4px;
  font-family: monospace;
}
</style>
