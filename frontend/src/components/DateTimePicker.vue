<template>
  <div ref="root" class="dtp">
    <div class="dtp-input" @click="open = !open">
      <input
        :value="modelValue"
        readonly
        :placeholder="placeholder || t('datepicker.placeholder')"
      />
      <button
        v-if="modelValue"
        type="button"
        class="dtp-clear"
        :title="t('datepicker.clear')"
        @click.stop="clear"
      >×</button>
      <span class="dtp-icon">▾</span>
    </div>

    <div v-if="open" class="dtp-popup">
      <div class="dtp-head">
        <button type="button" class="dtp-nav" @click="prevMonth">‹</button>
        <select v-model="viewYear" class="dtp-sel">
          <option v-for="y in years" :key="y" :value="y">{{ y }}</option>
        </select>
        <select v-model="viewMonth" class="dtp-sel">
          <option v-for="(m, i) in months" :key="i" :value="i">{{ m }}</option>
        </select>
        <button type="button" class="dtp-nav" @click="nextMonth">›</button>
      </div>

      <div class="dtp-grid">
        <button
          v-for="c in cells"
          :key="c.date"
          type="button"
          class="dtp-day"
          :class="{ selected: c.selected, today: c.today, past: c.past }"
          :disabled="c.past"
          @click="selectDay(c.date)"
        >{{ c.day }}</button>
      </div>

      <div class="dtp-time">
        <select v-model="selHour" class="dtp-sel" @change="commit">
          <option v-for="h in hours" :key="h" :value="h">{{ h }}</option>
        </select>
        <span class="dtp-colon">:</span>
        <select v-model="selMinute" class="dtp-sel" @change="commit">
          <option v-for="m in minutes" :key="m" :value="m">{{ m }}</option>
        </select>
      </div>

      <div class="dtp-actions">
        <button type="button" class="btn btn-sm" @click="clear">{{ t('datepicker.clear') }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const { t } = useI18n()
const root = ref(null)
const open = ref(false)

const now = new Date()
const thisYear = now.getFullYear()
const years = Array.from({ length: 16 }, (_, i) => thisYear - 10 + i)
const hours = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'))
const minutes = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'))

const months = Array.from({ length: 12 }, (_, i) => String(i + 1))

const viewYear = ref(thisYear)
const viewMonth = ref(now.getMonth())
const selDate = ref(null) // 'YYYY-MM-DD' 或 null
const selHour = ref(String(now.getHours()).padStart(2, '0'))
const selMinute = ref(String(now.getMinutes()).padStart(2, '0'))

function pad(n) { return String(n).padStart(2, '0') }

function syncFromModel() {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(props.modelValue || '')
  if (m) {
    selDate.value = `${m[1]}-${m[2]}-${m[3]}`
    selHour.value = m[4]
    selMinute.value = m[5]
    viewYear.value = Number(m[1])
    viewMonth.value = Number(m[2]) - 1
  } else {
    selDate.value = null
    viewYear.value = now.getFullYear()
    viewMonth.value = now.getMonth()
    selHour.value = String(now.getHours()).padStart(2, '0')
    selMinute.value = String(now.getMinutes()).padStart(2, '0')
  }
}

watch(open, (v) => { if (v) syncFromModel() })

const cells = computed(() => {
  const daysInMonth = new Date(viewYear.value, viewMonth.value + 1, 0).getDate()
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
  const list = []
  for (let d = 1; d <= daysInMonth; d++) {
    const date = `${viewYear.value}-${pad(viewMonth.value + 1)}-${pad(d)}`
    list.push({
      day: d,
      date,
      selected: selDate.value === date,
      today: date === todayStr,
      past: date < todayStr,
    })
  }
  return list
})

function commit() {
  if (!selDate.value) return
  emit('update:modelValue', `${selDate.value} ${selHour.value}:${selMinute.value}`)
}

function selectDay(date) {
  selDate.value = date
  commit()
}

function clear() {
  selDate.value = null
  emit('update:modelValue', '')
  open.value = false
}

function prevMonth() {
  if (viewMonth.value === 0) { viewMonth.value = 11; viewYear.value-- }
  else viewMonth.value--
}
function nextMonth() {
  if (viewMonth.value === 11) { viewMonth.value = 0; viewYear.value++ }
  else viewMonth.value++
}

function onDocClick(e) {
  if (open.value && root.value && !root.value.contains(e.target)) open.value = false
}
function onKey(e) {
  if (e.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<style scoped>
.dtp { position: relative; }
.dtp-input { position: relative; }
.dtp-input input {
  cursor: pointer;
  padding-right: 44px;
}
.dtp-icon {
  position: absolute; right: 12px; top: 50%; transform: translateY(-50%);
  color: var(--text-dim); font-size: 10px; pointer-events: none;
}
.dtp-clear {
  position: absolute; right: 28px; top: 50%; transform: translateY(-50%);
  background: transparent; border: none; color: var(--text-dim); cursor: pointer;
  font-size: 14px; line-height: 1; padding: 2px 4px;
}
.dtp-clear:hover { color: var(--text); }
.dtp-popup {
  position: absolute; left: 0; top: calc(100% + 4px); z-index: 1000;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 10px; width: 272px;
  box-shadow: 0 12px 40px rgba(0,0,0,0.5);
}
.dtp-head { display: flex; align-items: center; gap: 4px; margin-bottom: 6px; }
.dtp-nav {
  background: var(--bg-input); color: var(--text-dim); border: 1px solid var(--border);
  border-radius: var(--radius-sm); width: 26px; height: 26px; cursor: pointer;
  font-size: 14px; line-height: 1; flex-shrink: 0;
}
.dtp-nav:hover { color: var(--text); border-color: var(--text-dim); }
.dtp-sel {
  flex: 1; margin: 0; padding: 4px 6px; font-size: 11px;
}
.dtp-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
.dtp-day {
  height: 28px; border: none; background: transparent; color: var(--text);
  border-radius: var(--radius-sm); cursor: pointer; font-size: 12px;
}
.dtp-day:hover:not(:disabled) { background: var(--bg-card-hover); }
.dtp-day.today { border: 1px solid var(--text-dim); }
.dtp-day.selected { background: var(--accent); color: #0b0d12; font-weight: 700; }
.dtp-day.past { color: #4a4d58; cursor: not-allowed; }
.dtp-time {
  display: flex; align-items: center; gap: 6px; margin-top: 8px;
}
.dtp-time .dtp-sel { flex: 1; }
.dtp-colon { color: var(--text-dim); font-size: 12px; }
.dtp-actions {
  display: flex; justify-content: flex-end; margin-top: 8px;
}
.dtp-actions .btn { margin: 0; }
</style>
