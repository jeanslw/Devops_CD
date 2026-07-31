import { ref, shallowRef } from 'vue'
import { useAuth } from './useAuth'
import { useI18n } from 'vue-i18n'

export function useSseStream() {
  const auth = useAuth()
  const { t } = useI18n()
  const output = ref('')
  const loading = ref(false)

  async function stream(url, body, opts = {}) {
    output.value = opts.initialMsg || '$ Deploying...\n'
    loading.value = true

    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...auth.A() },
        body: JSON.stringify(body)
      })
      const reader = r.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        while (buffer.includes('\n\n')) {
          const idx = buffer.indexOf('\n\n')
          const block = buffer.substring(0, idx)
          buffer = buffer.substring(idx + 2)

          // SSE 事件块可能含多行 data:，收集所有 data: 行拼接
          const lines = block.split('\n')
          const dataLines = []
          for (const ln of lines) {
            if (ln.startsWith('data: ')) {
              dataLines.push(ln.substring(6))
            }
          }
          const data = dataLines.join('\n')
          if (!data) continue

          if (data.startsWith('ERROR:')) {
            output.value += '\n❌ ' + data.substring(6)
            opts.onError?.()
            return false
          } else if (data.startsWith('END:')) {
            const parts = data.substring(4).split(':')
            const success = parts[0] === 'true'
            opts.onEnd?.(success)
            return success
          } else if (data === '.') {
            continue
          } else if (data.startsWith('STATUS:')) {
            // i18n message from backend: parse JSON and translate
            try {
              const msg = JSON.parse(data.substring(7))
              output.value += t(msg.key, msg) + '\n'
            } catch {
              output.value += data.substring(7) + '\n'
            }
          } else {
            output.value += data + '\n'
          }
        }
      }
    } catch (e) {
      output.value += '\n❌ ' + e.message
      opts.onError?.()
    } finally {
      loading.value = false
    }
    return false
  }

  return { output, loading, stream }
}
