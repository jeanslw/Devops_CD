import { reactive, watch } from 'vue'

const state = reactive({
  token: sessionStorage.getItem('cd_token') || '',
  initialized: !!sessionStorage.getItem('cd_token'),
  user: null,          // { username, role }
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

  function isSuperAdmin() {
    return state.user?.role === 'super_admin'
  }

  function isAdmin() {
    return state.user?.role === 'admin' || state.user?.role === 'super_admin'
  }

  function isDeployer() {
    return state.user?.role === 'deployer'
  }

  function canDeploy() {
    return state.user?.role === 'admin' || state.user?.role === 'super_admin' || state.user?.role === 'deployer'
  }

  function canManage() {
    return state.user?.role === 'admin' || state.user?.role === 'super_admin'
  }

  return { state, A, setToken, setUser, fetchMe, logout, handle401, isAdmin, isDeployer, isSuperAdmin, canDeploy, canManage }
}
