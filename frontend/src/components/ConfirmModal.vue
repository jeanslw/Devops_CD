<template>
  <div v-if="state.show" class="modal-overlay" @click.self="onCancel">
    <div class="modal-box">
      <h4 v-if="state.title" style="margin-top:0">{{ state.title }}</h4>
      <p style="margin:12px 0;white-space:pre-wrap;font-size:13px;line-height:1.6">{{ state.text }}</p>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" @click="onCancel">{{ state.cancelText || t('common.cancel') }}</button>
        <button :class="state.danger ? 'btn btn-red' : 'btn btn-green'" @click="onConfirm">
          {{ state.confirmText || t('common.confirm') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { useI18n } from 'vue-i18n'
import { useConfirm } from '@/composables/useConfirm'

const { t } = useI18n()
const { confirmState: state, resolveConfirm } = useConfirm()

function onConfirm() { resolveConfirm(true) }
function onCancel() { resolveConfirm(false) }
</script>
