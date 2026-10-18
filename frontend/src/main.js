import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import i18n from './locales'
import './assets/style.css'

const app = createApp(App)
app.use(router)
app.use(i18n)

app.config.errorHandler = (err, instance, info) => {
  console.error('[Vue Error]', err, '\nComponent:', instance?.$?.type?.__name || instance?.$?.type?.name || 'unknown', '\nInfo:', info)
}

window.addEventListener('unhandledrejection', (event) => {
  console.error('[Unhandled Promise]', event.reason)
  event.preventDefault()
})

window.addEventListener('error', (event) => {
  console.error('[Global Error]', event.message, 'at', event.filename, ':', event.lineno)
  if (event.target !== window) {
    event.preventDefault()
  }
}, true)

app.mount('#app')
