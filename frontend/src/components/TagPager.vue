<template>
  <div class="tag-pager">
    <button class="btn-page" :disabled="page <= 1" @click="changePage(-1)">◀</button>
    <select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)" ref="selRef">
      <option v-if="loading" value="">{{ $t('tagPager.loading') }}</option>
      <option v-else-if="tags.length === 0" value="">{{ $t('tagPager.empty') }}</option>
      <option v-for="t in tags" :key="t.tag" :value="t.tag">{{ t.tag }}</option>
    </select>
    <button class="btn-page" :disabled="page >= totalPages" @click="changePage(1)">▶</button>
  </div>
  <span v-if="totalPages > 1" class="tag-info">({{ page }}/{{ totalPages }} {{ $t('tagPager.totalCount', { count: total }) }})</span>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'

const props = defineProps({
  modelValue: String,
  tags: { type: Array, default: () => [] },
  page: { type: Number, default: 1 },
  totalPages: { type: Number, default: 1 },
  total: { type: Number, default: 0 },
  loading: { type: Boolean, default: false }
})

const emit = defineEmits(['update:modelValue', 'pageChange'])
const selRef = ref(null)

function changePage(delta) {
  emit('pageChange', delta)
}

watch(() => props.page, async () => {
  // Auto re-open dropdown after page change
  await nextTick()
  try { selRef.value?.showPicker?.() } catch (_) { selRef.value?.focus?.() }
})
</script>
