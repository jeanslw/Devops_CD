import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueI18n from '@intlify/unplugin-vue-i18n/vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [
    vue(),
    // 预编译 i18n 文案（AST），避免 vue-i18n 运行时 new Function 触发 CSP unsafe-eval 拦截
    vueI18n({
      include: [resolve(__dirname, 'src/locales/*.json')],
      strictMessage: false, // 文案含 <br>（landing 卡片 v-html 渲染），默认 true 会报错
    }),
  ],
  base: '/static/',
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/static': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true }
    }
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    assetsDir: 'assets',
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['vue', 'vue-router']
        }
      }
    }
  }
})
