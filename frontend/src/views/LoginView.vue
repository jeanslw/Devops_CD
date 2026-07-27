<template>
  <div class="login-card">
    <h3>{{ $t('login.title') }}</h3>
    <p>{{ $t('login.subtitle') }}</p>
    <input v-model="user" :placeholder="$t('login.username')" @keydown.enter="doLogin">
    <input v-model="password" type="password" :placeholder="$t('login.password')" @keydown.enter="doLogin">
    <button class="btn btn-green" style="width:100%;justify-content:center" @click="doLogin">{{ $t('login.loginBtn') }}</button>
    <div class="login-err" :class="{ show: err }" v-text="err"></div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'

const router = useRouter()
const auth = useAuth()
const { t } = useI18n()
const user = ref('')
const password = ref('')
const err = ref('')

async function doLogin() {
  err.value = ''
  if (!user.value.trim() || !password.value) {
    err.value = t('login.enterCredentials')
    return
  }
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: user.value.trim(), password: password.value })
    })
    const d = await r.json()
    if (r.ok && d.token) {
      auth.setToken(d.token)
      router.push('/')
    } else {
      err.value = d.detail || t('login.loginFailed')
    }
  } catch (e) {
    err.value = t('login.networkError')
  }
}
</script>
