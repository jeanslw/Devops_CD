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
          <select v-model="cdType">
              <option value="kubectl">kubectl SSH</option>
              <option value="helm">Helm</option>
              <option value="argocd">Argo CD</option>
              <option value="fluxcd">Flux CD</option>
            </select>
        </div>
        <div>
          <label>{{ $t('k8sDeploy.cluster') }}</label>
          <div style="display:flex;gap:8px;align-items:center">
            <select v-model="clusterId">
              <option :value="0">{{ $t('k8sDeploy.selectCluster') }}</option>
              <option v-for="s in k8sServers" :key="s.id" :value="s.id">{{ s.name }} ({{ s.type }})</option>
            </select>
          </div>
        </div>
      </div>
      <div v-if="cdType === 'fluxcd'" style="margin-bottom:8px;font-size:11px;color:#667">Flux CD {{ $t('k8sDeploy.fluxNamespaceHint') }}</div>
      <div v-if="cdType === 'fluxcd'" class="grid2" style="margin-bottom:8px">
        <div>
          <label>{{ $t('k8sDeploy.fluxResource') }} <span style="color:#e5484d">*</span></label>
          <input v-model="targetApp" :placeholder="$t('k8sDeploy.fluxResourcePlaceholder')">
        </div>
        <div>
          <label>{{ $t('k8sDeploy.fluxKind') }}</label>
          <select v-model="fluxKind">
            <option value="helmrelease">HelmRelease</option>
            <option value="kustomization">Kustomization</option>
          </select>
        </div>
        <div>
          <label>{{ $t('k8sDeploy.fluxPath') }}</label>
          <input v-model="path" placeholder="./">
        </div>
      </div>
      <div v-if="cdType === 'argocd'" class="grid2" style="margin-bottom:8px">
        <div>
          <label>{{ $t('k8sDeploy.apiUrl') }}</label>
          <input v-model="apiUrl" placeholder="https://argocd:30443">
        </div>
        <div>
          <label>{{ $t('k8sDeploy.namespace') }}</label>
          <input v-model="k8sNs" placeholder="namespace">
        </div>
      </div>
      <!-- kubectl:默认 set image;YAML apply 作为高级模式 -->
      <div v-if="cdType === 'kubectl'" class="grid2" style="margin-bottom:8px">
        <div>
          <label>{{ $t('k8sDeploy.kubectlMode') }}</label>
          <select v-model="kubectlMode">
            <option value="setimage">{{ $t('k8sDeploy.modeSetImage') }}</option>
            <option value="apply">{{ $t('k8sDeploy.modeApply') }}</option>
          </select>
        </div>
        <div>
          <label>{{ $t('k8sDeploy.namespace') }}</label>
          <input v-model="k8sNs" placeholder="namespace">
        </div>
        <div v-if="kubectlMode === 'setimage'">
          <label>{{ $t('k8sDeploy.container') }} <span style="color:#e5484d">*</span></label>
          <input v-model="container" :placeholder="$t('k8sDeploy.containerPlaceholder')">
        </div>
        <div v-if="kubectlMode === 'setimage'">
          <label>{{ $t('k8sDeploy.targetApp') }}</label>
          <input v-model="targetApp" :placeholder="defaultAppName || $t('k8sDeploy.targetAppPlaceholder')">
        </div>
        <div v-if="kubectlMode === 'apply'" style="grid-column:1 / -1">
          <label>{{ $t('k8sDeploy.yamlPath') }} <span style="color:#e5484d">*</span></label>
          <input v-model="path" placeholder="/opt/k8s/deploy.yaml">
        </div>
        <div v-if="kubectlMode === 'setimage'" style="grid-column:1 / -1">
          <label style="display:flex;align-items:center;gap:6px;text-transform:none">
            <input type="checkbox" v-model="useDigest" style="width:auto">
            {{ $t('k8sDeploy.useDigest') }}
          </label>
          <div style="font-size:11px;color:var(--text-dim)">{{ $t('k8sDeploy.useDigestHint') }}</div>
        </div>
        <div v-if="kubectlMode === 'apply'" style="grid-column:1 / -1">
          <label style="display:flex;align-items:center;gap:6px;text-transform:none">
            <input type="checkbox" v-model="stopManifest" style="width:auto">
            {{ $t('k8sDeploy.stopManifest') }}
          </label>
          <div style="font-size:11px;color:#e5484d">{{ $t('k8sDeploy.stopManifestHint') }}</div>
        </div>
      </div>
      <div v-if="cdType === 'helm'" class="grid2" style="margin-bottom:8px">
        <div>
          <label>{{ $t('k8sDeploy.yamlPath') }}</label>
          <input v-model="path" placeholder="/opt/charts/app">
        </div>
        <div>
          <label>{{ $t('k8sDeploy.namespace') }}</label>
          <input v-model="k8sNs" placeholder="namespace">
        </div>
        <div>
          <label>{{ $t('k8sDeploy.helmRepoKey') }}</label>
          <input v-model="helmRepoKey" placeholder="image.repository">
        </div>
        <div>
          <label>{{ $t('k8sDeploy.helmTagKey') }}</label>
          <input v-model="helmTagKey" placeholder="image.tag">
        </div>
      </div>
      <div style="margin-bottom:8px;font-size:11px;color:#667">{{ $t('k8sDeploy.namespaceHint') }}</div>
      <div class="grid2" style="margin-bottom:8px">
        <div>
          <label>{{ $t('deploy.notify') }}</label>
          <select v-model="botId">
            <option :value="0">{{ $t('bots.noNotify') }}</option>
            <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
          </select>
        </div>
        <div>
          <label>{{ $t('deploy.note') }}</label>
          <input v-model="deployNote" :placeholder="$t('deploy.notePlaceholder')">
        </div>
        <div>
          <label>{{ $t('deploy.scheduleLabel') }}</label>
          <DateTimePicker v-model="schedule" />
          <div style="font-size:11px;color:var(--text-dim)">{{ $t('deploy.scheduleHint') }}</div>
        </div>
      </div>
      <div class="action-row">
        <button class="btn btn-green" @click="doDeploy" :disabled="loading">{{ $t('deploy.deploy') }}</button>
        <button class="btn btn-red" @click="doStop">{{ $t('deploy.stop') }}</button>
        <button class="btn btn-orange" @click="doCancel" :disabled="!loading">{{ $t('deploy.cancel') }}</button>
        <button class="btn btn-blue btn-wide" @click="doRollbackToTag" :disabled="loading || !selectedTag" :title="$t('deploy.rollbackToTagTip')">{{ $t('deploy.rollbackToTag') }}</button>
        <button v-if="cdType !== 'fluxcd'" class="btn btn-blue btn-wide" @click="doRollbackOneStep" :disabled="loading" :title="$t('deploy.rollbackOneStepTip')">{{ $t('deploy.rollbackOneStep') }}</button>
        <span class="btn-tip">{{ $t('deploy.rollbackTip') }}</span>
      </div>
      <ApprovalFlowPanel
        v-if="panelApprovalId"
        :approval-id="panelApprovalId"
        @close="onPanelClose"
      />
      <ApprovalActiveList
        v-else-if="activeApprovals.length"
        :items="activeApprovals"
        @open="openPanel"
      />
      <pre class="output" v-if="!panelApprovalId" v-text="output"></pre>
    </div>

    <!-- 预检弹窗 -->
    <div v-if="checkModal.show" class="modal-overlay" @click.self="checkModal.show = false">
      <div class="modal-box">
        <h4>{{ $t('deploy_check.title') }}</h4>
        <p style="margin:12px 0;white-space:pre-wrap;font-size:13px;color:#ff9800;line-height:1.6">{{ checkModal.text }}</p>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn btn-green" :disabled="checkModal.confirming" @click="checkModal.confirm">
            {{ checkModal.confirming ? '...' : $t('deploy_check.confirm') }}
          </button>
          <button class="btn" @click="checkModal.cancel">{{ $t('deploy_check.cancel') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useDeploy } from '@/composables/useDeploy'
import { useApprovalPanel } from '@/composables/useApprovalPanel'
import { confirm } from '@/composables/useConfirm'
import CiPipelineStatus from '@/components/CiPipelineStatus.vue'
import TagPager from '@/components/TagPager.vue'
import ApprovalFlowPanel from '@/components/ApprovalFlowPanel.vue'
import ApprovalActiveList from '@/components/ApprovalActiveList.vue'
import DateTimePicker from '@/components/DateTimePicker.vue'
import { localInputToUtc } from '@/utils/time.js'

const route = useRoute()
const auth = useAuth()
const { t, locale } = useI18n()
const { toast } = useToast()
const { projects, selectedProject, pipelineData, pipelineLoading, tagState, selectedTag, output, loading, loadProjects, changeProject, changeTagPage, stream, cancelDeploy, rollbackStream } = useDeploy()
const {
  panelApprovalId, activeApprovals,
  openPanel, refreshActive, onPending, onPanelClose, resetPanel,
} = useApprovalPanel(auth, selectedProject, 'k8s')

const cdType = ref('kubectl')
const clusterId = ref(0)
const k8sServers = ref([])
const bots = ref([])
const botId = ref(0)
const path = ref('')
const apiUrl = ref('')
const k8sNs = ref('default')
const deployNote = ref('')
const schedule = ref('')
// kubectl 主路径:set image;YAML apply 作为高级模式保留
const kubectlMode = ref('setimage')
const container = ref('')
const targetApp = ref('')
const fluxKind = ref('helmrelease')
const helmRepoKey = ref('')
const helmTagKey = ref('')
const useDigest = ref(false)
const stopManifest = ref(false)
const defaultAppName = computed(() => (selectedProject.value || '').split('/').pop() || '')

const checkModal = reactive({
  show: false, text: '', warning: '', confirming: false,
  resolve: null,
  confirm() { this.show = false; this.resolve?.(true) },
  cancel() { this.show = false; this.resolve?.(false) },
})
function showCheckModal(text) {
  return new Promise((resolve) => {
    checkModal.text = text
    checkModal.resolve = resolve
    checkModal.show = true
  })
}

async function checkDeploy(body) {
  // 没有 YAML 路径跳过预检;FluxCD 走 SSH 发现
  if (!body.path && body.cd_type !== 'fluxcd') return true

  try {
    const r = await fetch('/api/deploy-k8s-check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...auth.A() },
      body: JSON.stringify({
        project: body.project,
        cd_type: body.cd_type,
        cluster_id: body.cluster_id,
        path: body.path,
        api_url: body.api_url,
        k8s_ns: body.k8s_ns || '',
      })
    })

    if (!r.ok) return true  // 预检失败不阻断,直接放行

    const data = await r.json()
    // 只有 warning_severe_new(严重不匹配 + K8S 不存在)才弹窗
    if (data.warning !== 'warning_severe_new') return true

    const text = t(`deploy_check.${data.warning}`, {
      yaml: data.yaml_deploy_name,
      project: data.project_name,
    })
    return await showCheckModal(text)
  } catch (e) {
    return true  // 网络异常不阻断
  }
}

async function onProjectChange() {
  resetPanel()
  await changeProject(selectedProject.value)
  await refreshActive()
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

async function doDeploy() {
  if (!selectedTag.value) return toast(t('deploy.noTag'), false)
  const cid = parseInt(clusterId.value) || 0
  if (!cid) return toast(t('deploy.selectCluster'), false)
  // kubectl 主路径必须有容器名;apply / helm 必须有路径
  if (cdType.value === 'kubectl') {
    if (kubectlMode.value === 'setimage' && !container.value.trim()) return toast(t('k8sDeploy.containerRequired'), false)
    if (kubectlMode.value === 'apply' && !path.value.trim()) return toast(t('k8sDeploy.pathRequired'), false)
  }
  if (cdType.value === 'helm' && !path.value.trim()) return toast(t('k8sDeploy.pathRequired'), false)
  if (cdType.value === 'fluxcd' && !targetApp.value.trim()) return toast(t('k8sDeploy.fluxResourceRequired'), false)

  const body = {
    project: selectedProject.value,
    tag: selectedTag.value,
    cd_type: cdType.value,
    cluster_id: cid,
    // set image 模式不使用 YAML,避免切换模式后残留的路径触发预检
    path: cdType.value === 'kubectl' && kubectlMode.value === 'setimage' ? '' : path.value,
    api_url: apiUrl.value,
    k8s_ns: k8sNs.value.trim(),
    image_mode: cdType.value === 'kubectl' ? kubectlMode.value : '',
    container: cdType.value === 'kubectl' && kubectlMode.value === 'setimage' ? container.value.trim() : '',
    target_app: cdType.value === 'fluxcd'
      ? targetApp.value.trim()
      : (cdType.value === 'kubectl' && kubectlMode.value === 'setimage' ? targetApp.value.trim() : ''),
    flux_kind: cdType.value === 'fluxcd' ? fluxKind.value : '',
    helm_repo_key: cdType.value === 'helm' ? helmRepoKey.value.trim() : '',
    helm_tag_key: cdType.value === 'helm' ? helmTagKey.value.trim() : '',
    use_digest: cdType.value === 'kubectl' && kubectlMode.value === 'setimage' && useDigest.value,
    deploy_note: deployNote.value,
    scheduled_at: localInputToUtc(schedule.value),
    bot_id: parseInt(botId.value) || 0,
    lang: locale.value
  }

  // 预检:YAML 名称 vs 项目名
  const ok = await checkDeploy(body)
  if (!ok) return

  const success = await stream('/api/deploy-k8s-stream', body, {
    onEnd: (ok) => toast(ok ? t('deploy.deploySuccess') : t('deploy.deployFailed'), ok),
    onError: () => toast(t('deploy.deployFailed'), false),
    onPending: (id, scheduled) => { toast(scheduled ? t('deploy.submitScheduled') : t('deploy.submitPending'), true); onPending(id) }
  })
}

async function doRollbackToTag() {
  if (!selectedProject.value) return toast(t('deploy.selectServerFirst'), false)
  if (!selectedTag.value) return toast(t('deploy.noTag'), false)
  if (!await confirm({ text: t('deploy.rollbackToTagConfirm', { project: selectedProject.value, tag: selectedTag.value }), danger: true })) return
  await rollbackStream(selectedProject.value, locale.value, 'k8s/' + cdType.value, selectedTag.value, deployNote.value, {
    initialMsg: '',
    onEnd: (ok) => toast(ok ? t('deploy.rollbackSuccess') : t('deploy.rollbackFailed'), ok),
    onError: () => toast(t('deploy.rollbackFailed'), false),
    onPending: (id) => { toast(t('deploy.rollbackPending'), true); onPending(id) }
  })
}

async function doRollbackOneStep() {
  if (!selectedProject.value) return toast(t('deploy.selectServerFirst'), false)
  if (!await confirm({ text: t('deploy.rollbackOneStepConfirm', { project: selectedProject.value }), danger: true })) return
  await rollbackStream(selectedProject.value, locale.value, 'k8s/' + cdType.value, '', deployNote.value, {
    initialMsg: '',
    onEnd: (ok) => toast(ok ? t('deploy.rollbackSuccess') : t('deploy.rollbackFailed'), ok),
    onError: () => toast(t('deploy.rollbackFailed'), false),
    onPending: (id) => { toast(t('deploy.rollbackPending'), true); onPending(id) }
  })
}

async function doCancel() {
  if (!await confirm({ text: t('deploy.confirmCancel'), danger: true })) return
  const d = await cancelDeploy(selectedProject.value)
  if (d.success) toast(t('deploy.cancelled'), true)
  else toast(t('deploy.cancelFailed'), false)
}

async function doStop() {
  const manifestStop = cdType.value === 'kubectl' && kubectlMode.value === 'apply' && stopManifest.value
  if (!await confirm({ text: t(manifestStop ? 'deploy.confirmStopManifest' : 'deploy.confirmStop'), danger: true })) return
  const cid = parseInt(clusterId.value) || 0
  if (!cid) return toast('请选择集群', false)
  const body = {
    project: selectedProject.value,
    deploy_type: 'k8s',
    server_ids: String(cid),
    target_path: path.value,
    cd_type: cdType.value,
    api_url: apiUrl.value,
    k8s_ns: k8sNs.value.trim(),
    // 停止与部署同源:显式工作负载名优先,Flux 还需资源类型
    k8s_deploy: targetApp.value.trim(),
    target_app: targetApp.value.trim(),
    flux_kind: cdType.value === 'fluxcd' ? fluxKind.value : '',
    stop_mode: manifestStop ? 'manifest' : '',
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

onMounted(async () => {
  await Promise.all([loadProjects(), loadServers(), loadBots()])
  if (route.query.project) {
    selectedProject.value = route.query.project
  }
  if (selectedProject.value) {
    await changeProject(selectedProject.value)
  }
  if (route.query.approval) {
    panelApprovalId.value = Number(route.query.approval) || 0
  } else {
    await refreshActive()
  }
})

watch(() => route.query.approval, (v) => {
  if (v) panelApprovalId.value = Number(v) || 0
})
</script>
