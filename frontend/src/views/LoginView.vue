<template>
  <ParticleBackground />
  <div class="login-lang">
    <button :class="{ active: locale === 'zh' }" @click="setLang('zh')">中文</button>
    <button :class="{ active: locale === 'en' }" @click="setLang('en')">EN</button>
  </div>
  <div class="login-card">
    <div class="login-brand">
      <img :src="logoUrl" alt="Logo" class="login-logo" />
      <span class="login-title">Devops-Glue CD</span>
    </div>
    <!-- 唯一登录方式:Devops-Glue 单点登录(整页跳转 /api/auth/oidc/start).
         失败由后端 302 回 /login?error=<key>,统一在下方 error 框提示. -->
    <a class="btn btn-green login-sso" href="/api/auth/oidc/start">{{ $t('login.ssoBtn') }}</a>
    <p class="login-sso-hint">{{ $t('login.ssoHint') }}</p>
    <div class="login-err" :class="{ show: err }" v-text="err"></div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { setLang } from '@/locales'
import ParticleBackground from '@/components/ParticleBackground.vue'

const route = useRoute()
const router = useRouter()
const { t, te, locale } = useI18n()
const err = ref('')
const logoUrl = '/static/logo.png'

onMounted(() => {
  const key = route.query.error
  if (typeof key === 'string' && key) {
    const params = {}
    if (typeof route.query.missing === 'string') {
      params.items = route.query.missing.split(',').join(' / ')
    }
    for (const [k, v] of Object.entries(route.query)) {
      if (k === 'error' || k === 'missing') continue
      if (typeof v === 'string') params[k] = v
    }
    err.value = te(key) ? t(key, params) : t('login.loginFailed')
    router.replace({ path: route.path })
  }
})
</script>
