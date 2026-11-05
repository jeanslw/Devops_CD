import { reactive } from 'vue'

// token 存于 HttpOnly cookie,JS 不可读;sessionStorage 只放非敏感标志 cd_authed.
const state = reactive({
  authenticated: !!sessionStorage.getItem('cd_authed'),
  initialized: !!sessionStorage.getItem('cd_authed'),
  user: restoreUser(),
  serviceUnavailable: false,
  logoutReason: null,
})

function restoreUser() {
  try {
    const raw = sessionStorage.getItem('cd_user')
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function persistUser() {
  if (state.user) sessionStorage.setItem('cd_user', JSON.stringify(state.user))
}

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
        state.serviceUnavailable = false
        state.logoutReason = null
        persistUser()
      } else if (r.status === 401) {
        logout('session_expired')
      } else if (state.user) {
        state.serviceUnavailable = true
      } else {
        logout('service_unavailable')
      }
    } catch {
      if (state.user) state.serviceUnavailable = true
      else logout('service_unavailable')
    }
  }

  async function logout(reason = null) {
    try {
      await fetch('/api/logout', { method: 'POST', headers: A() })
    } catch {
    }
    state.authenticated = false
    state.user = null
    state.serviceUnavailable = false
    state.logoutReason = reason
    sessionStorage.removeItem('cd_authed')
    sessionStorage.removeItem('cd_user')
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
        state.serviceUnavailable = false
        state.logoutReason = null
        persistUser()
      }
    } catch {
    } finally {
      bootstrapping = false
    }
  }

  function handle401(r) {
    if (r.status === 401) {
      logout('session_expired')
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
