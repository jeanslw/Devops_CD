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
      <div class="user-menu" v-if="auth.state.user" ref="menuRef">
        <button
          type="button"
          class="user-tag"
          :aria-expanded="menuOpen"
          aria-haspopup="menu"
          :title="$t('topbar.account')"
          @click="toggleMenu"
        >
          <span class="user-icon">👤</span>
          <span class="user-name">{{ auth.state.user.username }}</span>
          <span class="user-role">{{ roleLabel }}</span>
          <span class="user-caret">▾</span>
        </button>
        <div v-if="menuOpen" class="user-dropdown" role="menu">
          <div class="dropdown-user">
            <span class="user-name">{{ auth.state.user.username }}</span>
            <span class="user-role">{{ roleLabel }}</span>
          </div>
          <button type="button" class="dropdown-item is-danger" role="menuitem" @click="onLogout">
            <span class="dropdown-icon">🚪</span>{{ $t('topbar.logout') }}
          </button>
        </div>
      </div>
      <div class="lang-toggle">
        <button :class="['lang-btn', locale === 'en' ? 'active' : '']" @click="setLang('en')">EN</button>
        <button :class="['lang-btn', locale === 'zh' ? 'active' : '']" @click="setLang('zh')">中文</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { inject, computed, ref, watch, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { setLang } from '@/locales'

const auth = inject('auth')

const emit = defineEmits(['logout', 'toggle-sidebar'])

const { locale, t, te } = useI18n()

const roleLabel = computed(() => {
  const role = auth.state.user?.role || ''
  const key = 'users.role_' + role
  return te(key) ? t(key) : role
})

// 点击账号弹出菜单:退出收进菜单,顶栏不再单独挂一个退出链接
const menuOpen = ref(false)
const menuRef = ref(null)

function closeMenu() {
  menuOpen.value = false
}

function toggleMenu() {
  menuOpen.value = !menuOpen.value
}

function onLogout() {
  closeMenu()
  emit('logout')
}

function onDocumentClick(e) {
  if (menuRef.value && !menuRef.value.contains(e.target)) {
    closeMenu()
  }
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    closeMenu()
  }
}

watch(menuOpen, (open) => {
  if (open) {
    document.addEventListener('click', onDocumentClick)
    document.addEventListener('keydown', onKeydown)
  } else {
    document.removeEventListener('click', onDocumentClick)
    document.removeEventListener('keydown', onKeydown)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>
