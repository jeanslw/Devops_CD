import { ref } from 'vue'

// 落地页"运行中"徽标的真实状态:后端公开接口 GET /api/status(无需认证),只返回非敏感事实.
// 模块级共享:多个组件同时挂载时只请求一次;refresh() 供轮询 / 手动刷新复用.
const state = ref(null) // { status, status_detail, oidc_missing, tables_missing, ... } | null
let loading = null

async function load() {
  try {
    const r = await fetch('/api/status')
    if (!r.ok) return
    state.value = await r.json()
  } catch {
    // 网络失败:保留上一次结果,不阻塞渲染
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
