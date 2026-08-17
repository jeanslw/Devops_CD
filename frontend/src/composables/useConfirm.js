import { reactive } from 'vue'

// 全局确认弹窗状态（模块级单例，ConfirmModal 组件监听此 state）
const state = reactive({
  show: false,
  title: '',
  text: '',
  confirmText: '',
  cancelText: '',
  danger: false, // true = 红色确认按钮（停止/删除等危险操作）
  resolve: null,
})

/**
 * 弹出确认框，返回 Promise<boolean>（true=确认，false=取消）。
 * @param {{title?:string, text?:string, confirmText?:string, cancelText?:string, danger?:boolean}} opts
 */
export function confirm(opts = {}) {
  return new Promise((resolve) => {
    state.title = opts.title || ''
    state.text = opts.text || ''
    state.confirmText = opts.confirmText || ''
    state.cancelText = opts.cancelText || ''
    state.danger = !!opts.danger
    state.resolve = resolve
    state.show = true
  })
}

// 供 ConfirmModal 组件调用
export function resolveConfirm(val) {
  state.show = false
  state.resolve?.(val)
  state.resolve = null
}

export function useConfirm() {
  return { confirmState: state, confirm, resolveConfirm }
}
