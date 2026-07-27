import { reactive } from 'vue'

const state = reactive({
  visible: false,
  message: '',
  ok: true
})

let _timer = null

export function useToast() {
  function toast(msg, ok = true) {
    state.message = msg
    state.ok = ok
    state.visible = true
    clearTimeout(_timer)
    _timer = setTimeout(() => {
      state.visible = false
    }, 3000)
  }

  return { toastState: state, toast }
}
