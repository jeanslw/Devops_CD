import { ref, onUnmounted } from 'vue'
import { useAuth } from './useAuth'

const DEFAULT_INTERVAL = 30000

export function useCiPolling() {
  const auth = useAuth()
  let _timer = null

  const interval = ref(loadInterval())

  function loadInterval() {
    const v = localStorage.getItem('cd_refresh_ci')
    return v !== null ? parseInt(v) : DEFAULT_INTERVAL
  }

  function saveInterval(ms) {
    interval.value = ms
    localStorage.setItem('cd_refresh_ci', ms)
  }

  function start(callback) {
    stop()
    const ms = interval.value
    if (!ms || ms <= 0) return
    _timer = setInterval(callback, ms)
  }

  function stop() {
    if (_timer) { clearInterval(_timer); _timer = null }
  }

  onUnmounted(stop)

  return { interval, saveInterval, start, stop, auth }
}
