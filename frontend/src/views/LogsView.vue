<template>
  <div class="card">
    <h3>{{ $t('logs.title') }}</h3>
    <table>
      <thead><tr><th>{{ $t('logs.id') }}</th><th class="col-time">{{ $t('logs.time') }}</th><th class="col-project">{{ $t('logs.project') }}</th><th class="col-tag">{{ $t('logs.tag') }}</th><th class="col-method">{{ $t('logs.method') }}</th><th class="col-status">{{ $t('logs.status') }}</th><th class="col-duration">{{ $t('logs.duration') }}</th><th class="col-operator">{{ $t('logs.operator') }}</th><th class="col-detail">{{ $t('logs.detail') }}</th><th class="col-note">{{ $t('logs.note') }}</th></tr></thead>
      <tbody>
        <tr v-if="loading"><td colspan="10" style="text-align:center;color:#888">{{ $t('common.loading') }}</td></tr>
        <template v-else-if="logs.length === 0">
          <tr><td colspan="10" style="text-align:center;color:#888">{{ $t('logs.noRecords') }}</td></tr>
        </template>
        <template v-else v-for="(l, idx) in logs" :key="l.id">
          <tr style="cursor:pointer" @click="toggleDetail(idx)">
            <td>
              <div class="log-id-cell">
                <span style="color:#81c784;font-weight:bold">#{{ l.deploy_id }}</span>
                <div
                  v-if="l.approval_id"
                  class="approval-badge"
                  :class="approvalBadgeClass(l)"
                  :title="approvalTip(l)"
                >
                  {{ approvalBadgeText(l) }}
                </div>
              </div>
            </td>
            <td class="col-time">{{ l.created_at }}</td>
            <td class="col-project">{{ l.project }}</td>
            <td class="col-tag">{{ l.tag }}</td>
            <td class="col-method">{{ l.deploy_type }}</td>
            <td class="col-status">
              <span class="badge" :class="badgeClass(l.status)">{{ statusText(l.status) }}</span>
              <button v-if="l.status === 'running'" class="btn btn-sm btn-orange" style="margin-left:6px" @click.stop="cancelRun(l)">{{ $t('deploy.cancel') }}</button>
            </td>
            <td class="col-duration">{{ fmtDuration(l.duration_ms) }}</td>
            <td class="col-operator">{{ l.triggered_by || '-' }}</td>
            <td><div class="output-preview">{{ renderOutput(l.output || '') }}</div></td>
            <td><div class="note-preview" :title="renderNote(l.deploy_note || '')">{{ l.deploy_note ? renderNote(l.deploy_note) : '-' }}</div></td>
          </tr>
          <tr v-if="expandedIdx === idx" class="log-detail">
            <td colspan="10">
              <div class="target-block">
                <pre class="output-code">{{ escapeHtml(renderOutput(l.output || $t('logs.noOutput'))) }}</pre>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    <div v-if="totalPages > 1" class="log-pager">
      <span class="log-pager-info">{{ $t('logs.totalInfo', { total, pages: totalPages }) }}</span>
      <button v-if="page > 1" class="btn btn-sm log-pager-btn" @click="loadData(page - 1)">{{ $t('common.prev') }}</button>
      <button v-for="i in pageRange" :key="i" class="btn btn-sm log-pager-btn" :class="{ active: i === page }" @click="loadData(i)">{{ i }}</button>
      <button v-if="page < totalPages" class="btn btn-sm log-pager-btn" @click="loadData(page + 1)">{{ $t('common.next') }}</button>
      <select class="log-pager-size" v-model="pageSize" @change="loadData(1)">
        <option :value="10">{{ $t('logs.perPage', { n: 10 }) }}</option>
        <option :value="15" selected>{{ $t('logs.perPage', { n: 15 }) }}</option>
        <option :value="30">{{ $t('logs.perPage', { n: 30 }) }}</option>
        <option :value="50">{{ $t('logs.perPage', { n: 50 }) }}</option>
      </select>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { confirm } from '@/composables/useConfirm'

const auth = useAuth()
const { t } = useI18n()
const { toast } = useToast()

const logs = ref([])
const loading = ref(true)
const page = ref(1)
const pageSize = ref(15)
const total = ref(0)
const totalPages = ref(1)
const expandedIdx = ref(-1)

const pageRange = computed(() => {
  const start = Math.max(1, page.value - 2)
  const end = Math.min(totalPages.value, page.value + 2)
  const arr = []
  for (let i = start; i <= end; i++) arr.push(i)
  return arr
})

function toggleDetail(idx) {
  expandedIdx.value = expandedIdx.value === idx ? -1 : idx
}

function escapeHtml(s) {
  if (!s) return ''
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// 后端日志/说明里的 STATUS:{json} 结构化消息按当前界面语言翻译，其余文本原样保留
function translateStatusToken(token) {
  if (!token.startsWith('STATUS:')) return token
  try {
    const msg = JSON.parse(token.slice(7))
    return t(msg.key, msg)
  } catch {
    return token.slice(7)
  }
}

// output 为多行日志，逐行翻译
function renderOutput(s) {
  return String(s || '').split('\n').map(translateStatusToken).join('\n')
}

// 说明为单行混合串（STATUS 摘要 + 普通文本）。STATUS 的 JSON 内部可能含空格，
// 故用正则整体匹配扁平对象后翻译，其余文本原样保留
function renderNote(s) {
  return String(s || '').replace(/STATUS:(\{[^{}]*\})/g, (whole, json) => {
    try {
      const msg = JSON.parse(json)
      return t(msg.key, msg)
    } catch {
      return whole.slice(7)
    }
  })
}

// 审批徽章按审批单状态渲染（list_logs 挂载的 approval 带 status）；
// 无 status（旧接口/兼容场景）时回退 approver 判断。
// failed 也归入"已批准"：部署失败的单据审批已通过（批准事实由 approver/approved_at 承载），
// 部署成败由状态列展示，徽章只表达审批是否发生。
function approvalState(l) {
  const a = l.approval || {}
  const st = a.status || ''
  if (st === 'cancelled' || st === 'rejected') return st
  if (st === 'approved' || st === 'deploying' || st === 'deployed' || st === 'failed') return 'approved'
  if (!st && a.approver) return 'approved'
  return 'pending'
}

function approvalBadgeText(l) {
  const st = approvalState(l)
  if (st === 'cancelled') return t('logs.cancelledBadge', { id: l.approval_id })
  if (st === 'rejected') return t('logs.rejectedBadge', { id: l.approval_id })
  if (st === 'approved') return t('logs.approvedBadge', { id: l.approval_id })
  return t('logs.pendingBadge', { id: l.approval_id })
}

function approvalBadgeClass(l) {
  const st = approvalState(l)
  if (st === 'cancelled' || st === 'rejected') return 'approval-badge--cancelled'
  if (st === 'approved') return ''
  return 'approval-badge--pending'
}

// 审批徽章 tooltip：已批准显示审批人+批准时间；已撤销/已驳回/申请中给出对应提示
function approvalTip(l) {
  const st = approvalState(l)
  if (st === 'cancelled') return t('logs.approvalTipCancelled', { id: l.approval_id })
  if (st === 'rejected') return t('logs.approvalTipRejected', { id: l.approval_id })
  const a = l.approval || {}
  if (st === 'pending') {
    return t('logs.approvalTipPending', { id: l.approval_id })
  }
  return t('logs.approvalTip', {
    id: l.approval_id,
    approver: a.approver || '-',
    time: a.approved_at || '',
  })
}

// 部署状态翻译成界面语言；未知状态原样展示
function statusText(status) {
  const key = ({
    ok: 'logs.statusOk',
    failed: 'logs.statusFailed',
    running: 'logs.statusRunning',
    pending: 'logs.statusPending',
    rejected: 'logs.statusRejected',
    cancelled: 'logs.statusCancelled',
    terminated: 'logs.statusTerminated',
    interrupted: 'logs.statusInterrupted',
    partial: 'logs.statusPartial',
  })[status]
  return key ? t(key) : status
}

function badgeClass(status) {
  switch (status) {
    case 'ok': return 'badge-ok'
    case 'failed':
    case 'rejected': return 'badge-err'
    case 'running': return 'badge-running'
    case 'pending': return 'badge-pend'
    case 'terminated':
    case 'interrupted':
    case 'cancelled': return 'badge-gray'
    case 'partial': return 'badge-blue'
    default: return 'badge-pend'
  }
}

function fmtDuration(ms) {
  if (!ms) return '-'
  if (ms < 1000) return ms + 'ms'
  const s = Math.floor(ms / 1000)
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  const r = s % 60
  return m + 'm ' + r + 's'
}

async function cancelRun(l) {
  if (!await confirm({ text: t('deploy.confirmCancel'), danger: true })) return
  try {
    const r = await fetch('/api/deploy/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...auth.A() },
      body: JSON.stringify({ deploy_id: l.deploy_id })
    })
    const d = await r.json()
    toast(d.success ? t('deploy.cancelled') : t('deploy.cancelFailed'), d.success)
    if (d.success) loadData(page.value)
  } catch (e) {
    toast(t('deploy.cancelFailed'), false)
  }
}

async function loadData(p = 1) {
  loading.value = true
  expandedIdx.value = -1
  try {
    const r = await fetch(`/api/deploy-logs?page=${p}&page_size=${pageSize.value}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) {
      console.error('API 返回非 JSON:', await r.text().catch(() => ''))
      return
    }
    const d = await r.json()
    logs.value = Array.isArray(d.items) ? d.items : []
    page.value = d.page || 1
    total.value = d.total || 0
    totalPages.value = d.total_pages || 1
  } catch (e) {
    console.error('加载部署记录失败:', e)
  } finally {
    loading.value = false
  }
}

onMounted(() => loadData())
</script>

<style scoped>
/* 固定内容列：不换行，保证时间/项目/TAG 等一行完整显示 */
.col-time,
.col-project,
.col-tag,
.col-method,
.col-status,
.col-duration,
.col-operator {
  white-space: nowrap;
}

/* ID 列：编号 + 审批徽章强制同一行不换行 */
.log-id-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

/* 审批徽章：flex 子项，inline-block 保证不被挤压换行 */
.approval-badge {
  display: inline-block;
  flex: none;
  padding: 0 7px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.7;
  border-radius: 9px;
  color: var(--accent);
  background: rgba(59,130,246,0.1);
  border: 1px solid rgba(59,130,246,0.3);
  cursor: help;
  white-space: nowrap;
}

/* 已终止/中断/撤销/驳回 等中性/异常灰态（全局未定义 badge-gray） */
.badge-gray { background: rgba(107,112,132,0.12); color: var(--text-dim); }

/* 待审批徽章用琥珀色与已批准蓝色区分 */
.approval-badge--pending {
  color: var(--amber);
  background: rgba(251,191,36,0.1);
  border-color: rgba(251,191,36,0.35);
}

/* 已撤销/已驳回徽章用灰态与待审批琥珀色、已批准蓝色区分 */
.approval-badge--cancelled {
  color: var(--text-dim);
  background: rgba(107,112,132,0.12);
  border-color: rgba(107,112,132,0.3);
}

/* 说明列移到最后并收窄：长回滚说明两行截断，展开行可看完整 output，hover 可见全文 */
.note-preview {
  max-width: 200px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.3;
  display: -webkit-box;
  line-clamp: 2;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.output-preview {
  max-width: 280px;
  font-size: 11px;
  white-space: pre-wrap;
  line-height: 1.3;
  display: -webkit-box;
  line-clamp: 3;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.output-code {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  max-height: 300px;
  overflow-y: auto;
  background: #111;
  color: #00ff00;
  padding: 10px;
  border-radius: 4px;
  font-family: monospace;
}
</style>
