/** 统一错误处理 — 解析后端 error_key 并 i18n 翻译 */
import { useToast } from './useToast.js'
import { useI18n } from 'vue-i18n'

export function useError() {
  const { toast } = useToast()
  const { t } = useI18n()

  /** 根据 Response 或 body 显示错误提示 */
  async function showError(responseOrBody) {
    let body = responseOrBody
    // 如果是 Response 对象（fetch 未解析），先解析
    if (body && typeof body.json === 'function') {
      try {
        body = await body.json()
      } catch {
        toast(t('common.error'), false)
        return
      }
    }

    const errorKey = body?.error_key
    if (errorKey) {
      // 有 i18n key → 优先翻译
      const params = body?.error_params || {}
      toast(t(`errors.${errorKey}`, params), false)
    } else if (body?.error) {
      // 兜底：原始 error 字段
      toast(body.error, false)
    } else if (body?.detail && typeof body.detail === 'string') {
      toast(body.detail, false)
    } else {
      toast(t('common.error'), false)
    }
  }

  return { showError }
}
