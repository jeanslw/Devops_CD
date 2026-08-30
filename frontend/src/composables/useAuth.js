import { reactive } from 'vue'

const state = reactive({
  token: sessionStorage.getItem('cd_token') || '',
  initialized: !!sessionStorage.getItem('cd_token'),
  user: null,          // { username, role, permissions: [...] }
  loadError: false,    // 首次加载用户信息失败
})

export function useAuth() {
  const A = () => (state.token ? { Authorization: 'Bearer ' + state.token } : {})

  function setToken(t) {
    state.token = t
    sessionStorage.setItem('cd_token', t)
    state.initialized = true
    fetchMe()
  }

  function setUser(u) {
    state.user = u
  }

  async function fetchMe() {
    if (!state.token) return
    try {
      const r = await fetch('/api/me', { headers: A() })
      if (r.ok) {
        state.user = await r.json()
        state.loadError = false
      } else {
        if (!state.user) state.loadError = true
        state.user = null
      }
    } catch {
      // 已登录用户的网络闪断：保留旧 user，避免 UI 闪白
      if (!state.user) state.loadError = true
    }
  }

  function logout() {
    state.token = ''
    state.user = null
    sessionStorage.removeItem('cd_token')
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
  // 审批：审批中心菜单权限 / 审批人可见（发起部署者不放行，避免看到审批规则）
  function canViewApprovals() { return hasPerm('cd.approval-center') || hasPerm('cd.deploy.approve') || isSuperAdmin() }
  function canApprove()       { return hasPerm('cd.deploy.approve') || isSuperAdmin() }
  // 审批规则管理（新增/编辑/删除审批规则）：审批人 cd.deploy.approve，不写死角色名
  function canManageApprovalRules() { return hasPerm('cd.deploy.approve') || isSuperAdmin() }
  function canMonitorApp()    { return hasPerm('cd.monitor.app') || isSuperAdmin() }
  function canMonitorSystem() { return hasPerm('cd.monitor.system') || isSuperAdmin() }
  function canMonitorCustom() { return hasPerm('cd.monitor.custom') || isSuperAdmin() }
  function canMonitorAlert()  { return hasPerm('cd.monitor.alert') || isSuperAdmin() }
  function canTriggerBuild()  { return hasPerm('ci.trigger') || isSuperAdmin() }

  // ── 旧版兼容 ──
  function canDeploy() { return canDeployManage() }
  function canManage() { return canNotificationManage() }
  function isAdmin()   { return canServerManage() }
  function isDeployer(){ return canDeployManage() }

  return {
    state, A, setToken, setUser, fetchMe, logout, handle401,
    hasPerm, isSuperAdmin,
    // 一级
    canBuildManage, canDeployManage, canServerManage, canWebshell,
    canDeployRecord, canImageRegistry, canResourceMonitor, canNotificationManage,
    canBot, canWebhook,
    // 二级
    canDeploySingle, canDeployDocker, canDeployK8s,
    canMonitorApp, canMonitorSystem, canMonitorCustom, canMonitorAlert,
    canTriggerBuild, canViewApprovals, canApprove, canManageApprovalRules,
    // 兼容
    canDeploy, canManage,
  }
}
