import { createI18n } from 'vue-i18n'
import en from './en.js'
import zh from './zh.js'

const saved = localStorage.getItem('cd_lang')
const defaultLocale = saved || navigator.language?.startsWith('zh') ? 'zh' : 'en'

const i18n = createI18n({
  legacy: false,
  locale: defaultLocale,
  fallbackLocale: 'en',
  messages: { en, zh }
})

export function setLang(locale) {
  i18n.global.locale.value = locale
  localStorage.setItem('cd_lang', locale)
  document.documentElement.lang = locale === 'zh' ? 'zh' : 'en'
}

export default i18n
