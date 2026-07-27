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
        <h3>{{ $t('sshDeploy.title') }}</h3>
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
              <option value="commands">{{ $t('sshDeploy.customCommand') }}</option>
              <option value="ansible">{{ $t('sshDeploy.ansible') }}</option>
            </select>
          </div>
          <div>
            <label>{{ $t('deploy.server') }}</label>
            <MultiSelect v-model="selectedServers" :servers="allServers" />
          </div>
          <div v-if="mode === 'ansible'">
            <label>{{ $t('sshDeploy.playbook') }}</label>
            <input v-model="path" placeholder="/opt/ansible/deploy.yml">
          </div>
          <div v-if="mode === 'ansible'">
            <label>{{ $t('sshDeploy.inventory') }}</label>
            <input v-model="inventory" :placeholder="$t('sshDeploy.inventoryPlaceholder')">
          </div>
          <div>
            <label>{{ $t('deploy.notify') }}</label>
            <select v-model="botId">
              <option :value="0">{{ $t('bots.noNotify') }}</option>
              <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
            </select>
          </div>
        </div>
        <div v-if="mode === 'commands'" style="margin-bottom:8px">
          <label>{{ $t('deploy.commands') }}</label>
          <textarea v-model="commands" rows="6" :placeholder="$t('sshDeploy.commandsPlaceholder')" style="width:100%;resize:vertical;font-family:monospace"></textarea>
        </div>
        <div style="margin-bottom:8px">
          <label>{{ $t('deploy.filterRule') }}</label>
          <input v-model="filter" :placeholder="$t('deploy.filterPlaceholder')">
        </div>
        <button class="btn btn-green" @click="doDeploy" :disabled="loading">{{ $t('deploy.deploy') }}</button>
        <button class="btn btn-red" style="margin-left:8px" @click="doStop">{{ $t('deploy.stop') }}</button>
        <pre class="output" v-text="output"></pre>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useDeploy } from '@/composables/useDeploy'
import CiPipelineStatus from '@/components/CiPipelineStatus.vue'
import TagPager from '@/components/TagPager.vue'
import MultiSelect from '@/components/MultiSelect.vue'

const route = useRoute()
const auth = useAuth()
const { t, locale } = useI18n()
const { toast } = useToast()
const { projects, selectedProject, pipelineData, pipelineLoading, tagState, selectedTag, output, loading, loadProjects, loadPipeline, changeProject, changeTagPage, stream } = useDeploy()

const mode = ref('commands')
const selectedServers = ref([])
const allServers = ref([])
const bots = ref([])
const botId = ref(0)
const path = ref('')
const inventory = ref('')
const commands = ref('')
const filter = ref('')

async function onProjectChange() {
  await changeProject(selectedProject.value)
}

async function loadServers() {
  try {
    const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    allServers.value = await r.json()
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
  if (!selectedTag.value) return toast(t('deploy.noTag'), false)
  const sid = selectedServers.value.join(',')
  if (!sid) return toast(t('deploy.selectServer'), false)
  let cmdStr = commands.value
  if (inventory.value) cmdStr += '|INV|' + inventory.value
  cmdStr += '|FILTER|' + (filter.value || '')

  const body = {
    project: selectedProject.value,
    tag: selectedTag.value,
    deploy_type: 'ssh',
    server_ids: sid,
    deploy_mode: mode.value,
    target_path: path.value,
    commands: cmdStr,
    bot_id: parseInt(botId.value) || 0,
    lang: locale.value
  }

  const success = await stream('/api/deploy-stream', body, {
    onEnd: (ok) => toast(ok ? t('deploy.deploySuccess') : t('deploy.deployFailed'), ok),
    onError: () => toast(t('deploy.deployFailed'), false)
  })
}

async function doStop() {
  if (!confirm(t('deploy.confirmStop'))) return
  const sid = selectedServers.value.join(',')
  if (!sid) return toast(t('deploy.selectServerFirst'), false)
  const body = {
    project: selectedProject.value,
    tag: selectedTag.value,
    deploy_type: 'ssh',
    server_ids: sid,
    target_path: path.value,
    commands: commands.value,
  }
  try {
    const r = await fetch('/api/stop', { method: 'POST', headers: { 'Content-Type': 'application/json', ...auth.A() }, body: JSON.stringify(body) })
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
})
</script>
