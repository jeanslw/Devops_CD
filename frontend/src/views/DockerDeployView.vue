<template>
  <div class="deploy-layout">
    <div>
      <CiPipelineStatus
        :tag="pipelineData.tag"
        :pipeline-iid="pipelineData.iid"
        :created-at="pipelineData.created"
        :loading="pipelineLoading"
      />
    </div>
    <div>
      <div class="card">
        <h3>{{ $t('dockerDeploy.title') }}</h3>
        <div class="grid2">
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
            <select v-model="mode" @change="onModeChange">
              <option value="remote">{{ $t('dockerDeploy.composeRemote') }}</option>
              <option value="commands">{{ $t('dockerDeploy.customCommand') }}</option>
            </select>
          </div>
          <div>
            <label>{{ $t('deploy.server') }}</label>
            <MultiSelect v-model="selectedServers" :servers="dockerServers" />
          </div>
          <div v-if="mode === 'remote'">
            <label>{{ $t('dockerDeploy.appPath') }}</label>
            <input v-model="path" :placeholder="$t('dockerDeploy.appPathPlaceholder')">
          </div>
          <div v-if="mode === 'remote'">
            <label>{{ $t('dockerDeploy.envFile') }}</label>
            <input v-model="envFile" :placeholder="$t('dockerDeploy.envFilePlaceholder')">
          </div>
        </div>
        <div class="grid2" style="margin-bottom:8px">
          <div>
            <label>{{ $t('deploy.notify') }}</label>
            <select v-model="botId">
              <option :value="0">{{ $t('bots.noNotify') }}</option>
              <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }} ({{ b.type }})</option>
            </select>
          </div>
          <div>
            <label>{{ $t('deploy.note') }}</label>
            <input v-model="deployNote" :placeholder="$t('deploy.notePlaceholder')">
          </div>
        </div>
        <div v-if="mode === 'remote'" style="margin-bottom:8px">
          <label @click="yamlExpanded = !yamlExpanded" style="cursor:pointer;user-select:none">
            <span style="display:inline-block;width:14px;transition:transform 0.2s" :style="{ transform: yamlExpanded ? 'rotate(90deg)' : '' }">&#9654;</span>
            {{ $t('dockerDeploy.inlineYaml') }}
            <span v-if="!yamlExpanded && yamlContent" style="color:#999;font-weight:normal">（{{ $t('dockerDeploy.yamlFilled') }}）</span>
          </label>
          <textarea v-if="yamlExpanded" v-model="yamlContent" rows="14" :placeholder="$t('dockerDeploy.yamlPlaceholder')" style="width:100%;resize:vertical;font-family:monospace"></textarea>
        </div>
        <div v-if="mode === 'commands'" style="margin-bottom:8px">
          <label>{{ $t('deploy.commands') }}</label>
          <textarea v-model="commands" rows="6" style="width:100%;resize:vertical;font-family:monospace"></textarea>
        </div>
        <button class="btn btn-green" @click="doDeploy" :disabled="loading">{{ $t('deploy.deploy') }}</button>
        <button class="btn btn-red" style="margin-left:8px" @click="doStop">{{ $t('deploy.stop') }}</button>
        <button class="btn btn-orange" style="margin-left:8px" @click="doCancel" :disabled="!loading">{{ $t('deploy.cancel') }}</button>
        <button class="btn btn-blue" style="margin-left:8px" @click="doRollback" :disabled="loading || !selectedTag">{{ $t('deploy.rollbackToTag') }}</button>
        <pre class="output" v-text="output"></pre>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useDeploy } from '@/composables/useDeploy'
import { confirm } from '@/composables/useConfirm'
import CiPipelineStatus from '@/components/CiPipelineStatus.vue'
import TagPager from '@/components/TagPager.vue'
import MultiSelect from '@/components/MultiSelect.vue'

const route = useRoute()
const auth = useAuth()
const { t, locale } = useI18n()
const { toast } = useToast()
const { projects, selectedProject, pipelineData, pipelineLoading, tagState, selectedTag, output, loading, loadProjects, changeProject, changeTagPage, stream, cancelDeploy, rollbackStream } = useDeploy()

const mode = ref('remote')
const selectedServers = ref([])
const dockerServers = ref([])
const bots = ref([])
const botId = ref(0)
const path = ref('')
const envFile = ref('')
const commands = ref('')
const yamlContent = ref('')
const yamlExpanded = ref(false)
const deployNote = ref('')

async function onProjectChange() {
  await changeProject(selectedProject.value)
}

async function loadServers() {
  try {
    const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const all = await r.json()
    dockerServers.value = all.filter(s => s.type === 'docker')
  } catch (e) {}
}

async function loadBots() {
  try {
    const r = await fetch('/api/bots', { headers: auth.A() })
    if (r.ok) bots.value = await r.json()
  } catch (e) {}
}

function onModeChange() {}

async function doDeploy() {
  if (!selectedTag.value) return toast(t('deploy.noTagHint'), false)
  const sid = selectedServers.value.join(',')
  if (!sid) return toast(t('deploy.selectServer'), false)

  const body = {
    project: selectedProject.value,
    tag: selectedTag.value,
    deploy_type: 'compose',
    server_ids: sid,
    target_path: path.value,
    deploy_mode: mode.value,
    commands: commands.value,
    yaml_content: yamlContent.value,
    env_file: envFile.value,
    deploy_note: deployNote.value,
    bot_id: parseInt(botId.value) || 0,
    lang: locale.value
  }

  const success = await stream('/api/deploy-stream', body, {
    onEnd: (ok) => toast(ok ? t('deploy.deploySuccess') : t('deploy.deployFailed'), ok),
    onError: () => toast(t('deploy.deployFailed'), false),
    onPending: () => toast(t('deploy.submitPending'), true)
  })
}

async function doRollback() {
  if (!selectedProject.value) return toast(t('deploy.selectServerFirst'), false)
  if (!selectedTag.value) return toast(t('deploy.noTag'), false)
  if (!await confirm({ text: t('deploy.rollbackToTagConfirm', { project: selectedProject.value, tag: selectedTag.value }), danger: true })) return
  await rollbackStream(selectedProject.value, locale.value, 'compose', selectedTag.value, {
    initialMsg: '',
    onEnd: (ok) => toast(ok ? t('deploy.rollbackSuccess') : t('deploy.rollbackFailed'), ok),
    onError: () => toast(t('deploy.rollbackFailed'), false),
    onPending: () => toast(t('deploy.rollbackPending'), true)
  })
}

async function doCancel() {
  if (!await confirm({ text: t('deploy.confirmCancel'), danger: true })) return
  const d = await cancelDeploy(selectedProject.value)
  if (d.success) toast(t('deploy.cancelled'), true)
  else if (!d.success) toast(t('deploy.cancelFailed'), false)
}

async function doStop() {
  if (!await confirm({ text: t('deploy.confirmStop'), danger: true })) return
  const body = {
    project: selectedProject.value,
    tag: selectedTag.value,
    deploy_type: 'compose',
    server_ids: selectedServers.value.join(','),
    target_path: path.value
  }
  try {
    const r = await fetch('/api/stop', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth.A() }, body: JSON.stringify(body) })
    const d = await r.json()
    output.value = JSON.stringify(d, null, 2)
    toast(d.success ? t('deploy.stopSuccess') : t('deploy.stopFailed'), d.success)
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
})
</script>
