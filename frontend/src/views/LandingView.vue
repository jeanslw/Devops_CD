<template>
  <div class="landing">
    <!-- 粒子背景 -->
    <ParticleBackground />

    <!-- 导航栏 -->
    <nav class="landing-nav">
      <div class="nav-brand">
        <img :src="logoUrl" alt="Logo" class="nav-logo" />
        <span class="nav-brand-text">Devops-Glue CD</span>
      </div>
      <div class="nav-links">
        <a href="#">{{ $t('landing.nav.home') }}</a>
        <div class="nav-lang">
          <button :class="{ active: locale === 'zh' }" @click="setLang('zh')">中文</button>
          <button :class="{ active: locale === 'en' }" @click="setLang('en')">EN</button>
        </div>
        <!-- 登录入口固定指向 /login(Devops-Glue 单点登录). -->
        <router-link to="/login" class="nav-btn">🔐 {{ $t('landing.nav.login') }}</router-link>
      </div>
    </nav>

    <!-- 主视觉区 -->
    <section class="landing-hero">
      <h1>
        <span class="glow">{{ $t('landing.hero.text1') }}</span>
        &nbsp;→&nbsp;
        <span class="glow">{{ $t('landing.hero.text2') }}</span>
      </h1>
      <div class="hero-status" :class="statusInfo.cls">
        <span class="status-dot"></span>
        <span class="status-text">{{ statusInfo.label }}</span>
        <template v-if="statusInfo.detail">
          <span class="status-sep">·</span>
          <span class="status-detail">{{ statusInfo.detail }}</span>
        </template>
      </div>
    </section>

    <!-- 功能卡片 -->
    <section class="landing-cards">
      <div v-for="card in cards" :key="card.key" class="landing-card">
        <div class="card-icon">{{ card.icon }}</div>
        <h3>{{ card.title }}</h3>
        <p v-html="card.desc"></p>
      </div>
    </section>

    <!-- 页脚 -->
    <footer class="landing-footer">
      Copyright © 2026 jeanslw
      <span class="divider">|</span>
      <a href="https://github.com/jeanslw/Devops-Glue" target="_blank" rel="noopener">DevOps-Glue</a>
      <span class="divider">|</span>
      <a href="https://github.com/jeanslw/Devops_CD" target="_blank" rel="noopener">DevOps-CD</a>
      <span class="divider">|</span>
      <a href="mailto:jeanslw@qq.com">{{ $t('landing.nav.support') }}</a>
      <template v-if="version">
        <span class="divider">|</span>
        <span>version: v{{ version }}</span>
      </template>
    </footer>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { setLang } from '@/locales'
import { useAppInfo } from '@/composables/useAppInfo'
import { useStatus } from '@/composables/useStatus'
import ParticleBackground from '@/components/ParticleBackground.vue'

const { t, locale } = useI18n()
const { version } = useAppInfo()
const { status, refresh } = useStatus()

const logoUrl = '/static/logo.png'

const cards = computed(() => [
  { key: 'trigger',  icon: '🚀', title: t('landing.cards.trigger.title'),  desc: t('landing.cards.trigger.desc') },
  { key: 'registry', icon: '📦', title: t('landing.cards.registry.title'), desc: t('landing.cards.registry.desc') },
  { key: 'k8s',      icon: '☸️', title: t('landing.cards.k8s.title'),      desc: t('landing.cards.k8s.desc') },
  { key: 'verify',   icon: '🔍', title: t('landing.cards.verify.title'),   desc: t('landing.cards.verify.desc') },
  { key: 'shell',    icon: '🖥️', title: t('landing.cards.shell.title'),    desc: t('landing.cards.shell.desc') },
  { key: 'notify',   icon: '🔔', title: t('landing.cards.notify.title'),   desc: t('landing.cards.notify.desc') },
])

const STATUS_POLL_MS = 30000

const statusInfo = computed(() => {
  const s = status.value
  if (!s) {
    return { cls: 'st-checking', label: t('landing.status.checking'), detail: '' }
  }
  const label = t(`landing.status.${s.status}`)
  const detailKey = `landing.status.detail.${s.status_detail}`
  let detail = ''
  if (s.status_detail === 'oidc') {
    detail = t(detailKey, { items: (s.oidc_missing || []).join(' / ') })
  } else if (s.status_detail === 'not_deployed') {
    detail = t(detailKey, { tables: (s.tables_missing || []).length })
  } else {
    detail = t(detailKey)
  }
  return { cls: `st-${s.status}`, label, detail }
})

let pollId = null
onMounted(() => {
  pollId = setInterval(refresh, STATUS_POLL_MS)
})
onUnmounted(() => {
  if (pollId) clearInterval(pollId)
})
</script>

<style scoped>
.hero-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 12px;
  margin-top: 22px;
  background: rgba(16, 185, 129, 0.12);
  border: 1px solid rgba(16, 185, 129, 0.45);
  border-radius: 999px;
  color: #34d399;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.5px;
}
.hero-status .status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 0 0 currentColor;
  animation: status-pulse 1.8s infinite;
}
.hero-status .status-sep { opacity: 0.5; }
.hero-status .status-detail { font-weight: 400; opacity: 0.85; }
.hero-status.st-checking {
  background: rgba(96, 165, 250, 0.12);
  border-color: rgba(96, 165, 250, 0.45);
  color: #93c5fd;
}
.hero-status.st-stopped,
.hero-status.st-not_deployed,
.hero-status.st-misconfigured {
  background: rgba(251, 191, 36, 0.12);
  border-color: rgba(251, 191, 36, 0.45);
  color: #fbbf24;
}
.hero-status.st-unreachable,
.hero-status.st-db_error {
  background: rgba(248, 113, 113, 0.12);
  border-color: rgba(248, 113, 113, 0.45);
  color: #f87171;
}
@keyframes status-pulse {
  0%   { box-shadow: 0 0 0 0    currentColor; }
  70%  { box-shadow: 0 0 0 10px transparent; }
  100% { box-shadow: 0 0 0 0    transparent; }
}
.landing-footer {
  color: #cbd5e1;
}
.landing-footer a {
  color: #cbd5e1;
  text-decoration: none;
  transition: color 0.2s;
}
.landing-footer a:hover {
  color: #60a5fa;
  text-decoration: underline;
}
.landing-footer .divider {
  margin: 0 10px;
  color: #cbd5e1;
  opacity: 0.65;
}
</style>
