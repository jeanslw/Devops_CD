// ESLint 9 flat config —— 与 eslint-plugin-vue v10 搭配
// 只开 essential 级（模板解析 / 未定义名等正确性规则），不开风格类规则，
// 避免给既有 Vue 代码引入大量风格报错；关键正确性问题仍能在 CI 挡住。
import pluginVue from 'eslint-plugin-vue'

export default [
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.{js,vue}'],
    rules: {
      // 本项目视图/组件多为单文件，不强制多词组件名
      'vue/multi-word-component-names': 'off',
    },
  },
]
