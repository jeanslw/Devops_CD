<template>
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0">{{ $t('alerts.title') }}</h3>
      <button class="btn btn-sm" @click="refresh">{{ $t('alerts.checkNow') }}</button>
    </div>

    <!-- 新建/编辑表单 -->
    <div class="grid2" style="margin-bottom:12px;gap:8px">
      <select v-model="form.target_type" @change="onTargetChange">
        <option value="system">{{ $t('alerts.systemResource') }}</option>
        <option value="app">{{ $t('alerts.appResource') }}</option>
        <option value="custom">{{ $t('alerts.customResource') }}</option>
      </select>
      <select v-model="form.resource_type">
        <template v-for="r in resourceOptions" :key="r.value">
          <optgroup v-if="r.children && r.children.length" :label="locale === 'zh' ? r.label_zh : r.label_en">
            <option v-for="c in r.children" :key="c.value" :value="c.value">
              {{ locale === 'zh' ? c.label_zh : c.label_en }}
            </option>
          </optgroup>
          <option v-else :value="r.value">
            {{ locale === 'zh' ? r.label_zh : r.label_en }}
          </option>
        </template>
      </select>
      <input v-model.number="form.threshold" type="number" min="1" max="100" :placeholder="$t('alerts.threshold')">
      <select v-model="form.bot_id">
        <option value="0">{{ $t('alerts.noBot') }}</option>
        <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
      </select>
    </div>
    <div style="margin-bottom:8px">
      <label style="font-size:12px;color:#888;display:block;margin-bottom:2px">{{ $t('alerts.servers') }}</label>
      <input v-model="form.name" :placeholder="$t('alerts.ruleName')" style="margin-bottom:8px">
      <MultiSelect v-model="selectedServerIds" :servers="allServers" style="margin-bottom:8px" />
      <div style="font-size:11px;color:#888;margin-bottom:8px">{{ $t('alerts.serverHint') }}</div>
      <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#888">
          <input type="checkbox" v-model="form.enabled"> {{ $t('alerts.enabled') }}
        </label>
        <label style="font-size:12px;color:#888;display:flex;align-items:center;gap:4px">
          {{ $t('alerts.duration') }}
          <select v-model.number="form.duration_minutes" style="width:80px;padding:3px 4px;font-size:12px;margin:0">
            <option :value="0">{{ $t('alerts.immediate') }}</option>
            <option :value="5">5 {{ $t('alerts.min') }}</option>
            <option :value="10">10 {{ $t('alerts.min') }}</option>
            <option :value="30">30 {{ $t('alerts.min') }}</option>
          </select>
        </label>
        <label style="font-size:12px;color:#888;display:flex;align-items:center;gap:4px">
          {{ $t('alerts.cooldown') }}
          <input v-model.number="form.cooldown_minutes" type="number" min="1" max="1440" style="width:60px;padding:2px 4px;font-size:12px;margin:0">
          {{ $t('alerts.min') }}
        </label>
      </div>
      <textarea v-model="form.template" :placeholder="$t('alerts.templatePlaceholder')" rows="5" style="width:100%;font-family:monospace;font-size:12px;box-sizing:border-box"></textarea>
    </div>
    <div style="margin-bottom:12px">
      <button class="btn btn-green" @click="save">{{ editingId ? $t('alerts.update') : $t('alerts.create') }}</button>
      <button v-if="editingId" class="btn" @click="cancelEdit" style="margin-left:8px">{{ $t('alerts.cancel') }}</button>
    </div>

    <!-- 规则列表 -->
    <table>
      <thead>
        <tr>
          <th>{{ $t('alerts.ruleName') }}</th>
          <th>{{ $t('alerts.targetType') }}</th>
          <th>{{ $t('alerts.resource') }}</th>
          <th>{{ $t('alerts.threshold') }}</th>
          <th>{{ $t('alerts.duration') }}</th>
          <th>{{ $t('alerts.bot') }}</th>
          <th>{{ $t('alerts.enabled') }}</th>
          <th>{{ $t('common.action') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading"><td colspan="8" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
        <tr v-for="r in rules" :key="r.id">
          <td>{{ r.name }}</td>
          <td>
            <span v-if="r.target_type === 'system'" :style="{color:'var(--accent)'}">{{ $t('alerts.systemResource') }}</span>
            <span v-else-if="r.target_type === 'custom'" :style="{color:'var(--orange)'}">{{ $t('alerts.customResource') }}</span>
            <span v-else :style="{color:'var(--green)'}">{{ $t('alerts.appResource') }}</span>
          </td>
          <td>{{ getResourceLabel(r.resource_type) }}</td>
          <td><code>≥ {{ r.threshold }}%</code></td>
          <td>
            <span v-if="r.duration_minutes">{{ r.duration_minutes }}{{ $t('alerts.min') }}</span>
            <span v-else style="color:#888">{{ $t('alerts.immediate') }}</span>
          </td>
          <td>{{ r.bot_name || ('#' + r.bot_id) }}</td>
          <td><span v-if="r.enabled" :style="{color:'var(--green)'}">{{ $t('alerts.on') }}</span><span v-else :style="{color:'var(--text-dim)'}">{{ $t('alerts.off') }}</span></td>
          <td>
            <button class="btn btn-sm" @click="edit(r)">{{ $t('common.edit') }}</button>
            <button class="btn btn-red btn-sm" @click="del(r.id)">{{ $t('common.delete') }}</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import MultiSelect from '@/components/MultiSelect.vue'

const auth = useAuth()
const { t, locale } = useI18n()
const { toast } = useToast()

const rules = ref([])
const bots = ref([])
const allServers = ref([])
const loading = ref(true)
const editingId = ref(0)
const selectedServerIds = ref([])
const resourceTypes = ref({ system: [], app: [] })

const form = reactive({
  name: '', target_type: 'system', resource_type: '', server_ids: '',
  threshold: 80, bot_id: 0, template: '', enabled: true, cooldown_minutes: 10, duration_minutes: 0
})

const resourceOptions = computed(() => {
  const items = resourceTypes.value[form.target_type] || []
  // 自定义类型：返回包含 children 的层级结构（模板用 optgroup 渲染）
  if (form.target_type === 'custom') return items
  return items
})

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
    const [r1, r2, r3] = await Promise.all([
      fetch('/api/alerts', { headers: auth.A() }),
      fetch('/api/bots', { headers: auth.A() }),
      fetch('/api/alerts/resource-types', { headers: auth.A() })
    ])
    if (auth.handle401(r1)) return
    rules.value = await r1.json()
    bots.value = await r2.json()
    const rt = await r3.json()
    resourceTypes.value = rt
    if (!form.resource_type && rt[form.target_type]?.length) {
      form.resource_type = rt[form.target_type][0].value
    }
    // 补 bot name
    const botMap = {}
    bots.value.forEach(b => botMap[b.id] = b.name)
    rules.value.forEach(r => r.bot_name = botMap[r.bot_id] || '')
  } catch (e) { console.error(e) }
  finally { loading.value = false }
}

function onTargetChange() {
  const opts = resourceTypes.value[form.target_type] || []
  if (form.target_type === 'custom') {
    // 自定义：取第一个有子指标的组的第一个子指标
    for (const g of opts) {
      if (g.children && g.children.length) {
        form.resource_type = g.children[0].value
        return
      }
    }
    form.resource_type = opts[0]?.value || ''
  } else {
    if (opts.length) form.resource_type = opts[0].value
  }
}

function getResourceLabel(type) {
  const all = [
    ...(resourceTypes.value.system || []),
    ...(resourceTypes.value.app || []),
    ...(resourceTypes.value.custom || []),
  ]
  const found = all.find(r => r.value === type)
  if (found) return locale.value === 'zh' ? found.label_zh : found.label_en
  // 搜索子指标
  for (const g of resourceTypes.value.custom || []) {
    if (!g.children) continue
    const c = g.children.find(ch => ch.value === type)
    if (c) {
      const gLabel = locale.value === 'zh' ? g.label_zh : g.label_en
      const cLabel = locale.value === 'zh' ? c.label_zh : c.label_en
      return `${gLabel} › ${cLabel}`
    }
  }
  return type
}

async function save() {
  if (!form.name.trim() || !form.resource_type || !form.bot_id) {
    return toast(t('alerts.fillRequired'), false)
  }
  const payload = {
    name: form.name.trim(), target_type: form.target_type, resource_type: form.resource_type,
    server_ids: selectedServerIds.value.join(','), threshold: form.threshold, bot_id: form.bot_id,
    template: form.template, enabled: form.enabled, cooldown_minutes: form.cooldown_minutes,
    duration_minutes: form.duration_minutes
  }
  const url = editingId.value ? `/api/alerts/${editingId.value}` : '/api/alerts'
  const method = editingId.value ? 'PUT' : 'POST'
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify(payload)
  })
  if (auth.handle401(r)) return
  toast(editingId.value ? t('alerts.updated') : t('alerts.created'), true)
  cancelEdit()
  loadData()
}

function edit(r) {
  editingId.value = r.id
  Object.assign(form, {
    name: r.name, target_type: r.target_type, resource_type: r.resource_type,
    threshold: r.threshold, bot_id: r.bot_id,
    template: r.template || '', enabled: !!r.enabled, cooldown_minutes: r.cooldown_minutes || 10,
    duration_minutes: r.duration_minutes || 0
  })
  selectedServerIds.value = (r.server_ids || '').split(',').filter(Boolean).map(Number)
}

function cancelEdit() {
  editingId.value = 0
  form.name = ''; form.server_ids = ''; form.threshold = 80; form.bot_id = 0
  form.template = ''; form.enabled = true; form.cooldown_minutes = 10; form.duration_minutes = 0
  selectedServerIds.value = []
  onTargetChange()
}

async function del(id) {
  if (!confirm(t('alerts.confirmDelete'))) return
  const r = await fetch(`/api/alerts/${id}`, { method: 'DELETE', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('alerts.deleted'), true)
  loadData()
}

async function refresh() {
  const r = await fetch('/api/alerts/check', { method: 'POST', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('alerts.checkDone'), true)
}

onMounted(() => { loadData(); loadServers() })
</script>
