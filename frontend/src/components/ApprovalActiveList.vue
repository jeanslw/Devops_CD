<template>
  <!-- 部署页：本项目当前用户本人发起的全部活跃审批单（待审批/已批准/执行中） -->
  <div v-if="items.length" class="card active-approvals">
    <div class="aa-head">
      <span class="aa-title">📋 {{ $t('approvals.activeListTitle', { n: items.length }) }}</span>
    </div>
    <div
      v-for="a in items"
      :key="a.id"
      class="aa-row"
      :class="'row-' + a.status"
      @click="emit('open', a.id)"
    >
      <span class="aa-icon">{{ icon(a.status) }}</span>
      <span class="aa-id">#{{ a.id }}</span>
      <span v-if="a.tag" class="aa-tag">{{ a.tag }}</span>
      <span class="badge" :class="statusClass(a.status)">{{ $t(`approvals.status_${a.status}`) }}</span>
      <span class="aa-time">{{ fmtTime(a.created_at) }}</span>
      <button
        v-if="a.status === 'approved' && a.can_execute"
        class="btn btn-sm btn-green aa-action"
        @click.stop="emit('open', a.id)"
      >{{ $t('approvals.flowExecute') }}</button>
      <span v-else-if="a.status === 'deploying'" class="aa-action aa-busy">{{ $t('approvals.flowPolling') }}</span>
      <span v-else class="aa-action aa-link">{{ $t('approvals.flowView') }} →</span>
    </div>
  </div>
</template>

<script setup>
defineProps({
  items: { type: Array, default: () => [] },
})
const emit = defineEmits(['open'])

function icon(s) {
  return ({ pending: '⏳', approved: '✅', deploying: '🚀' })[s] || '📋'
}

function statusClass(s) {
  return ({
    pending: 'badge-pend',
    approved: 'badge-running',
    deploying: 'badge-running',
  })[s] || 'badge-pending'
}

// 列表只占一行高度：ISO 时间截到分钟
function fmtTime(t) {
  if (!t) return ''
  return String(t).replace('T', ' ').slice(0, 16)
}
</script>

<style scoped>
.active-approvals {
  margin: 10px 0;
  padding: 8px 12px;
}
.aa-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 0 6px;
}
.aa-title {
  font-size: 13px;
  font-weight: 600;
}
.aa-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13px;
  border-left: 3px solid transparent;
}
.aa-row:hover { background: var(--bg-input); }
.aa-row.row-approved { border-left-color: var(--accent); }
.aa-row.row-deploying { border-left-color: var(--blue, #60a5fa); }
.aa-row.row-pending { border-left-color: var(--amber, #fbbf24); }

.aa-icon { flex: none; }
.aa-id {
  flex: none;
  font-weight: 700;
  color: var(--accent);
  min-width: 34px;
}
.aa-tag {
  flex: none;
  color: var(--text-dim);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.aa-time {
  margin-left: auto;
  flex: none;
  color: var(--text-dim);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.aa-action { flex: none; }
.aa-link { color: var(--accent); font-size: 12px; }
.aa-busy { color: var(--blue, #60a5fa); font-size: 12px; }
</style>
