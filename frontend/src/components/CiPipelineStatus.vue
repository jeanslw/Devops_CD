<template>
  <div class="card" v-if="tag">
    <h3>🔍 {{ $t('ci.buildStatus') }}</h3>
    <div style="padding:10px;background:#1b3a1b;border-radius:6px;border:1px solid #388e3c">
      <span style="color:#81c784;font-weight:600">{{ $t('ci.ciCompleted') }}</span>
      <div style="margin-top:4px;font-size:12px;color:#999">
        <span v-if="pipelineIid">Pipeline <b>#{{ pipelineIid }}</b> · </span>
        Tag <b>{{ tag }}</b>
        <span v-if="createdAt"> · {{ createdAt }}</span>
      </div>
    </div>
  </div>
  <div class="card" v-else-if="loading">
    <h3>🔍 {{ $t('ci.buildStatus') }}</h3>
    <span style="color:#888;font-size:12px">选择项目后自动加载</span>
  </div>
</template>

<script setup>
defineProps({
  tag: { type: String, default: '' },
  pipelineIid: { type: [Number, String], default: '' },
  createdAt: { type: String, default: '' },
  loading: { type: Boolean, default: true }
})
</script>
