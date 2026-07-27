<template>
  <div class="card">
    <h3>{{ $t('servers.title') }}
      <button class="btn btn-green btn-sm" style="margin-left:12px" @click="showForm">{{ $t('servers.addServer') }}</button>
    </h3>
    <div style="margin-bottom:8px">
      <label style="display:inline;margin-right:4px">{{ $t('common.filter') }}</label>
      <input v-model="filter" :placeholder="$t('servers.filterPlaceholder')" style="width:auto;display:inline" @input="loadData">
    </div>
    <table>
      <thead><tr><th>{{ $t('servers.name') }}</th><th>{{ $t('servers.host') }}</th><th>{{ $t('servers.type') }}</th><th>{{ $t('servers.tags') }}</th><th>{{ $t('common.action') }}</th></tr></thead>
      <tbody>
        <tr v-if="loading"><td colspan="5" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
        <tr v-for="s in filteredServers" :key="s.id">
          <td>{{ s.name }}</td>
          <td>{{ s.host }}:{{ s.port }}</td>
          <td>{{ s.type }}</td>
          <td>
            <span v-for="t in (s.tags||'').split(',').filter(Boolean)" :key="t" class="badge badge-gitlab" style="margin:1px">{{ t }}</span>
          </td>
          <td>
            <button class="btn btn-edit btn-sm" style="margin-right:4px" @click="edit(s)">{{ $t('common.edit') }}</button>
            <button class="btn btn-red btn-sm" @click="del(s.id)">{{ $t('common.delete') }}</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- Form Card -->
  <div class="card" v-if="showFormCard">
    <h3>{{ editId ? $t('servers.editServer') : $t('servers.addServer') }}</h3>
    <input type="hidden" v-model="editId">
    <div class="grid2" style="margin-bottom:12px">
      <input v-model="form.name" :placeholder="$t('servers.form.name')">
      <input v-model="form.hostPort" :placeholder="$t('servers.form.hostPort')">
      <input v-model="form.user">
      <select v-model="form.type">
        <option value="ssh">SSH</option>
        <option value="docker">Docker</option>
        <option value="k8s">K8s</option>
      </select>
    </div>
    <div style="margin-bottom:12px">
      <label style="margin-bottom:4px;display:block">{{ $t('servers.sshAuth') }}</label>
      <select v-model="form.auth_type" style="width:auto;margin-bottom:8px">
        <option value="password">{{ $t('servers.passwordLogin') }}</option>
        <option value="key">{{ $t('servers.keyLogin') }}</option>
      </select>
      <input v-model="form.password" type="password" :placeholder="$t('servers.sshPassword')" v-if="form.auth_type==='password'" style="margin-bottom:8px">
      <textarea v-model="form.ssh_key" :placeholder="$t('servers.sshKey')" v-else style="height:100px;resize:vertical;font-family:monospace;font-size:12px;width:100%"></textarea>
    </div>
    <div style="margin-bottom:12px">
      <label style="margin-bottom:4px;display:block">{{ $t('servers.tags') }}</label>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px">
        <label v-for="t in existingTags" :key="t" class="tag-checkbox-label">
          <input type="checkbox" :value="t" v-model="form.tags"> <span>{{ t }}</span>
        </label>
      </div>
      <input v-model="tagInput" :placeholder="$t('servers.newTagPlaceholder')" style="width:160px;display:inline;margin:0" @keydown.enter.prevent="addCustomTag">
      <button class="btn btn-sm" style="margin:0 0 0 4px" @click="addCustomTag">{{ $t('servers.addTag') }}</button>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">
        <label v-for="t in customTags" :key="t" class="tag-checkbox-label" style="background:rgba(74,222,128,0.1);color:var(--accent);border-color:var(--accent)">
          <input type="checkbox" :value="t" v-model="form.tags" checked> <span>{{ t }}</span>
        </label>
      </div>
    </div>
    <button class="btn btn-green" @click="save">{{ editId ? '💾 ' + $t('common.save') : '＋ ' + $t('common.add') }}</button>
    <button class="btn btn-edit" v-if="editId" @click="cancelForm" style="margin-left:8px">{{ $t('common.cancel') }}</button>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'

const auth = useAuth()
const { t } = useI18n()
const { toast } = useToast()

const servers = ref([])
const loading = ref(true)
const filter = ref('')
const showFormCard = ref(false)
const editId = ref('')
const form = reactive({ name: '', hostPort: '', user: 'root', type: 'ssh', auth_type: 'password', password: '', ssh_key: '', tags: [] })
const tagInput = ref('')
const existingTags = ref([])
const customTags = reactive([])

const filteredServers = computed(() => {
  if (!filter.value) return servers.value
  return servers.value.filter(s => (s.tags || '').toLowerCase().includes(filter.value.toLowerCase()))
})

async function loadData() {
  loading.value = true
  try {
    const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    servers.value = await r.json()
  } catch (e) {} finally { loading.value = false }
}

async function loadTags() {
  try {
    const r = await fetch('/api/tags', { headers: auth.A() })
    if (auth.handle401(r)) return
    const tags = await r.json()
    existingTags.value = tags.map(t => t.name)
  } catch (e) {}
}

function showForm() {
  editId.value = ''
  Object.assign(form, { name: '', hostPort: '', user: 'root', type: 'ssh', auth_type: 'password', password: '', ssh_key: '', tags: [] })
  customTags.splice(0, customTags.length)
  tagInput.value = ''
  showFormCard.value = true
}

function cancelForm() {
  showFormCard.value = false
  editId.value = ''
}

async function save() {
  const n = form.name.trim()
  const h = form.hostPort.trim()
  if (!n || !h) return toast(t('servers.fillNameAndHost'), false)
  const hostParts = h.split(':')
  const host = hostParts[0]
  const port = parseInt(hostParts[1] || '22')
  const body = {
    name: n, host, port, user: form.user.trim() || 'root',
    auth_type: form.auth_type, password: form.auth_type === 'password' ? form.password : '',
    ssh_key: form.auth_type === 'key' ? form.ssh_key.trim() : '',
    tags: form.tags.join(','), type: form.type
  }
  const isEdit = !!editId.value
  const url = isEdit ? `/api/servers/${editId.value}` : '/api/servers'
  const method = isEdit ? 'PUT' : 'POST'
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify(body)
  })
  if (auth.handle401(r)) return
  const d = await r.json()
  toast(d.success ? (isEdit ? t('servers.updated') : t('servers.added')) : t('common.failed'), d.success)
  if (d.success) { cancelForm(); loadData() }
}

function addCustomTag() {
  const t = tagInput.value.trim()
  if (!t) return
  if (!form.tags.includes(t)) form.tags.push(t)
  if (!customTags.includes(t)) customTags.push(t)
  tagInput.value = ''
}

async function edit(s) {
  form.name = s.name
  form.hostPort = s.host + ':' + s.port
  form.user = s.user
  form.type = s.type || 'ssh'
  form.auth_type = s.auth_type || 'password'
  form.password = s.password || ''
  form.ssh_key = s.ssh_key || ''
  form.tags = (s.tags || '').split(',').filter(Boolean).map(t => t.trim())
  editId.value = String(s.id)
  customTags.splice(0, customTags.length)
  tagInput.value = ''
  showFormCard.value = true
  await loadTags()
  // Mark non-existing tags as custom
  form.tags.forEach(t => {
    if (!existingTags.value.includes(t) && !customTags.includes(t)) {
      customTags.push(t)
    }
  })
}

async function del(id) {
  if (!confirm(t('servers.confirmDelete'))) return
  const r = await fetch(`/api/servers/${id}`, { method: 'DELETE', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('servers.deleted'), true)
  loadData()
}

onMounted(() => { loadData(); loadTags() })
</script>
