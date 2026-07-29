import { reactive, watch } from 'vue'

const state = reactive({
  token: sessionStorage.getItem('cd_token') || '',
  initialized: !!sessionStorage.getItem('cd_token'),
  user: null,          // { username, role, permissions: [...] }
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
      } else {
        state.user = null
      }
    } catch {
      state.user = null
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

  function hasPerm(key) {
    return state.user?.permissions?.includes(key) || false
  }

  function isAdmin() {
    return hasPerm('cd.admin')
  }

  function isSuperAdmin() {
    return hasPerm('cd.super_admin')
  }

  function isDeployer() {
    return hasPerm('cd.deploy') && !hasPerm('cd.admin') && !hasPerm('cd.super_admin')
  }

  function canDeploy() {
    return hasPerm('cd.deploy') || isSuperAdmin()
  }

  function canManage() {
    return hasPerm('cd.admin') || isSuperAdmin()
  }

  function canTriggerBuild() {
    return hasPerm('ci.trigger')
  }

  return { state, A, setToken, setUser, fetchMe, logout, handle401, hasPerm, isAdmin, isDeployer, isSuperAdmin, canDeploy, canManage, canTriggerBuild }
}
