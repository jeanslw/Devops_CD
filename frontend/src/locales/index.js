import { createI18n } from 'vue-i18n'
// 预编译文案由 unplugin-vue-i18n 在构建期生成（AST），运行时不再 new Function
import messages from '@intlify/unplugin-vue-i18n/messages'

const urlParams = new URLSearchParams(window.location.search)
const langParam = urlParams.get('lang')
const saved = langParam || localStorage.getItem('cd_lang')
const defaultLocale = saved || (navigator.language?.startsWith('zh') ? 'zh' : 'en')

const i18n = createI18n({
  legacy: false,
  locale: defaultLocale,
  fallbackLocale: 'en',
  messages,
})

export function setLang(locale) {
  i18n.global.locale.value = locale
  localStorage.setItem('cd_lang', locale)
  document.documentElement.lang = locale === 'zh' ? 'zh' : 'en'
}

export default i18n
