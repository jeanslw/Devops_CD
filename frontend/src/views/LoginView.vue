<template>
  <div class="login-lang">
    <button :class="{ active: locale === 'zh' }" @click="setLang('zh')">中文</button>
    <button :class="{ active: locale === 'en' }" @click="setLang('en')">EN</button>
  </div>
  <div class="login-card">
    <!-- 本地模式:副标题说明卡片用途;不放大字标题"登 录"(与按钮文案重复). -->
    <!-- AUTH_MODE=oidc:主入口是 Devops-Glue 单点登录,本地表单折叠.模式是服务端全局开关
         (见 docs/SSO_OIDC_ZH.md §6),此时本地登录已被拒绝,因此不提供"切回本地表单"入口.
         登录失败由后端 302 回 /login?error=<key>(§7.1),统一在下方 error 框提示. -->
    <template v-if="sso">
      <!-- 卡片顶部只有这一个登录按钮;下方是说明行,失败时再显示错误框. -->
      <a class="btn btn-green login-sso" href="/api/auth/oidc/start">{{ $t('login.ssoBtn') }}</a>
      <p class="login-sso-hint">{{ $t('login.ssoHint') }}</p>
    </template>
    <!-- 模式未知(/api/info 未返回)时不渲染任何一态,避免首帧本地表单随后切成 SSO 卡片的闪动;
         探测结束(settled)且模式不是 oidc(含探测失败)才渲染本地表单. -->
    <template v-else-if="settled">
      <p>{{ $t('login.subtitle') }}</p>
      <input v-model="user" :placeholder="$t('login.username')" @keydown.enter="doLogin">
      <input v-model="password" type="password" :placeholder="$t('login.password')" @keydown.enter="doLogin">
      <button class="btn btn-green" style="width:100%;justify-content:center" @click="doLogin">{{ $t('login.loginBtn') }}</button>
    </template>
    <!-- 错误框置于两种形态之外:SSO 回跳失败时不必等模式探测完就能显示. -->
    <div class="login-err" :class="{ show: err }" v-text="err"></div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useAppInfo } from '@/composables/useAppInfo'
import { setLang } from '@/locales'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const { t, te, locale } = useI18n()
// 认证模式来自公开接口 GET /api/info(后端只暴露模式名,不含 issuer / secret).
// 与落地页共用 useAppInfo 的模块级缓存:SPA 内跳进来时模式已知,首帧即正确形态.
const { authMode, settled } = useAppInfo()
const user = ref('')
const password = ref('')
const err = ref('')
const sso = computed(() => authMode.value === 'oidc')

onMounted(() => {
  // SSO 回跳失败时后端带 /login?error=errors.xxx(必填项缺失时另带 &missing=OIDC_ISSUER,...):
  // 复用登录失败同一套 te(key)/t(key) 策略,随后清掉 query,避免刷新后旧错误反复出现.
  const key = route.query.error
  if (typeof key === 'string' && key) {
    const items = typeof route.query.missing === 'string' ? route.query.missing.split(',').join(' / ') : ''
    err.value = te(key) ? t(key, { items }) : t('login.loginFailed')
    router.replace({ path: route.path })
  }
})

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
      // 与 useError 同一策略:有 error_key 就按当前语言翻译并插值;
      // 无 key / 当前语言缺该 key 时回退后端文案,最后兜底本地通用文案.
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
