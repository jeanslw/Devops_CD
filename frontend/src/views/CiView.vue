<template>
  <div class="card">
    <h3>
      {{ $t('ci.title') }}
      <span class="refresh-bar">
        <select v-model="interval" @change="onIntervalChange">
          <option :value="0">{{ $t('ci.off') }}</option>
          <option :value="15000">{{ $t('ci.seconds', { n: 15 }) }}</option>
          <option :value="30000" selected>{{ $t('ci.seconds', { n: 30 }) }}</option>
          <option :value="60000">{{ $t('ci.seconds', { n: 60 }) }}</option>
        </select>
        <button class="btn btn-sm" :class="interval > 0 ? 'btn-auto-on' : 'btn-auto-off'" @click="loadData" :title="interval > 0 ? $t('ci.autoRefresh') : $t('ci.refreshNow')">🔄</button>
      </span>
    </h3>
    <table>
      <thead>
        <tr><th>{{ $t('ci.project') }}</th><th>{{ $t('ci.ciSource') }}</th><th>{{ $t('ci.harborRepo') }}</th><th>{{ $t('ci.latestTag') }}</th><th>{{ $t('ci.pipeline') }}</th><th>{{ $t('common.action') }}</th></tr>
      </thead>
      <tbody>
        <tr v-if="loading"><td colspan="6" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
        <template v-else v-for="p in projects" :key="p.job_name">
          <tr>
            <td><strong>{{ p.job_name }}</strong></td>
            <td><span class="badge" :class="p.build_provider === 'gitlab_ci' ? 'badge-gitlab' : 'badge-jenkins'">{{ p.build_provider }}</span></td>
            <td>{{ p.harbor_repository || '—' }}</td>
            <td>{{ p.latest_tag || '—' }}</td>
            <td>{{ p.latest_pipeline ? '#' + p.latest_pipeline : '—' }}</td>
            <td>
              <button class="btn btn-blue btn-sm" @click="togglePipeline(p)" style="margin-right:6px">{{ $t('ci.buildStatus') }}</button>
              <select class="deploy-select" @change="quickDeploy($event, p)" v-model="deployTargets[p.job_name]" style="width:auto">
                <option value="">{{ $t('ci.deployTo') }}</option>
                <option value="ssh">{{ $t('ci.ssh') }}</option>
                <option value="docker">{{ $t('ci.docker') }}</option>
                <option value="k8s">{{ $t('ci.k8s') }}</option>
              </select>
            </td>
          </tr>
          <tr v-if="p._showPipeline" class="ci-detail-row">
            <td colspan="6">
              <div v-if="p._pipelineLoading" style="padding:10px;color:#888;font-size:12px">{{ $t('common.loading') }}</div>
              <div v-else-if="p._pipelineTag" style="padding:10px;background:#1b3a1b;border-radius:4px;border:1px solid #388e3c">
                <span style="color:#81c784;font-weight:600">{{ $t('ci.ciCompleted') }}</span>
                <span style="font-size:12px;color:#999;margin-left:8px">
                  <template v-if="p._pipelineIid">Pipeline #{{ p._pipelineIid }} · </template>
                  Tag {{ p._pipelineTag }}
                  <template v-if="p._pipelineCreated"> · {{ p._pipelineCreated }}</template>
                </span>
              </div>
              <div v-else style="padding:10px;color:#888;font-size:12px">{{ $t('ci.noCiData') }}</div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'

const router = useRouter()
const auth = useAuth()
const { toast } = useToast()

const projects = reactive([])
const loading = ref(true)
const interval = ref(parseInt(localStorage.getItem('cd_refresh_ci') || '30000'))
const deployTargets = reactive({})
let _timer = null

function onIntervalChange() {
  localStorage.setItem('cd_refresh_ci', interval.value)
  restartPolling()
}

function restartPolling() {
  clearInterval(_timer)
  if (interval.value > 0) {
    _timer = setInterval(loadData, interval.value)
  }
}

async function loadData() {
  try {
    const r = await fetch('/api/projects', { headers: auth.A() })
    if (auth.handle401(r)) return
    const d = await r.json()
    projects.splice(0, projects.length, ...d.map(p => ({
      ...p,
      _showPipeline: false,
      _pipelineLoading: false,
      _pipelineTag: '',
      _pipelineIid: '',
      _pipelineCreated: ''
    })))
    d.forEach(p => {
      if (!(p.job_name in deployTargets)) deployTargets[p.job_name] = ''
    })
    loading.value = false
  } catch (e) {
    loading.value = false
  }
}

async function togglePipeline(p) {
  if (p._showPipeline) {
    p._showPipeline = false
    return
  }
  p._showPipeline = true
  if (p._pipelineTag) return
  p._pipelineLoading = true
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(p.job_name)}/pipeline`, { headers: auth.A() })
    if (auth.handle401(r)) { p._showPipeline = false; return }
    const d = await r.json()
    p._pipelineTag = d.latest_tag || ''
    p._pipelineIid = d.pipeline?.iid
    p._pipelineCreated = d.pipeline?.created_at || ''
  } catch (e) {
    p._pipelineTag = null
  } finally {
    p._pipelineLoading = false
  }
}

function quickDeploy(e, p) {
  const target = e.target.value
  if (!target) return
  e.target.value = ''
  const routeMap = { ssh: '/deploy/ssh', docker: '/deploy/docker', k8s: '/deploy/k8s' }
  router.push({ path: routeMap[target], query: { project: p.job_name, tag: p.latest_tag } })
}

watch(interval, restartPolling, { immediate: false })

onMounted(() => {
  loadData()
  if (interval.value > 0) _timer = setInterval(loadData, interval.value)
})

onUnmounted(() => clearInterval(_timer))
</script>
