<template>
  <div id="app-root">
    <div v-if="auth.state.authenticated" class="main-app">
      <Topbar @logout="auth.logout" @toggle-sidebar="toggleSidebar" />
      <div class="layout">
        <Sidebar :open="sidebarOpen" @close="closeSidebar" />
        <div v-if="sidebarOpen" class="sidebar-overlay" @click="closeSidebar"></div>
        <div class="content" @click="onContentClick">
          <Toast />
          <ConfirmModal />
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
import ConfirmModal from '@/components/ConfirmModal.vue'
import LoginView from '@/views/LoginView.vue'
import LandingView from '@/views/LandingView.vue'
import { provide, watch, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

const router = useRouter()
const auth = useAuth()
const { toast } = useToast()
const { t } = useI18n()
provide('auth', auth)

const MOBILE_BREAKPOINT = 768
const sidebarOpen = ref(window.innerWidth > MOBILE_BREAKPOINT)

function updateSidebarByWidth() {
  sidebarOpen.value = window.innerWidth > MOBILE_BREAKPOINT
}

function toggleSidebar() {
  sidebarOpen.value = !sidebarOpen.value
}

function closeSidebar() {
  sidebarOpen.value = false
}

function onContentClick() {
  if (window.innerWidth <= MOBILE_BREAKPOINT && sidebarOpen.value) {
    sidebarOpen.value = false
  }
}

// 启动时加载用户信息:
// - 已有本地标志(local 模式登录后):直接刷新用户信息;
// - 没有本地标志:向服务端自举一次(OIDC 整页回跳后 sessionStorage 为空,见 useAuth.bootstrap).
//   自举成功时若守卫已把深链踢到 /401,则回到首页,避免用户看到错误页.
onMounted(async () => {
  if (auth.state.authenticated) {
    auth.fetchMe()
  } else {
    await auth.bootstrap()
    if (auth.state.authenticated && router.currentRoute.value.path === '/401') router.replace('/')
  }
  window.addEventListener('resize', updateSidebarByWidth)
})

onUnmounted(() => {
  window.removeEventListener('resize', updateSidebarByWidth)
})

// 登录成功后自动跳转到首页
watch(() => auth.state.authenticated, (val, oldVal) => {
  if (val && !oldVal) {
    router.push('/')
  }
})

// 首次加载用户信息失败时提示
watch(() => auth.state.loadError, (val) => {
  if (val) toast(t('errors.load_user_failed'), false)
})

// 移动端路由切换后自动收起侧边栏
const unsubscribeRoute = router.afterEach(() => {
  if (window.innerWidth <= MOBILE_BREAKPOINT) {
    sidebarOpen.value = false
  }
})
onUnmounted(() => unsubscribeRoute())
</script>
