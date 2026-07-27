<template>
  <div class="page">
    <div class="app-card" style="max-width: 640px; margin: 0 auto;">
      <div class="card-header">
        <h3>{{ $t('bots.createBot') }}</h3>
      </div>

      <div class="form-grid" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">
        <input v-model="form.name" :placeholder="$t('bots.name')" autofocus>
        <input v-model="form.type" list="botTypes" :placeholder="$t('bots.type')">
        <datalist id="botTypes">
          <option value="dingtalk"></option>
          <option value="wecom"></option>
          <option value="feishu"></option>
          <option value="telegram"></option>
          <option value="slack"></option>
          <option value="discord"></option>
          <option value="teams"></option>
          <option value="custom"></option>
        </datalist>
        <input v-model="form.url" :placeholder="$t('bots.url')" style="grid-column:1/-1">
      </div>

      <div class="form-group" style="margin-bottom:12px">
        <label style="display:block;margin-bottom:4px;font-size:13px;color:#888">{{ $t('bots.template') }}</label>
        <textarea v-model="form.template" :placeholder="$t('bots.templatePlaceholder')" rows="6" style="width:100%;font-family:monospace;font-size:12px;box-sizing:border-box"></textarea>
        <div style="font-size:11px;color:#aaa;margin-top:4px">{{ $t('bots.templateHint') }}</div>
      </div>

      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" @click="doAdd">{{ $t('common.add') }}</button>
        <button class="btn" @click="$router.push('/bots')">{{ $t('common.cancel') }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, inject } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useToast } from '@/composables/useToast'

const auth = inject('auth')
const router = useRouter()
const { t } = useI18n()
const { toast } = useToast()

const form = reactive({ name: '', type: 'dingtalk', url: '', template: '' })

async function doAdd() {
  const n = form.name.trim()
  const u = form.url.trim()
  if (!n || !u) return toast(t('bots.fillNameAndUrl'), false)
  const r = await fetch('/api/bots', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...auth.A() },
    body: JSON.stringify({ name: n, type: form.type, webhook_url: u, template: form.template })
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    toast(t('bots.added'))
    router.push('/bots')
  } else {
    const e = await r.json()
    toast(e.detail || t('common.failed'))
  }
}
</script>
