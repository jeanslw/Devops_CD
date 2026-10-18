import { ref } from 'vue'

const version = ref('')
let loading = null

async function load() {
  try {
    const r = await fetch('/api/info')
    if (!r.ok) return
    const d = await r.json()
    if (d && d.version) version.value = d.version
  } catch {
  }
}

function ensure() {
  if (!loading) {
    loading = load().finally(() => { loading = null })
  }
}

export function useAppInfo() {
  ensure()
  return { version }
}
