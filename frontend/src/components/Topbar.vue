<template>
  <div class="topbar">
    <div class="topbar-left">
      <button class="menu-toggle" @click="$emit('toggle-sidebar')" :aria-label="$t('topbar.menu')">
        <span></span>
        <span></span>
        <span></span>
      </button>
      <h2>
        <img :src="'/static/logo.png'" alt="Logo" class="topbar-logo">
        <span class="topbar-title">Devops-Glue CD</span>
      </h2>
    </div>
    <div class="topbar-actions">
      <div class="user-tag" v-if="auth.state.user">
        <span class="user-icon">👤</span>
        <span class="user-name">{{ auth.state.user.username }}</span>
        <span class="user-role">{{ $t('users.role_' + auth.state.user.role) }}</span>
      </div>
      <div class="lang-toggle">
        <button :class="['lang-btn', locale === 'en' ? 'active' : '']" @click="setLang('en')">EN</button>
        <button :class="['lang-btn', locale === 'zh' ? 'active' : '']" @click="setLang('zh')">中文</button>
      </div>
      <a class="logout-link" href="#" @click.prevent="$emit('logout')">{{ $t('topbar.logout') }}</a>
    </div>
  </div>
</template>

<script setup>
import { inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { setLang } from '@/locales'

const auth = inject('auth')

defineEmits(['logout', 'toggle-sidebar'])

const { locale } = useI18n()
</script>
