<template>
  <div v-if="approval" class="card approval-flow">
    <div class="flow-head">
      <h3>
        {{ $t('approvals.flowTitle') }} #{{ approval.id }}
        <span v-if="approval.tag" class="flow-tag">{{ approval.tag }}</span>
        <span v-if="approval.deploy_type" class="flow-type">{{ approval.deploy_type }}</span>
      </h3>
      <button class="btn btn-sm" @click="emit('close')">{{ $t('approvals.flowClose') }}</button>
    </div>

    <!-- 阶段条：提交申请 → 等待审批 → 批准待执行 → 执行部署 → 完成 -->
    <div class="steps">
      <template v-for="(s, i) in STEPS" :key="s.key">
        <div class="step" :class="stepClass(s.key)">
          <div class="dot"><span class="dot-mark">{{ stepMark(s.key) }}</span></div>
          <div class="step-label">{{ stepLabel(s) }}</div>
        </div>
        <div v-if="i < STEPS.length - 1" class="bar" :class="{ done: barDone(i) }"></div>
      </template>
    </div>

    <!-- 当前状态提示 -->
    <div class="flow-hint" :class="'hint-' + status">{{ hintText }}</div>

    <!-- 驳回原因 -->
    <div v-if="status === 'rejected' && approval.approve_note" class="flow-note">
      <span class="flow-note-label">{{ $t('approvals.note') }}:</span> {{ approval.approve_note }}
    </div>

    <!-- 元信息 -->
    <div class="flow-meta">
      <span>{{ $t('approvals.flowRequester') }}: {{ approval.requester || '—' }}</span>
      <span v-if="scheduledAt" class="flow-sched-badge" :title="t('approvals.scheduleTip', { time: scheduledAt.slice(0, 16) })">
        ⏰ [{{ t('approvals.scheduledBadge') }}] {{ scheduledAt.slice(0, 16) }}
      </span>
      <span v-if="approval.approver">
        {{ $t('approvals.flowApprover') }}: {{ approval.approver }}<template v-if="approval.approved_at"> · {{ approval.approved_at }}</template>
      </span>
      <span v-if="approval.deploy_id">
        {{ $t('approvals.flowDeployId') }}<a :href="`/logs?deploy_id=${approval.deploy_id}`">#{{ approval.deploy_id }}</a>
      </span>
    </div>

    <!-- 操作区 -->
    <div class="flow-actions">
      <button
        v-if="status === 'approved' && approval.can_execute && !scheduledAt"
        class="btn btn-green"
        :disabled="running"
        @click="doExecute"
      >{{ running ? $t('approvals.flowExecutingBtn') : $t('approvals.flowExecute') }}</button>
      <button
        v-if="(status === 'pending' || status === 'approved') && approval.can_cancel"
        class="btn btn-red"
        :disabled="running"
        @click="doCancel"
      >{{ $t('approvals.flowCancel') }}</button>
      <span v-if="status === 'deploying'" class="flow-polling">{{ $t('approvals.flowPolling') }}</span>
    </div>

    <!-- 执行日志（SSE） -->
    <pre v-if="output" class="output flow-output" v-text="output"></pre>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'
import { useSseStream } from '@/composables/useSseStream'
import { confirm } from '@/composables/useConfirm'

const props = defineProps({
  approvalId: { type: [Number, String], default: 0 },
})
const emit = defineEmits(['close', 'finished'])

const { t } = useI18n()
const auth = useAuth()
const { toast } = useToast()
const { showError } = useError()
const { output, loading: running, stream } = useSseStream()

const approval = ref(null)
let timer = null

const TERMINAL = ['deployed', 'failed', 'rejected', 'cancelled']
const status = computed(() => approval.value?.status || '')
const isTerminal = computed(() => TERMINAL.includes(status.value))
const scheduledAt = computed(() => (approval.value?.scheduled_at || '').trim())

const STEPS = [
  { key: 'submit', label: 'approvals.flowStepSubmit' },
  { key: 'wait', label: 'approvals.flowStepWait' },
  { key: 'approve', label: 'approvals.flowStepApprove' },
  { key: 'run', label: 'approvals.flowStepRun' },
  { key: 'done', label: 'approvals.flowStepDone' },
]

function stepLabel(s) {
  if (s.key === 'approve' && scheduledAt.value) return t('approvals.flowStepScheduled')
  return t(s.label)
}

// ── 阶段条状态映射 ──
function stepClass(key) {
  const s = status.value
  const map = {
    submit: { default: 'done' },
    wait: {
      pending: 'active', approved: 'done', deploying: 'done', deployed: 'done', failed: 'done',
      rejected: 'error', cancelled: 'warn',
    },
    approve: {
      pending: 'skip', approved: 'active', deploying: 'done', deployed: 'done', failed: 'done',
      rejected: 'skip', cancelled: 'warn',
    },
    run: {
      pending: 'skip', approved: 'skip', deploying: 'active', deployed: 'done', failed: 'error',
      rejected: 'skip', cancelled: 'skip',
    },
    done: {
      deployed: 'done', failed: 'error', default: 'skip',
    },
  }
  const row = map[key] || {}
  return row[s] || row.default || 'skip'
}

function stepMark(key) {
  const c = stepClass(key)
  if (c === 'done') return '✓'
  if (c === 'active') return ''
  if (c === 'error') return '✕'
  if (c === 'warn') return '⊘'
  return ''
}

// 连接条：当前阶段之前的连线点亮
function barDone(i) {
  const order = ['submit', 'wait', 'approve', 'run', 'done']
  const nextKey = order[i + 1]
  return ['done', 'error', 'warn'].includes(stepClass(nextKey))
}

const hintText = computed(() => {
  const s = status.value
  if (s === 'approved' && approval.value) {
    if (scheduledAt.value) return t('approvals.scheduleTip', { time: scheduledAt.value.slice(0, 16) })
    if (!approval.value.can_execute) return t('approvals.flowHintApprovedOther')
  }
  return t(`approvals.flowHint_${s}`) || ''
})

// ── 拉取 + 轮询 ──
async function refresh() {
  const id = Number(props.approvalId)
  if (!id) return
  try {
    const r = await fetch(`/api/approvals/${id}`, { headers: auth.A() })
    if (auth.handle401(r)) return
    if (!r.ok) return
    approval.value = await r.json()
    if (isTerminal.value) stopTimer()
  } catch (e) {}
}

function startTimer() {
  stopTimer()
  timer = setInterval(refresh, 5000)
}
function stopTimer() {
  if (timer) { clearInterval(timer); timer = null }
}

watch(() => props.approvalId, async (v) => {
  if (!v) { approval.value = null; stopTimer(); return }
  await refresh()
  if (!isTerminal.value) startTimer()
})

// ── 申请人本人执行（SSE 实时日志） ──
async function doExecute() {
  const id = Number(props.approvalId)
  if (!id) return
  const ok = await stream(`/api/approvals/${id}/execute-stream`, {}, {
    onEnd: (success) => {
      toast(success ? t('deploy.deploySuccess') : t('deploy.deployFailed'), success)
      emit('finished', success)
      refresh()
    },
    onError: () => {
      toast(t('deploy.deployFailed'), false)
      emit('finished', false)
      refresh()
    },
  })
  // 流结束后再兜底拉一次，确保状态/部署记录号及时刷新
  if (ok !== undefined) refresh()
}

// ── 撤销申请 ──
async function doCancel() {
  const id = Number(props.approvalId)
  if (!id) return
  if (!await confirm({ text: t('approvals.flowCancelConfirm'), danger: true })) return
  try {
    const r = await fetch(`/api/approvals/${id}/cancel`, { method: 'POST', headers: auth.A() })
    if (auth.handle401(r)) return
    const d = await r.json()
    if (d.success) toast(t('approvals.cancelSuccess'), true)
    else await showError(d)
  } catch (e) {}
  refresh()
}

onMounted(async () => {
  await refresh()
  if (!isTerminal.value) startTimer()
})

onUnmounted(stopTimer)
</script>

<style scoped>
.approval-flow {
  margin: 10px 0;
  border-color: var(--accent);
}
.flow-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 14px;
}
.flow-head h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.flow-tag {
  font-size: 11px;
  font-weight: 500;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 1px 8px;
  color: var(--text-dim);
}
.flow-type {
  font-size: 11px;
  color: var(--text-dim);
}

/* 阶段条 */
.steps {
  display: flex;
  align-items: flex-start;
  margin: 6px 0 12px;
}
.step {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: 76px;
  flex: 0 0 76px;
}
.step .dot {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px solid var(--border);
  background: var(--bg-input);
  color: var(--text-dim);
  font-size: 12px;
  font-weight: 700;
}
.step-label {
  font-size: 11px;
  color: var(--text-dim);
  text-align: center;
  line-height: 1.3;
}
.bar {
  flex: 1 1 auto;
  height: 2px;
  background: var(--border);
  margin-top: 12px;
  min-width: 18px;
}
.bar.done { background: var(--accent); }

.step.done .dot { background: var(--accent); border-color: var(--accent); color: #fff; }
.step.done .step-label { color: var(--text); }
.step.active .dot {
  border-color: var(--accent);
  color: var(--accent);
  box-shadow: 0 0 0 4px rgba(59,130,246,0.15);
  animation: flow-pulse 1.4s ease-in-out infinite;
}
.step.active .step-label { color: var(--accent); font-weight: 600; }
.step.error .dot { background: #ef4444; border-color: #ef4444; color: #fff; }
.step.error .step-label { color: #ef4444; }
.step.warn .dot { background: var(--amber, #f59e0b); border-color: var(--amber, #f59e0b); color: #fff; }
.step.warn .step-label { color: var(--amber, #f59e0b); }
.step.skip .dot { opacity: 0.45; }

@keyframes flow-pulse {
  0%, 100% { box-shadow: 0 0 0 3px rgba(59,130,246,0.12); }
  50% { box-shadow: 0 0 0 7px rgba(59,130,246,0.22); }
}

.flow-hint {
  font-size: 13px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  border: 1px solid var(--border);
  margin-bottom: 10px;
}
.hint-pending { color: var(--amber, #f59e0b); }
.hint-approved { color: var(--accent); font-weight: 600; }
.hint-deploying { color: var(--accent); }
.hint-deployed { color: #16a34a; }
.hint-failed, .hint-rejected { color: #ef4444; }
.hint-cancelled { color: var(--text-dim); }

.flow-note {
  font-size: 12px;
  color: #ef4444;
  background: rgba(239,68,68,0.07);
  border: 1px solid rgba(239,68,68,0.25);
  border-radius: var(--radius-sm);
  padding: 6px 10px;
  margin-bottom: 10px;
}
.flow-note-label { font-weight: 600; }

.flow-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  font-size: 12px;
  color: var(--text-dim);
  margin-bottom: 10px;
}
.flow-meta a { color: var(--accent); text-decoration: none; margin-left: 2px; }
.flow-sched-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--amber, #f59e0b);
  color: #fff;
  font-weight: 600;
  border-radius: var(--radius-sm);
  padding: 1px 8px;
  font-size: 12px;
}

.flow-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.flow-polling {
  font-size: 12px;
  color: var(--accent);
}
.flow-output {
  margin-top: 10px;
  max-height: 420px;
}
</style>
