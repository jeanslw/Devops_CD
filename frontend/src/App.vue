<template>
  <div id="app-root">
    <div v-if="auth.state.token" class="main-app">
      <Topbar @logout="auth.logout" />
      <div class="layout">
        <Sidebar />
        <div class="content">
          <Toast />
          <router-view v-slot="{ Component }">
            <transition name="fade">
              <component :is="Component" :key="$route.fullPath" />
            </transition>
          </router-view>
        </div>
      </div>
    </div>
    <div v-else class="login-page">
      <LandingView v-if="$route.path !== '/login'" />
      <LoginView v-else />
    </div>
  </div>
</template>

<script setup>
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import Topbar from '@/components/Topbar.vue'
import Sidebar from '@/components/Sidebar.vue'
import Toast from '@/components/Toast.vue'
import LoginView from '@/views/LoginView.vue'
import LandingView from '@/views/LandingView.vue'
import { provide, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

const router = useRouter()
const auth = useAuth()
const { toast } = useToast()
const { t } = useI18n()
provide('auth', auth)

// 启动时加载用户信息
onMounted(() => {
  if (auth.state.token) auth.fetchMe()
})

// 登录成功后自动跳转到首页
watch(() => auth.state.token, (val, oldVal) => {
  if (val && !oldVal) {
    router.push('/')
  }
})

// 首次加载用户信息失败时提示
watch(() => auth.state.loadError, (val) => {
  if (val) toast(t('errors.load_user_failed'), false)
})
</script>
