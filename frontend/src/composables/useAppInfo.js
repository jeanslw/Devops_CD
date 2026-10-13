import { ref } from 'vue'

// 产品信息:版本号等,来自后端公开接口 GET /api/info(无需认证).
// 模块级共享:多个组件同时挂载时只请求一次.
const version = ref('')
// 认证模式:'local' | 'oidc'.后端只暴露模式名(不含 issuer / secret),登录页据此决定
// 展示 SSO 按钮还是本地表单.
const authMode = ref('')
// 探测是否已有结论(成功或失败都算):登录页据此区分"尚未探测"与"确实是 local".
// 否则未知时先渲染本地表单,随后切成 SSO 卡片,刷页可见两种形态交替闪动.
const settled = ref(false)
let loading = null

async function load() {
  try {
    const r = await fetch('/api/info')
    if (!r.ok) return
    const d = await r.json()
    if (d && d.version) version.value = d.version
    if (d && typeof d.auth_mode === 'string') authMode.value = d.auth_mode
  } catch {
    // 网络失败静默处理:界面不阻塞,下次挂载时重试
  } finally {
    // 失败也置位:调用方据此回退到本地表单,不会永远空着
    settled.value = true
  }
}

function ensure() {
  if (!loading) {
    loading = load().finally(() => { loading = null })
  }
}

export function useAppInfo() {
  ensure()
  return { version, authMode, settled }
}
