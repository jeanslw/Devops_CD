import { reactive } from 'vue'

// token 存于 HttpOnly cookie,JS 不可读;sessionStorage 只放非敏感标志 cd_authed.
const state = reactive({
  authenticated: !!sessionStorage.getItem('cd_authed'),
  initialized: !!sessionStorage.getItem('cd_authed'),
  user: null,          // { username, role, permissions: [...] }
  loadError: false,    // 首次加载用户信息失败
})

// bootstrap 去重
let bootstrapping = false

export function useAuth() {
  // 同源 fetch 自动携带 cookie,无需手动加 Authorization 头
  const A = () => ({})

  function setAuthenticated() {
    sessionStorage.setItem('cd_authed', '1')
    state.authenticated = true
    state.initialized = true
    fetchMe()
  }

  function setUser(u) {
    state.user = u
  }

  async function fetchMe() {
    if (!state.authenticated) return
    try {
      const r = await fetch('/api/me', { headers: A() })
      if (r.ok) {
        state.user = await r.json()
        state.loadError = false
      } else if (r.status === 401) {
        logout()  // cookie 失效/过期:清理本地状态
      } else {
        if (!state.user) state.loadError = true
        state.user = null
      }
    } catch {
      if (!state.user) state.loadError = true
    }
  }

  async function logout() {
    try {
      // 通知服务端吊销会话 + 清除 HttpOnly cookie;失败不影响本地清理
      await fetch('/api/logout', { method: 'POST', headers: A() })
    } catch {
    }
    state.authenticated = false
    state.user = null
    sessionStorage.removeItem('cd_authed')
    state.initialized = false
  }

  // 用一次 /api/me 让服务端 cookie 会话自证:200 进入已登录态并补上本地标志,401 维持未登录渲染.
  async function bootstrap() {
    if (state.authenticated || bootstrapping) return
    bootstrapping = true
    try {
      const r = await fetch('/api/me', { headers: A() })
      if (r.ok) {
        state.user = await r.json()
        sessionStorage.setItem('cd_authed', '1')
        state.authenticated = true
        state.initialized = true
        state.loadError = false
      }
    } catch {
    } finally {
      bootstrapping = false
    }
  }

  function handle401(r) {
    if (r.status === 401) {
      logout()
      return true
    }
    return false
  }

  function hasPerm(key) {
    return state.user?.permissions?.includes(key) || false
  }

  function isSuperAdmin() {
    return state.user?.role === 'super_admin'
  }

  function canBuildManage()       { return hasPerm('cd.build-manage') || isSuperAdmin() }
  function canDeployManage()      { return hasPerm('cd.deploy-manage') || isSuperAdmin() }
  function canServerManage()      { return hasPerm('cd.server-manage') || isSuperAdmin() }
  function canWebshell()          { return hasPerm('cd.webshell') || isSuperAdmin() }
  function canDeployRecord()      { return hasPerm('cd.deploy-record') || isSuperAdmin() }
  function canImageRegistry()     { return hasPerm('cd.image-registry') || isSuperAdmin() }
  function canResourceMonitor()   { return hasPerm('cd.resource-monitor') || isSuperAdmin() }
  function canNotificationManage(){ return hasPerm('cd.notification-manage') || isSuperAdmin() }
  function canBot()              { return hasPerm('cd.bot') || isSuperAdmin() }
  function canWebhook()          { return hasPerm('cd.webhook') || isSuperAdmin() }

  function canDeploySingle()  { return hasPerm('cd.deploy.single') || isSuperAdmin() }
  function canDeployDocker()  { return hasPerm('cd.deploy.docker') || isSuperAdmin() }
  function canDeployK8s()     { return hasPerm('cd.deploy.k8s') || isSuperAdmin() }
  function canViewApprovals() { return hasPerm('cd.approval-center') || hasPerm('cd.deploy.approve') || hasPerm('cd.deploy-manage') || isSuperAdmin() }
  function canApprove()       { return hasPerm('cd.deploy.approve') || isSuperAdmin() }
  function canManageApprovalRules() { return hasPerm('cd.deploy.approve') || isSuperAdmin() }
  function canMonitorApp()    { return hasPerm('cd.monitor.app') || isSuperAdmin() }
  function canMonitorSystem() { return hasPerm('cd.monitor.system') || isSuperAdmin() }
  function canMonitorCustom() { return hasPerm('cd.monitor.custom') || isSuperAdmin() }
  function canMonitorAlert()  { return hasPerm('cd.monitor.alert') || isSuperAdmin() }
  function canTriggerBuild()  { return hasPerm('ci.trigger') || isSuperAdmin() }

  return {
    state, A, setAuthenticated, setUser, fetchMe, logout, handle401, bootstrap,
    hasPerm, isSuperAdmin,
    canBuildManage, canDeployManage, canServerManage, canWebshell,
    canDeployRecord, canImageRegistry, canResourceMonitor, canNotificationManage,
    canBot, canWebhook,
    canDeploySingle, canDeployDocker, canDeployK8s,
    canMonitorApp, canMonitorSystem, canMonitorCustom, canMonitorAlert,
    canTriggerBuild, canViewApprovals, canApprove, canManageApprovalRules,
  }
}
