import { ref } from 'vue'

const state = ref(null) // { status, status_detail, oidc_missing, tables_missing, ... } | null
let loading = null

async function load() {
  try {
    const r = await fetch('/api/status')
    if (!r.ok) return
    state.value = await r.json()
  } catch {
  }
}

function refresh() {
  if (!loading) {
    loading = load().finally(() => { loading = null })
  }
}

function ensure() {
  if (state.value === null) refresh()
}

export function useStatus() {
  ensure()
  return { status: state, refresh }
}
