import { createRouter, createWebHistory } from 'vue-router'

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
    component: () => import('@/views/LoginView.vue'),
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

// 导航守卫：检查认证
router.beforeEach((to) => {
  const token = sessionStorage.getItem('cd_token')
  if (!token && to.path !== '/login' && to.path !== '/401') {
    return '/401'
  }
})

export default router
