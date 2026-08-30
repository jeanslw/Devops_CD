<template>
  <div class="page">
    <div class="app-card">
      <div class="card-header">
        <h3>{{ $t('approvals.title') }}</h3>
        <div class="tabs">
          <button class="tab-btn" :class="{ active: tab === 'list' }" @click="tab = 'list'">
            {{ $t('approvals.tabList') }}
          </button>
          <button
            v-if="canManageRules"
            class="tab-btn"
            :class="{ active: tab === 'rules' }"
            @click="tab = 'rules'; loadRules()"
          >
            {{ $t('approvals.tabRules') }}
          </button>
        </div>
      </div>

      <!-- ── 审批单列表 ── -->
      <div v-if="tab === 'list'">
        <div class="filter-row">
          <button
            v-for="s in statusOptions"
            :key="s.value"
            class="tab-btn"
            :class="{ active: statusFilter === s.value }"
            @click="setFilter(s.value)"
          >{{ s.label }}</button>
          <button class="tab-btn" @click="load">{{ $t('common.search') }}</button>
        </div>

        <table class="table" v-if="items.length">
          <thead>
            <tr>
              <th>{{ $t('approvals.id') }}</th>
              <th>{{ $t('approvals.project') }}</th>
              <th>{{ $t('approvals.tag') }}</th>
              <th>{{ $t('approvals.deployType') }}</th>
              <th>{{ $t('approvals.envs') }}</th>
              <th>{{ $t('approvals.requester') }}</th>
              <th>{{ $t('approvals.status') }}</th>
              <th>{{ $t('approvals.approver') }}</th>
              <th>{{ $t('approvals.createdAt') }}</th>
              <th>{{ $t('common.action') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in items" :key="a.id">
              <td>#{{ a.id }}</td>
              <td>{{ a.project }}</td>
              <td>{{ a.tag || '—' }}</td>
              <td>{{ a.deploy_type || '—' }}</td>
              <td>{{ a.envs || '—' }}</td>
              <td>{{ a.requester || '—' }}</td>
              <td><span class="badge" :class="statusClass(a.status)">{{ statusLabel(a.status) }}</span></td>
              <td>{{ a.approver || '—' }}</td>
              <td>{{ a.created_at }}</td>
              <td class="actions">
                <button
                  v-if="a.status === 'pending' && a.can_approve"
                  class="btn btn-sm btn-green"
                  @click="doApprove(a)"
                >{{ $t('approvals.approve') }}</button>
                <button
                  v-if="a.status === 'pending' && a.can_approve"
                  class="btn btn-sm btn-red"
                  @click="openReject(a)"
                >{{ $t('approvals.reject') }}</button>
                <button
                  v-if="a.status === 'pending' && a.can_cancel"
                  class="btn btn-sm"
                  @click="doCancel(a)"
                >{{ $t('approvals.cancelApproval') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">{{ $t('approvals.noApprovals') }}</div>

        <div v-if="totalPages > 1" class="pager">
          <button class="btn btn-sm" :disabled="page <= 1" @click="goPage(-1)">{{ $t('common.prev') }}</button>
          <span>{{ page }} / {{ totalPages }}</span>
          <button class="btn btn-sm" :disabled="page >= totalPages" @click="goPage(1)">{{ $t('common.next') }}</button>
        </div>
      </div>

      <!-- ── 审批规则管理 ── -->
      <div v-else>
        <div class="rule-form">
          <h4>{{ editingId ? $t('common.edit') : $t('approvals.addRule') }}</h4>
          <div class="grid2">
            <div class="form-group">
              <label>{{ $t('approvals.ruleProject') }}</label>
              <input v-model="ruleForm.project" placeholder="*, php/devops-glue,static" />
            </div>
            <div class="form-group">
              <label>{{ $t('approvals.ruleEnabled') }}</label>
              <label class="checkbox"><input type="checkbox" v-model="ruleForm.enabled" /> {{ $t('common.on') }}</label>
            </div>
            <div class="form-group">
              <label>{{ $t('approvals.ruleRequireEnvs') }}</label>
              <input v-model="ruleForm.require_envs" placeholder="prod" />
            </div>
            <div class="form-group">
              <label>{{ $t('approvals.ruleApproverRole') }}</label>
              <select v-model="ruleForm.approver_role">
                <option v-for="r in roles" :key="r.name" :value="r.name">{{ roleLabel(r) }}</option>
              </select>
            </div>
            <div class="form-group">
              <label>{{ $t('approvals.ruleApprovers') }}</label>
              <div v-if="users.length" class="user-picker">
                <label v-for="u in users" :key="u.username" class="checkbox">
                  <input type="checkbox" :value="u.username" v-model="approversArr" />
                  {{ u.username }}<span class="user-role">{{ u.role }}</span>
                </label>
              </div>
              <div v-else class="empty">{{ $t('approvals.noUsers') }}</div>
            </div>
            <div class="form-group">
              <label>{{ $t('approvals.ruleNotifyBot') }}</label>
              <select v-model="ruleForm.notify_bot_id">
                <option :value="0">{{ $t('bots.noNotify') }}</option>
                <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
              </select>
            </div>
            <div class="form-group">
              <label>{{ $t('approvals.ruleRequireRollback') }}</label>
              <label class="checkbox"><input type="checkbox" v-model="ruleForm.require_rollback_approval" /> {{ $t('common.on') }}</label>
            </div>
          </div>
          <div class="rule-actions">
            <button class="btn btn-green" @click="saveRule">{{ $t('approvals.saveRule') }}</button>
            <button v-if="editingId" class="btn" @click="resetRuleForm">{{ $t('common.cancel') }}</button>
          </div>
        </div>

        <table class="table" v-if="rules.length">
          <thead>
            <tr>
              <th>{{ $t('approvals.project') }}</th>
              <th>{{ $t('approvals.ruleEnabled') }}</th>
              <th>{{ $t('approvals.ruleRequireEnvs') }}</th>
              <th>{{ $t('approvals.ruleApproverRole') }}</th>
              <th>{{ $t('approvals.ruleApprovers') }}</th>
              <th>{{ $t('common.action') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rules" :key="r.id">
              <td>{{ r.project }}</td>
              <td>{{ r.enabled ? '✅' : '—' }}</td>
              <td>{{ r.require_envs || '—' }}</td>
              <td><span class="badge badge-role">{{ roleName(r.approver_role) }}</span></td>
              <td>
                <template v-if="r.approvers">
                  <span v-for="u in splitCsv(r.approvers)" :key="u" class="badge badge-user">{{ u }}</span>
                </template>
                <template v-else>—</template>
              </td>
              <td class="actions">
                <button class="btn btn-sm" @click="editRule(r)">{{ $t('common.edit') }}</button>
                <button class="btn btn-sm btn-red" @click="deleteRule(r)">{{ $t('common.delete') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">{{ $t('approvals.noRules') }}</div>
      </div>
    </div>

    <!-- 驳回弹窗 -->
    <div class="modal-overlay" v-if="rejectTarget" @click.self="rejectTarget = null">
      <div class="modal-box">
        <h4>{{ $t('approvals.reject') }} #{{ rejectTarget.id }} — {{ rejectTarget.project }}</h4>
        <div class="form-group">
          <label>{{ $t('approvals.note') }}</label>
          <input v-model="rejectNote" :placeholder="$t('approvals.rejectPlaceholder')" />
        </div>
        <div class="modal-actions">
          <button class="btn" @click="rejectTarget = null">{{ $t('common.cancel') }}</button>
          <button class="btn btn-red" @click="doReject">{{ $t('approvals.reject') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'

const auth = useAuth()
const { t } = useI18n()
const { toast } = useToast()
const { showError } = useError()

const tab = ref('list')
const statusFilter = ref('')
const items = ref([])
const page = ref(1)
const totalPages = ref(1)
const rejectTarget = ref(null)
const rejectNote = ref('')

const rules = ref([])
const bots = ref([])
const roles = ref([])
const users = ref([])
const approversArr = ref([])
const editingId = ref(0)
const ruleForm = ref({})

const canManageRules = computed(() => auth.canManageApprovalRules())

const STATUS_KEYS = ['pending', 'approved', 'deploying', 'deployed', 'failed', 'rejected', 'cancelled']
const statusOptions = computed(() => [
  { value: '', label: t('approvals.all') },
  ...STATUS_KEYS.map(k => ({ value: k, label: t(`approvals.status_${k}`) }))
])

function statusLabel(s) {
  return t(`approvals.status_${s}`) || s
}

function statusClass(s) {
  const map = {
    pending: 'badge-pend',
    approved: 'badge-running',
    deploying: 'badge-running',
    deployed: 'badge-super',
    failed: 'badge-err',
    rejected: 'badge-err',
    cancelled: 'badge-pending'
  }
  return map[s] || ''
}

async function load() {
  try {
    const qs = new URLSearchParams()
    if (statusFilter.value) qs.set('status', statusFilter.value)
    qs.set('page', page.value)
    qs.set('page_size', 20)
    const r = await fetch(`/api/approvals?${qs}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const d = await r.json()
    items.value = d.items || []
    page.value = d.page || 1
    totalPages.value = d.total_pages || 1
  } catch (e) {}
}

function setFilter(s) {
  statusFilter.value = s
  page.value = 1
  load()
}

function goPage(delta) {
  page.value = Math.max(1, Math.min(totalPages.value, page.value + delta))
  load()
}

async function doApprove(a) {
  const r = await fetch(`/api/approvals/${a.id}/approve`, { method: 'POST', headers: auth.A() })
  if (auth.handle401(r)) return
  const d = await r.json()
  if (d.success) toast(t('approvals.approveSuccess'), true)
  else await showError(d)
  load()
}

function openReject(a) {
  rejectTarget.value = a
  rejectNote.value = ''
}

async function doReject() {
  const a = rejectTarget.value
  if (!a) return
  const r = await fetch(`/api/approvals/${a.id}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify({ note: rejectNote.value })
  })
  if (auth.handle401(r)) return
  const d = await r.json()
  if (d.success) toast(t('approvals.rejectSuccess'), true)
  else await showError(d)
  rejectTarget.value = null
  load()
}

async function doCancel(a) {
  const r = await fetch(`/api/approvals/${a.id}/cancel`, { method: 'POST', headers: auth.A() })
  if (auth.handle401(r)) return
  const d = await r.json()
  if (d.success) toast(t('approvals.cancelSuccess'), true)
  else await showError(d)
  load()
}

// ── 规则 ──
async function loadBots() {
  try {
    const r = await fetch('/api/bots', { headers: auth.A() })
    if (r.ok) bots.value = await r.json()
  } catch (e) {}
}

async function loadRoles() {
  try {
    const r = await fetch('/api/roles', { headers: auth.A() })
    if (auth.handle401(r)) return
    if (r.ok) roles.value = (await r.json()).items || []
  } catch (e) {}
}

async function loadUsers() {
  try {
    const r = await fetch('/api/users', { headers: auth.A() })
    if (auth.handle401(r)) return
    if (r.ok) users.value = await r.json()
  } catch (e) {}
}

// 审批角色显示名：直接读 CI(Glue) 的 description，与 CI 侧保持一致（空则回退 name）
function roleLabel(r) {
  return r.description || r.name
}

function roleName(name) {
  const r = roles.value.find(x => x.name === name)
  return r ? roleLabel(r) : name
}

function splitCsv(s) {
  return (s || '').split(',').map(x => x.trim()).filter(Boolean)
}

async function loadRules() {
  try {
    const r = await fetch('/api/approval-rules', { headers: auth.A() })
    if (auth.handle401(r)) return
    const d = await r.json()
    rules.value = d.items || []
  } catch (e) {}
}

function resetRuleForm() {
  editingId.value = 0
  approversArr.value = []
  ruleForm.value = {
    project: '', enabled: false, require_envs: '', approver_role: 'cd_admin',
    approvers: '', notify_bot_id: 0, require_rollback_approval: true
  }
}

function editRule(r) {
  editingId.value = r.id
  approversArr.value = splitCsv(r.approvers)
  ruleForm.value = {
    project: r.project,
    enabled: !!r.enabled,
    require_envs: r.require_envs || '',
    approver_role: r.approver_role || 'cd_admin',
    approvers: r.approvers || '',
    notify_bot_id: r.notify_bot_id || 0,
    require_rollback_approval: !!r.require_rollback_approval
  }
}

async function saveRule() {
  const p = ruleForm.value.project
  if (!p) return toast(t('approvals.ruleProject'))
  const body = {
    enabled: ruleForm.value.enabled,
    require_envs: ruleForm.value.require_envs,
    approver_role: ruleForm.value.approver_role,
    approvers: approversArr.value.join(','),
    notify_bot_id: parseInt(ruleForm.value.notify_bot_id) || 0,
    require_rollback_approval: ruleForm.value.require_rollback_approval
  }
  const r = await fetch(`/api/approval-rules/${encodeURIComponent(p)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify(body)
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    toast(t('approvals.ruleSaved'), true)
    resetRuleForm()
    loadRules()
  } else {
    await showError(r)
  }
}

async function deleteRule(r) {
  const rp = await fetch(`/api/approval-rules/${encodeURIComponent(r.project)}`, { method: 'DELETE', headers: auth.A() })
  if (auth.handle401(rp)) return
  if (rp.ok) {
    toast(t('approvals.ruleDeleted'), true)
    loadRules()
  } else {
    await showError(rp)
  }
}

let _timer = null

onMounted(() => {
  resetRuleForm()
  load()
  loadBots()
  loadRoles()
  loadUsers()
  _timer = setInterval(() => { if (tab.value === 'list') load() }, 5000)
})

onUnmounted(() => {
  if (_timer) clearInterval(_timer)
})
</script>

<style scoped>
.app-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px 22px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.card-header h3 {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin: 0;
}
.tabs {
  display: flex;
  gap: 8px;
}
.tab-btn {
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  border-radius: var(--radius-sm);
  cursor: pointer;
  border: 1px solid var(--border);
  background: var(--bg-input);
  color: var(--text-dim);
  transition: all var(--transition);
}
.tab-btn:hover { color: var(--text); border-color: var(--text-dim); }
.tab-btn.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 14px 0;
}
.filter-row .tab-btn { padding: 4px 10px; font-size: 11px; }
.empty {
  padding: 24px;
  text-align: center;
  color: var(--text-dim);
  font-size: 13px;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 14px;
}
.actions { display: flex; gap: 6px; }
.rule-form {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  margin-bottom: 16px;
  background: var(--bg-input);
}
.rule-actions { display: flex; gap: 8px; margin-top: 12px; }
.form-group { margin-bottom: 12px; }
.checkbox {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text);
  text-transform: none;
  font-weight: 500;
  cursor: pointer;
}
.checkbox input { margin: 0; }
.modal-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 16px;
}
.badge-role {
  background: var(--accent);
  color: #fff;
  border-radius: var(--radius-sm);
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 600;
}
.badge-user {
  background: var(--bg-input);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: var(--radius-sm);
  padding: 1px 8px;
  font-size: 11px;
  margin-right: 4px;
}
.user-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  max-height: 160px;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
}
.user-role {
  color: var(--text-dim);
  font-size: 10px;
  margin-left: 4px;
}
</style>
