import { createRouter, createWebHistory } from 'vue-router'
import { useAuth } from '@/composables/useAuth'
// LoginView 由 App.vue 的登录门静态渲染(未登录分支),这里再懒加载无法拆出独立 chunk,
// 故改为静态引入,与 App.vue 同源,避免 Vite 告警「dynamically imported but also statically imported」.
import LoginView from '@/views/LoginView.vue'

const routes = [
  {
    path: '/vue',
    redirect: '/'
  },
  {
    path: '/dashboard',
    redirect: '/'
  },
  {
    path: '/',
    name: 'Home',
    component: () => import('@/views/CiView.vue'),
    meta: { title: 'CI构建结果' }
  },
  {
    path: '/ci-build',
    name: 'CiBuild',
    component: () => import('@/views/CiBuildView.vue'),
    meta: { title: '构建管理' }
  },
  {
    path: '/login',
    name: 'Login',
    component: LoginView,
  },
  {
    path: '/servers',
    name: 'Servers',
    component: () => import('@/views/ServersView.vue'),
    meta: { title: '服务器管理' }
  },
  {
    path: '/deploy/ssh',
    name: 'SshDeploy',
    component: () => import('@/views/SshDeployView.vue'),
    meta: { title: '部署到单机' }
  },
  {
    path: '/deploy/docker',
    name: 'DockerDeploy',
    component: () => import('@/views/DockerDeployView.vue'),
    meta: { title: '部署到Docker' }
  },
  {
    path: '/deploy/k8s',
    name: 'K8sDeploy',
    component: () => import('@/views/K8sDeployView.vue'),
    meta: { title: '部署到K8S' }
  },
  {
    path: '/shell',
    name: 'Shell',
    component: () => import('@/views/ShellView.vue'),
    meta: { title: 'Web Shell' }
  },
  {
    path: '/logs',
    name: 'Logs',
    component: () => import('@/views/LogsView.vue'),
    meta: { title: '部署记录' }
  },
  {
    path: '/approvals',
    name: 'Approvals',
    component: () => import('@/views/ApprovalsView.vue'),
    meta: { title: '审批中心' }
  },
  {
    path: '/registry',
    name: 'Registry',
    component: () => import('@/views/RegistryView.vue'),
    meta: { title: '镜像仓库' }
  },
  {
    path: '/monitor/app',
    name: 'MonitorApp',
    component: () => import('@/views/MonitorAppView.vue'),
    meta: { title: '应用资源' }
  },
  {
    path: '/monitor/system',
    name: 'MonitorSystem',
    component: () => import('@/views/MonitorSystemView.vue'),
    meta: { title: '系统资源' }
  },
  {
    path: '/bots',
    name: 'Bots',
    component: () => import('@/views/BotListView.vue'),
    meta: { title: '通知管理' }
  },
  {
    path: '/webhooks',
    name: 'Webhooks',
    component: () => import('@/views/WebhookView.vue'),
    meta: { title: 'Webhook接收' }
  },
  {
    path: '/bots/create',
    name: 'BotCreate',
    component: () => import('@/views/BotCreateView.vue'),
    meta: { title: '新建通知', admin: true }
  },
  {
    path: '/alerts',
    name: 'Alerts',
    component: () => import('@/views/AlertRulesView.vue'),
    meta: { title: '告警规则' }
  },
  {
    path: '/custom-monitors',
    name: 'CustomMonitors',
    component: () => import('@/views/CustomMonitorsView.vue'),
    meta: { title: '自定义资源' }
  },
  {
    path: '/401',
    name: 'Unauthorized',
    component: () => import('@/views/ErrorView.vue'),
    props: { code: 401 },
    meta: { title: '401' }
  },
  {
    path: '/403',
    name: 'Forbidden',
    component: () => import('@/views/ErrorView.vue'),
    props: { code: 403 },
    meta: { title: '403' }
  },
  {
    path: '/500',
    name: 'ServerError',
    component: () => import('@/views/ErrorView.vue'),
    props: { code: 500 },
    meta: { title: '500' }
  },
  {
    path: '/503',
    name: 'ServiceUnavailable',
    component: () => import('@/views/ErrorView.vue'),
    props: { code: 503 },
    meta: { title: '503' }
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/ErrorView.vue'),
    props: { code: 404 },
    meta: { title: '404' }
  },

]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 导航守卫:检查认证
// OIDC 模式下登录成功是整页跳转(Glue → 302 /api/auth/oidc/callback → /),sessionStorage 的
// 本地标志不会保留 即 深链首次进入时先向服务端自举一次,否则已登录用户会被误踢到 /401.
// local 模式下仅多一次 /api/me 探测(无 cookie 即 401),结果与改动前一致.
router.beforeEach(async (to) => {
  const { state, bootstrap } = useAuth()
  // 允许未登录访问的公开路径:登录页,错误页,首页(显示介绍页)
  const publicPaths = ['/login', '/401', '/']
  if (!state.authenticated && !publicPaths.includes(to.path)) {
    await bootstrap()
    if (!state.authenticated) return '/401'
  }
})

export default router
