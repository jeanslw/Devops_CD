<template>
  <div class="landing">
    <!-- 粒子背景 -->
    <div class="landing-bg"><canvas ref="canvasRef"></canvas></div>

    <!-- 导航栏 -->
    <nav class="landing-nav">
      <div class="nav-brand">
        <img :src="logoUrl" alt="Logo" class="nav-logo" />
        Devops-Glue CD
      </div>
      <div class="nav-links">
        <a href="#">{{ $t('landing.nav.home') }}</a>
        <div class="nav-lang">
          <button :class="{ active: locale === 'zh' }" @click="setLang('zh')">中文</button>
          <button :class="{ active: locale === 'en' }" @click="setLang('en')">EN</button>
        </div>
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
      <div class="hero-status">
        <span class="status-dot"></span>
        <span class="status-text">{{ $t('landing.running') }}</span>
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
    </footer>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { setLang } from '@/locales'

const { t, locale } = useI18n()

const logoUrl = '/static/logo.png'

const cards = computed(() => [
  { key: 'trigger',  icon: '🚀', title: t('landing.cards.trigger.title'),  desc: t('landing.cards.trigger.desc') },
  { key: 'registry', icon: '📦', title: t('landing.cards.registry.title'), desc: t('landing.cards.registry.desc') },
  { key: 'k8s',      icon: '☸️', title: t('landing.cards.k8s.title'),      desc: t('landing.cards.k8s.desc') },
  { key: 'verify',   icon: '🔍', title: t('landing.cards.verify.title'),   desc: t('landing.cards.verify.desc') },
  { key: 'shell',    icon: '🖥️', title: t('landing.cards.shell.title'),    desc: t('landing.cards.shell.desc') },
  { key: 'notify',   icon: '🔔', title: t('landing.cards.notify.title'),   desc: t('landing.cards.notify.desc') },
])

// 粒子动画
const canvasRef = ref(null)
let animId = null
let particles = []
let W, H

function resize() {
  const c = canvasRef.value
  if (!c) return
  W = c.width = window.innerWidth
  H = c.height = window.innerHeight
}

function initParticles() {
  particles = []
  for (let i = 0; i < 60; i++) {
    particles.push({
      x: Math.random() * W,
      y: Math.random() * H,
      r: Math.random() * 1.5 + 0.5,
      vx: (Math.random() - 0.5) * 0.3,
      vy: (Math.random() - 0.5) * 0.3,
      c: `rgba(0,${180 + Math.random() * 75},${120 + Math.random() * 136},${0.15 + Math.random() * 0.25})`,
    })
  }
}

function draw() {
  const c = canvasRef.value
  if (!c) return
  const ctx = c.getContext('2d')
  ctx.clearRect(0, 0, W, H)
  particles.forEach((p) => {
    ctx.beginPath()
    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
    ctx.fillStyle = p.c
    ctx.fill()
    p.x += p.vx
    p.y += p.vy
    if (p.x < 0) p.x = W
    if (p.x > W) p.x = 0
    if (p.y < 0) p.y = H
    if (p.y > H) p.y = 0
  })
  animId = requestAnimationFrame(draw)
}

onMounted(() => {
  resize()
  initParticles()
  draw()
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  cancelAnimationFrame(animId)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('visibilitychange', onVisibility)
})

function onResize() {
  resize()
  initParticles()
}

function onVisibility() {
  if (document.hidden) {
    cancelAnimationFrame(animId)
    animId = null
  } else if (!animId) {
    draw()
  }
}
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
  background: #10b981;
  box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
  animation: status-pulse 1.8s infinite;
}
@keyframes status-pulse {
  0%   { box-shadow: 0 0 0 0    rgba(16, 185, 129, 0.7); }
  70%  { box-shadow: 0 0 0 10px rgba(16, 185, 129, 0);   }
  100% { box-shadow: 0 0 0 0    rgba(16, 185, 129, 0);   }
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
