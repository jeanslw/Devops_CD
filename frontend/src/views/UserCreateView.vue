<template>
  <div class="page">
    <div class="app-card" style="max-width: 480px; margin: 0 auto;">
      <div class="card-header">
        <h3>{{ $t('users.createUser') }}</h3>
      </div>

      <div class="form-group">
        <label>{{ $t('users.username') }}</label>
        <input v-model="form.username" :placeholder="$t('users.usernamePlaceholder')" autofocus />
      </div>
      <div class="form-group">
        <label>{{ $t('login.password') }}</label>
        <input v-model="form.password" type="password" :placeholder="$t('users.passwordPlaceholder')" />
      </div>
      <div class="form-group">
        <label>{{ $t('users.role') }}</label>
        <select v-model="form.role">
          <option v-if="isSuperAdmin" value="cd_admin">{{ $t('users.role_cd_admin') }}</option>
          <option value="deployer">{{ $t('users.role_deployer') }}</option>
          <option value="viewer">{{ $t('users.role_viewer') }}</option>
        </select>
      </div>
      <div class="form-group">
        <label>{{ $t('users.systems') }}</label>
        <input :value="$t('users.systems_cd')" disabled />
      </div>

      <div style="display: flex; gap: 8px; margin-top: 16px;">
        <button class="btn btn-primary" @click="doCreate">{{ $t('common.add') }}</button>
        <button class="btn" @click="$router.push('/users')">{{ $t('common.cancel') }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, inject } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'

const auth = inject('auth')
const router = useRouter()
const { t } = useI18n()
const { toast } = useToast()
const { showError } = useError()

const isSuperAdmin = computed(() => auth.isSuperAdmin())

const form = ref({ username: '', password: '', role: 'viewer' })

async function doCreate() {
  if (!form.value.username || !form.value.password) {
    toast(t('users.fillUsernameAndPassword'))
    return
  }
  const r = await fetch('/api/users', {
    method: 'POST',
    headers: { ...auth.A(), 'Content-Type': 'application/json' },
    body: JSON.stringify(form.value),
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    toast(t('users.created'))
    router.push('/users')
  } else {
    await showError(r)
  }
}
</script>
