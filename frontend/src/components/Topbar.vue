<template>
  <div class="topbar">
    <h2>
      <img :src="'/static/logo.png'" alt="Logo" class="topbar-logo">
      Devops-Glue CD
    </h2>
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
      <a href="#" @click.prevent="$emit('logout')">{{ $t('topbar.logout') }}</a>
    </div>
  </div>
</template>

<script setup>
import { inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { setLang } from '@/locales'

const auth = inject('auth')

defineEmits(['logout'])

const { locale } = useI18n()
</script>
