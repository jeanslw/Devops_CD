<template>
  <div class="login-lang">
    <button :class="{ active: locale === 'zh' }" @click="setLang('zh')">中文</button>
    <button :class="{ active: locale === 'en' }" @click="setLang('en')">EN</button>
  </div>
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
import { setLang } from '@/locales'

const router = useRouter()
const auth = useAuth()
const { t, te, locale } = useI18n()
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
    if (r.ok) {
      auth.setAuthenticated()
      router.push('/')
    } else {
      // 与 useError 同一策略：后端给了 error_key 就按当前语言翻译（否则切 EN 时登录框仍是后端的中文兜底文案）；
      // 无 key / 当前语言缺该 key 时回退后端文案，最后兜底本地通用失败文案。
      const key = d && d.error_key
      err.value = key && te(key)
        ? t(key, (d && d.error_params) || {})
        : (d && d.error ? d.error : t('login.loginFailed'))
    }
  } catch (e) {
    err.value = t('login.networkError')
  }
}
</script>
