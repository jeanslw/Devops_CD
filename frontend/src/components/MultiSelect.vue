<template>
  <div class="multi-select" :class="{ open: isOpen }" ref="wrapRef">
    <div class="multi-select-trigger" @click="toggleOpen">
      <span class="multi-select-text" :class="{ 'has-selection': selectedIds.length > 0 }">{{ displayText }}</span>
      <span class="multi-select-arrow">▾</span>
    </div>
    <div class="multi-select-dropdown">
      <div class="multi-select-actions">
        <label class="multi-select-all">
          <input type="checkbox" :checked="allChecked" @change="toggleAll($event.target.checked)"> {{ $t('multiSelect.selectAll') }}
        </label>
      </div>
      <div class="multi-select-tags" v-if="tagList.length">
        <span
          v-for="t in tagList" :key="t"
          class="multi-select-tag"
          :class="{ active: activeTags.has(t) }"
          @click.stop="toggleTag(t)"
        >{{ t }}</span>
      </div>
      <div class="multi-select-list">
        <div
          v-for="s in filteredServers" :key="s.id"
          class="multi-select-item"
          @click="toggleItem(s.id)"
        >
          <input type="checkbox" :checked="isChecked(s.id)" @click.stop="toggleItem(s.id)">
          <label>{{ s.name }} ({{ s.host }})</label>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'

const { t: $t } = useI18n()

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  servers: { type: Array, default: () => [] }
})

const emit = defineEmits(['update:modelValue'])

const isOpen = ref(false)
const activeTags = ref(new Set())
const wrapRef = ref(null)

const selectedIds = computed(() => props.modelValue)
const allChecked = computed(() => props.servers.length > 0 && selectedIds.value.length === props.servers.length)

const tagList = computed(() => {
  const set = new Set()
  props.servers.forEach(s => {
    (s.tags || '').split(',').filter(Boolean).forEach(t => set.add(t.trim().toLowerCase()))
  })
  return Array.from(set).sort()
})

const filteredServers = computed(() => {
  if (activeTags.value.size === 0) return props.servers
  return props.servers.filter(s => {
    const stags = (s.tags || '').toLowerCase().split(',').map(t => t.trim())
    return Array.from(activeTags.value).some(t => stags.includes(t))
  })
})

const displayText = computed(() => {
  if (selectedIds.value.length === 0) return $t('multiSelect.placeholder')
  if (selectedIds.value.length === props.servers.length) return $t('multiSelect.selectedAll', { n: selectedIds.value.length })
  const names = props.servers
    .filter(s => selectedIds.value.includes(s.id))
    .map(s => s.name)
  if (names.length <= 3) return names.join(', ')
  return `${names.slice(0, 3).join(', ')} +${names.length - 3}`
})

function isChecked(id) {
  return selectedIds.value.includes(id)
}

function toggleOpen() {
  isOpen.value = !isOpen.value
}

function toggleAll(checked) {
  if (checked) {
    emit('update:modelValue', props.servers.map(s => s.id))
  } else {
    emit('update:modelValue', [])
  }
}

function toggleItem(id) {
  const ids = [...selectedIds.value]
  const idx = ids.indexOf(id)
  if (idx >= 0) ids.splice(idx, 1)
  else ids.push(id)
  emit('update:modelValue', ids)
}

function toggleTag(tag) {
  const s = new Set(activeTags.value)
  if (s.has(tag)) s.delete(tag)
  else s.add(tag)
  activeTags.value = s
}

function handleClickOutside(e) {
  if (wrapRef.value && !wrapRef.value.contains(e.target)) {
    isOpen.value = false
  }
}

onMounted(() => document.addEventListener('click', handleClickOutside))
onUnmounted(() => document.removeEventListener('click', handleClickOutside))
</script>
