import { ref } from 'vue'

// 产品信息:版本号等,来自后端公开接口 GET /api/info(无需认证).
// 模块级共享:多个组件同时挂载时只请求一次.
const version = ref('')
let loading = null

async function load() {
  try {
    const r = await fetch('/api/info')
    if (!r.ok) return
    const d = await r.json()
    if (d && d.version) version.value = d.version
  } catch {
    // 网络失败静默处理:界面不阻塞,下次挂载时重试
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
