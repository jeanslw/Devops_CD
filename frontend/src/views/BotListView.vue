<template>
  <div class="page">
    <div class="app-card">
      <div class="card-header">
        <h3>{{ $t('bots.title') }}</h3>
        <button v-if="auth.canManage()" class="btn btn-primary" @click="$router.push('/bots/create')">{{ $t('bots.createBot') }}</button>
      </div>

      <table class="table" v-if="bots.length">
        <thead>
          <tr><th>{{ $t('bots.name') }}</th><th>{{ $t('bots.type') }}</th><th>URL</th><th v-if="auth.canManage()">{{ $t('common.action') }}</th></tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td :colspan="auth.canManage() ? 4 : 3" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
          <tr v-for="b in bots" :key="b.id">
            <td>{{ b.name }}</td>
            <td>{{ b.type }}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">{{ b.webhook_url }}</td>
            <td v-if="auth.canManage()"><button class="btn btn-xs btn-danger" @click="del(b.id)">{{ $t('common.delete') }}</button></td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="!loading" class="empty">{{ $t('common.noData') }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, inject, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useToast } from '@/composables/useToast'

const auth = inject('auth')
const { t } = useI18n()
const { toast } = useToast()

const bots = ref([])
const loading = ref(true)

async function loadData() {
  loading.value = true
  try {
    const r = await fetch('/api/bots', { headers: auth.A() })
    if (auth.handle401(r)) return
    bots.value = await r.json()
  } catch (e) {} finally { loading.value = false }
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
