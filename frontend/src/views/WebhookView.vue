<template>
  <div class="page">
    <!-- 事件列表视图 -->
    <div v-if="selectedWebhook" class="app-card">
      <div class="card-header">
        <h3>{{ $t('webhooks.events') }} — {{ selectedWebhook.name }}</h3>
        <button class="btn" @click="selectedWebhook = null">{{ $t('webhooks.back') }}</button>
      </div>

      <table class="table" v-if="events.length">
        <thead>
          <tr>
            <th>{{ $t('webhooks.payload') }}</th>
            <th>{{ $t('webhooks.receivedAt') }}</th>
            <th>{{ $t('webhooks.forwarded') }}</th>
            <th v-if="auth.canNotificationManage()">{{ $t('common.action') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="eventsLoading"><td :colspan="4" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
          <tr v-for="e in events" :key="e.id">
            <td style="max-width:500px">
              <pre style="white-space:pre-wrap;word-break:break-all;font-size:12px;margin:0">{{ formatPayload(e.payload) }}</pre>
            </td>
            <td style="white-space:nowrap">{{ e.received_at }}</td>
            <td>
              <span v-if="e.forwarded" style="color:#52c41a">{{ $t('webhooks.forwarded') }}</span>
              <span v-else style="color:#999">{{ $t('webhooks.notForwarded') }}</span>
            </td>
            <td v-if="auth.canNotificationManage()">
              <select v-if="!e.forwarded" v-model="forwardBotId[e.id]" style="font-size:12px;margin-right:4px">
                <option value="">{{ $t('webhooks.selectBot') }}</option>
                <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
              </select>
              <button v-if="!e.forwarded" class="btn btn-xs btn-primary" :disabled="!forwardBotId[e.id]" @click="forwardEvent(e.id)">{{ $t('webhooks.forward') }}</button>
              <button class="btn btn-xs btn-danger" style="margin-left:4px" @click="deleteEvent(e.id)">{{ $t('common.delete') }}</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="!eventsLoading" class="empty">{{ $t('webhooks.noEvents') }}</div>

      <!-- 分页 -->
      <div v-if="eventsTotal > eventsPageSize" class="pagination" style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <button class="btn btn-xs" :disabled="eventsPage <= 1" @click="loadEvents(selectedWebhook.id, eventsPage - 1)">{{ $t('common.prev') }}</button>
        <span style="font-size:13px;color:#888">{{ eventsPage }} / {{ eventsTotalPages }}</span>
        <button class="btn btn-xs" :disabled="eventsPage >= eventsTotalPages" @click="loadEvents(selectedWebhook.id, eventsPage + 1)">{{ $t('common.next') }}</button>
      </div>
    </div>

    <!-- Webhook 列表视图 -->
    <div v-else class="app-card">
      <div class="card-header">
        <h3>{{ $t('webhooks.title') }}</h3>
        <button v-if="auth.canNotificationManage()" class="btn btn-primary" @click="showCreate = !showCreate">{{ $t('webhooks.createWebhook') }}</button>
      </div>

      <!-- 创建表单 -->
      <div v-if="showCreate" style="margin-bottom:16px;padding:12px;background:#f9f9f9;border-radius:6px">
        <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
          <div>
            <label style="display:block;font-size:12px;color:#888;margin-bottom:4px">{{ $t('webhooks.name') }}</label>
            <input v-model="createForm.name" :placeholder="$t('webhooks.name')" autofocus>
          </div>
          <div>
            <label style="display:block;font-size:12px;color:#888;margin-bottom:4px">{{ $t('webhooks.autoForward') }}</label>
            <select v-model="createForm.bot_id">
              <option :value="0">{{ $t('webhooks.noAutoForward') }}</option>
              <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
            </select>
          </div>
          <button class="btn btn-primary" @click="doCreate">{{ $t('common.add') }}</button>
          <button class="btn" @click="showCreate = false">{{ $t('common.cancel') }}</button>
        </div>
      </div>

      <table class="table" v-if="webhooks.length">
        <thead>
          <tr>
            <th>{{ $t('webhooks.name') }}</th>
            <th>{{ $t('webhooks.url') }}</th>
            <th>{{ $t('webhooks.autoForward') }}</th>
            <th>{{ $t('common.status') }}</th>
            <th v-if="auth.canNotificationManage()">{{ $t('common.action') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td :colspan="auth.canNotificationManage() ? 5 : 4" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
          <tr v-for="w in webhooks" :key="w.id">
            <td>{{ w.name }}</td>
            <td style="max-width:350px">
              <code style="font-size:11px;word-break:break-all">{{ getWebhookUrl(w.token) }}</code>
              <button class="btn btn-xs" style="margin-left:4px" @click="copyUrl(w.token)">{{ $t('webhooks.copyUrl') }}</button>
            </td>
            <td>{{ getBotName(w.bot_id) }}</td>
            <td>
              <span :style="w.enabled ? 'color:#52c41a' : 'color:#999'">{{ w.enabled ? $t('webhooks.enabled') : $t('webhooks.disabled') }}</span>
            </td>
            <td v-if="auth.canNotificationManage()">
              <button class="btn btn-xs" @click="viewEvents(w)">{{ $t('webhooks.viewEvents') }}</button>
              <button class="btn btn-xs" style="margin-left:4px" @click="toggleWebhook(w)">{{ w.enabled ? $t('webhooks.disabled') : $t('webhooks.enabled') }}</button>
              <button class="btn btn-xs btn-danger" style="margin-left:4px" @click="del(w)">{{ $t('common.delete') }}</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="!loading" class="empty">{{ $t('common.noData') }}</div>

      <!-- 使用说明 -->
      <div v-if="webhooks.length" style="margin-top:16px;padding:12px;background:#f0f5ff;border-radius:6px;font-size:12px;color:#555">
        <div style="font-weight:600;margin-bottom:4px">{{ $t('webhooks.testHint') }}</div>
        <pre style="margin:0;font-size:11px">curl -X POST "https://your-cd-host/api/webhooks/receive/TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project":"my-app","tag":"v1.0.0","image":"registry/my-app:v1.0.0","built_at":"2026-08-07 14:30:00"}'</pre>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, inject, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'

const auth = inject('auth')
const { t } = useI18n()
const { toast } = useToast()
const { showError } = useError()

const webhooks = ref([])
const bots = ref([])
const loading = ref(true)
const showCreate = ref(false)
const createForm = reactive({ name: '', bot_id: 0 })

// 事件列表
const selectedWebhook = ref(null)
const events = ref([])
const eventsLoading = ref(false)
const eventsPage = ref(1)
const eventsPageSize = ref(20)
const eventsTotal = ref(0)
const eventsTotalPages = ref(1)
const forwardBotId = reactive({})

async function loadData() {
  loading.value = true
  try {
    const [whRes, botRes] = await Promise.all([
      fetch('/api/webhooks', { headers: auth.A() }),
      fetch('/api/bots', { headers: auth.A() })
    ])
    if (auth.handle401(whRes) || auth.handle401(botRes)) return
    webhooks.value = await whRes.json()
    bots.value = await botRes.json()
  } catch (e) {} finally { loading.value = false }
}

async function doCreate() {
  const n = createForm.name.trim()
  if (!n) return toast(t('webhooks.fillName'), false)
  const r = await fetch('/api/webhooks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify({ name: n, bot_id: createForm.bot_id })
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    const body = await r.json()
    toast(t('webhooks.created'))
    showCreate.value = false
    createForm.name = ''
    createForm.bot_id = 0
    loadData()
  } else {
    await showError(r)
  }
}

async function del(w) {
  if (!confirm(t('webhooks.confirmDelete'))) return
  const r = await fetch(`/api/webhooks/${w.id}`, { method: 'DELETE', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('webhooks.deleted'))
  loadData()
}

async function toggleWebhook(w) {
  const r = await fetch(`/api/webhooks/${w.id}/toggle`, { method: 'POST', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('webhooks.toggled'))
  loadData()
}

async function viewEvents(w) {
  selectedWebhook.value = w
  events.value = []
  eventsPage.value = 1
  await loadEvents(w.id, 1)
}

async function loadEvents(wid, page) {
  eventsLoading.value = true
  eventsPage.value = page
  try {
    const r = await fetch(`/api/webhooks/${wid}/events?page=${page}&page_size=${eventsPageSize.value}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const body = await r.json()
    events.value = body.items || []
    eventsTotal.value = body.total || 0
    eventsTotalPages.value = body.total_pages || 1
  } catch (e) {} finally { eventsLoading.value = false }
}

async function forwardEvent(eid) {
  const bid = parseInt(forwardBotId[eid])
  if (!bid) return
  const r = await fetch(`/api/webhooks/events/${eid}/forward`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify({ bot_id: bid })
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    toast(t('webhooks.forwardedMsg'))
    forwardBotId[eid] = ''
    loadEvents(selectedWebhook.value.id, eventsPage.value)
  } else {
    await showError(r)
  }
}

async function deleteEvent(eid) {
  if (!confirm(t('webhooks.confirmDeleteEvent'))) return
  const r = await fetch(`/api/webhooks/events/${eid}`, { method: 'DELETE', headers: auth.A() })
  if (auth.handle401(r)) return
  toast(t('webhooks.deleted'))
  loadEvents(selectedWebhook.value.id, eventsPage.value)
}

function getWebhookUrl(token) {
  return `${window.location.origin}/api/webhooks/receive/${token}`
}

function getBotName(botId) {
  if (!botId) return t('webhooks.noAutoForward')
  const bot = bots.value.find(b => b.id === botId)
  return bot ? bot.name : `Bot #${botId}`
}

function copyUrl(token) {
  const url = getWebhookUrl(token)
  navigator.clipboard.writeText(url).then(() => {
    toast(t('webhooks.copied'))
  })
}

function formatPayload(payload) {
  try {
    return JSON.stringify(JSON.parse(payload), null, 2)
  } catch {
    return payload
  }
}

onMounted(loadData)
</script>
