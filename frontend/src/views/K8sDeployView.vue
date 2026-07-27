<template>
  <div>
    <CiPipelineStatus
      :tag="pipelineData.tag"
      :pipeline-iid="pipelineData.iid"
      :created-at="pipelineData.created"
      :loading="pipelineLoading"
    />
    <div class="card">
      <h3>{{ $t('k8sDeploy.title') }}</h3>
      <div class="grid2" style="margin-bottom:12px">
        <div>
          <label>{{ $t('deploy.project') }}</label>
          <select v-model="selectedProject" @change="onProjectChange">
            <option v-for="p in projects" :key="p.job_name" :value="p.job_name">{{ p.job_name }}</option>
          </select>
        </div>
        <div>
          <label>{{ $t('deploy.tag') }} <span class="tag-info" v-if="tagState.totalPages > 1">({{ tagState.page }}/{{ tagState.totalPages }} {{ $t('tagPager.totalCount', { count: tagState.total }) }})</span></label>
          <TagPager
            v-model="selectedTag"
            :tags="tagState.tags"
            :page="tagState.page"
            :total-pages="tagState.totalPages"
            :total="tagState.total"
            :loading="tagState.loading"
            @page-change="changeTagPage"
          />
        </div>
        <div>
          <label>{{ $t('deploy.mode') }}</label>
          <select v-model="cdType" @change="onTypeChange">
            <option value="kubectl">kubectl SSH</option>
            <option value="helm">Helm</option>
            <option value="argocd">Argo CD</option>
            <option value="fluxcd">Flux CD</option>
          </select>
        </div>
        <div>
          <label>{{ $t('k8sDeploy.cluster') }}</label>
          <select v-model="clusterId">
            <option :value="0">{{ $t('k8sDeploy.selectCluster') }}</option>
            <option v-for="s in k8sServers" :key="s.id" :value="s.id">{{ s.name }} ({{ s.type }})</option>
          </select>
        </div>
      </div>
      <div style="margin-bottom:8px;font-size:11px;color:#667">{{ $t('k8sDeploy.namespaceHint') }}</div>
      <div v-if="cdType === 'kubectl' || cdType === 'helm'" style="margin-bottom:8px">
        <label>{{ $t('k8sDeploy.yamlPath') }}</label>
        <input v-model="path" placeholder="/opt/k8s/deploy.yaml">
      </div>
      <div v-if="cdType === 'argocd'" style="margin-bottom:8px">
        <label>{{ $t('k8sDeploy.apiUrl') }}</label>
        <input v-model="apiUrl" placeholder="https://argocd:30443">
      </div>
      <div>
        <label>{{ $t('deploy.notify') }}</label>
        <select v-model="botId">
          <option :value="0">{{ $t('bots.noNotify') }}</option>
          <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
        </select>
      </div>
      <button class="btn btn-green" style="margin-top:8px" @click="doDeploy" :disabled="loading">{{ $t('deploy.deploy') }}</button>
      <button class="btn btn-red" style="margin-left:8px" @click="doStop">{{ $t('deploy.stop') }}</button>
      <button
        class="btn btn-blue btn-sm"
        style="margin-left:8px"
        v-if="showMonitorBtn"
        @click="jumpToMonitor"
      >{{ $t('k8sDeploy.viewResources') }}</button>
      <pre class="output" v-text="output"></pre>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useDeploy } from '@/composables/useDeploy'
import CiPipelineStatus from '@/components/CiPipelineStatus.vue'
import TagPager from '@/components/TagPager.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const { t, locale } = useI18n()
const { toast } = useToast()
const { projects, selectedProject, pipelineData, pipelineLoading, tagState, selectedTag, output, loading, loadProjects, changeProject, changeTagPage, stream } = useDeploy()

const cdType = ref('kubectl')
const clusterId = ref(0)
const k8sServers = ref([])
const bots = ref([])
const botId = ref(0)
const path = ref('')
const apiUrl = ref('')
const showMonitorBtn = ref(false)
let _lastClusterId = 0

async function onProjectChange() {
  await changeProject(selectedProject.value)
}

async function loadServers() {
  try {
    const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const all = await r.json()
    k8sServers.value = all.filter(s => s.type === 'k8s')
  } catch (e) {}
}

async function loadBots() {
  try {
    const r = await fetch('/api/bots', { headers: auth.A() })
    if (r.ok) bots.value = await r.json()
  } catch (e) {}
}

function onTypeChange() {
  showMonitorBtn.value = false
}

async function doDeploy() {
  if (!selectedTag.value) return toast(t('deploy.noTag'), false)
  const cid = parseInt(clusterId.value) || 0
  if (!cid) return toast(t('deploy.selectCluster'), false)

  const body = {
    project: selectedProject.value,
    tag: selectedTag.value,
    cd_type: cdType.value,
    cluster_id: cid,
    path: path.value,
    api_url: apiUrl.value,
    bot_id: parseInt(botId.value) || 0,
    lang: locale.value
  }

  const success = await stream('/api/deploy-k8s-stream', body, {
    initialMsg: '',
    onEnd: (ok) => {
      toast(ok ? t('deploy.deploySuccess') : t('deploy.deployFailed'), ok)
      if (ok) {
        showMonitorBtn.value = true
        _lastClusterId = cid
      }
    },
    onError: () => toast(t('deploy.deployFailed'), false)
  })
}

async function doStop() {
  if (!confirm(t('deploy.confirmStop'))) return
  const cid = parseInt(clusterId.value) || 0
  if (!cid) return toast('请选择集群', false)
  const body = {
    project: selectedProject.value,
    deploy_type: 'k8s',
    server_ids: String(cid),
    target_path: path.value
  }
  try {
    const r = await fetch('/api/stop-k8s', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth.A() }, body: JSON.stringify(body) })
    const d = await r.json()
    output.value = d.output || ''
    toast(d.success ? t('deploy.stopSuccess') : '❌ ' + t('common.failed'), d.success)
  } catch (e) {
    toast(t('deploy.stopFailed'), false)
  }
}

function jumpToMonitor() {
  showMonitorBtn.value = false
  router.push({ path: '/monitor/app', query: { clusterId: _lastClusterId } })
}

onMounted(async () => {
  await Promise.all([loadProjects(), loadServers(), loadBots()])
  if (route.query.project) {
    selectedProject.value = route.query.project
  }
  if (selectedProject.value) {
    await changeProject(selectedProject.value)
  }
})
</script>
