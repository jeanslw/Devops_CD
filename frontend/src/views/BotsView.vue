<template>
  <div class="card">
    <h3>{{ $t('bots.title') }}</h3>
    <div class="grid3" style="margin-bottom:12px">
      <input v-model="form.name" :placeholder="$t('bots.name')">
      <input v-model="form.type" list="botTypes" :placeholder="$t('bots.type')">
      <datalist id="botTypes">
        <option value="dingtalk"></option>
        <option value="wecom"></option>
        <option value="feishu"></option>
        <option value="telegram"></option>
        <option value="slack"></option>
        <option value="discord"></option>
        <option value="teams"></option>
        <option value="custom"></option>
      </datalist>
      <input v-model="form.url" :placeholder="$t('bots.url')">
    </div>
    <div style="margin-bottom:12px">
      <label style="display:block;margin-bottom:4px;font-size:13px;color:#888">{{ $t('bots.template') }}</label>
      <textarea v-model="form.template" :placeholder="$t('bots.templatePlaceholder')" rows="6" style="width:100%;font-family:monospace;font-size:12px;box-sizing:border-box"></textarea>
      <div style="font-size:11px;color:#aaa;margin-top:4px">{{ $t('bots.templateHint') }}</div>
    </div>
    <button class="btn btn-green" @click="add">＋ {{ $t('common.add') }}</button>
    <table style="margin-top:12px">
      <thead><tr><th>{{ $t('bots.name') }}</th><th>{{ $t('bots.type') }}</th><th>URL</th><th>{{ $t('common.action') }}</th></tr></thead>
      <tbody>
        <tr v-if="loading"><td colspan="4" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
        <tr v-for="b in bots" :key="b.id">
          <td>{{ b.name }}</td>
          <td>{{ b.type }}</td>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">{{ b.webhook_url }}</td>
          <td><button class="btn btn-red btn-sm" @click="del(b.id)">{{ $t('common.delete') }}</button></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'

const auth = useAuth()
const { t } = useI18n()
const { toast } = useToast()

const bots = ref([])
const loading = ref(true)
const form = reactive({ name: '', type: 'dingtalk', url: '', template: '' })

async function loadData() {
  loading.value = true
  try {
    const r = await fetch('/api/bots', { headers: auth.A() })
    if (auth.handle401(r)) return
    bots.value = await r.json()
  } catch (e) {} finally { loading.value = false }
}

async function add() {
  const n = form.name.trim()
  const u = form.url.trim()
  if (!n || !u) return toast(t('bots.fillNameAndUrl'), false)
  const r = await fetch('/api/bots', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify({ name: n, type: form.type, webhook_url: u, template: form.template })
  })
  if (auth.handle401(r)) return
  const d = await r.json()
  toast(d.success ? t('bots.added') : t('common.failed'), d.success)
  if (d.success) { form.name = ''; form.url = ''; form.template = ''; loadData() }
}

async function del(id) {
  if (!confirm(t('bots.confirmDelete'))) return
  const r = await fetch(`/api/bots/${id}`, { method: 'DELETE', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('bots.deleted'), true)
  loadData()
}

onMounted(loadData)
</script>
