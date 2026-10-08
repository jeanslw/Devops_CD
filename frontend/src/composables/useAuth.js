import { reactive } from 'vue'

// token 存于 HttpOnly cookie（服务端下发），JS 不可读（防 XSS 窃取）。
// sessionStorage 只放一个非敏感标志 cd_authed（仅供路由守卫同步判断，无凭证价值）。
const state = reactive({
  authenticated: !!sessionStorage.getItem('cd_authed'),
  initialized: !!sessionStorage.getItem('cd_authed'),
  user: null,          // { username, role, permissions: [...] }
  loadError: false,    // 首次加载用户信息失败
})

export function useAuth() {
  // 同源 fetch 默认携带 cookie（credentials: 'same-origin'），无需手动加 Authorization 头
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
        logout()  // cookie 失效/过期：清理本地状态
      } else {
        if (!state.user) state.loadError = true
        state.user = null
      }
    } catch {
      // 已登录用户的网络闪断：保留旧 user，避免 UI 闪白
      if (!state.user) state.loadError = true
    }
  }

  async function logout() {
    try {
      // 通知服务端吊销会话 + 清除 HttpOnly cookie；失败不影响本地清理
      await fetch('/api/logout', { method: 'POST', headers: A() })
    } catch {
      // 网络异常时跳过：服务端会话会随 TTL 过期自动失效
    }
    state.authenticated = false
    state.user = null
    sessionStorage.removeItem('cd_authed')
    state.initialized = false
  }

  function handle401(r) {
    if (r.status === 401) {
      logout()
      return true
    }
    return false
  }

  // ── 底层权限检查 ──
  function hasPerm(key) {
    return state.user?.permissions?.includes(key) || false
  }

  // ── super_admin 角色判断 ──
  function isSuperAdmin() {
    return state.user?.role === 'super_admin'
  }

  // ── 一级菜单权限 ──
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

  // ── 二级操作权限 ──
  function canDeploySingle()  { return hasPerm('cd.deploy.single') || isSuperAdmin() }
  function canDeployDocker()  { return hasPerm('cd.deploy.docker') || isSuperAdmin() }
  function canDeployK8s()     { return hasPerm('cd.deploy.k8s') || isSuperAdmin() }
  // 审批：审批中心菜单权限（审批人 / 有部署权限的申请人 / super_admin）
  function canViewApprovals() { return hasPerm('cd.approval-center') || hasPerm('cd.deploy.approve') || hasPerm('cd.deploy-manage') || isSuperAdmin() }
  function canApprove()       { return hasPerm('cd.deploy.approve') || isSuperAdmin() }
  // 审批规则管理（新增/编辑/删除审批规则）：审批人 cd.deploy.approve，不写死角色名
  function canManageApprovalRules() { return hasPerm('cd.deploy.approve') || isSuperAdmin() }
  function canMonitorApp()    { return hasPerm('cd.monitor.app') || isSuperAdmin() }
  function canMonitorSystem() { return hasPerm('cd.monitor.system') || isSuperAdmin() }
  function canMonitorCustom() { return hasPerm('cd.monitor.custom') || isSuperAdmin() }
  function canMonitorAlert()  { return hasPerm('cd.monitor.alert') || isSuperAdmin() }
  function canTriggerBuild()  { return hasPerm('ci.trigger') || isSuperAdmin() }

  return {
    state, A, setAuthenticated, setUser, fetchMe, logout, handle401,
    hasPerm, isSuperAdmin,
    // 一级
    canBuildManage, canDeployManage, canServerManage, canWebshell,
    canDeployRecord, canImageRegistry, canResourceMonitor, canNotificationManage,
    canBot, canWebhook,
    // 二级
    canDeploySingle, canDeployDocker, canDeployK8s,
    canMonitorApp, canMonitorSystem, canMonitorCustom, canMonitorAlert,
    canTriggerBuild, canViewApprovals, canApprove, canManageApprovalRules,
  }
}
