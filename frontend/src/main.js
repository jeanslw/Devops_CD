import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import i18n from './locales'
import './assets/style.css'

const app = createApp(App)
app.use(router)
app.use(i18n)

// 全局错误处理，防止单个组件崩溃导致整个应用白屏
app.config.errorHandler = (err, instance, info) => {
  console.error('[Vue Error]', err, '\nComponent:', instance?.$?.type?.__name || instance?.$?.type?.name || 'unknown', '\nInfo:', info)
  // 不要让错误传播导致整个应用崩溃
}

// 捕获未处理的 Promise 错误
window.addEventListener('unhandledrejection', (event) => {
  console.error('[Unhandled Promise]', event.reason)
  event.preventDefault()
})

// 捕获全局 JS 错误
window.addEventListener('error', (event) => {
  console.error('[Global Error]', event.message, 'at', event.filename, ':', event.lineno)
  // 防止网络资源加载错误（如 xterm.js 404）触发未捕获异常
  if (event.target !== window) {
    event.preventDefault()
  }
}, true)

app.mount('#app')
