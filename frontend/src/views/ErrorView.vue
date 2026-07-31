<template>
  <div class="error-page">
    <div class="error-card">
      <div class="code">{{ code }}</div>
      <div class="text">{{ t(`error_page.${codeText}.title`) }}</div>
      <div class="hint">{{ t(`error_page.${codeText}.hint`) }}</div>
      <a :href="linkHref" class="back-btn">{{ t(`error_page.${codeText}.action`) }}</a>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  code: { type: [Number, String], required: true }
})

const { t } = useI18n()

const codeText = computed(() => String(props.code))
const linkHref = computed(() => {
  const c = Number(props.code)
  return c === 401 ? '/login' : '/'
})
</script>

<style scoped>
.error-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0f0f11;
}
.error-card {
  text-align: center;
  padding: 48px 40px;
  border-radius: 12px;
  border: 1px solid rgba(255,255,255,.08);
  background: rgba(255,255,255,.03);
  max-width: 400px;
  width: 90%;
}
.code {
  font-size: 72px;
  font-weight: 700;
  color: rgba(255,255,255,.12);
  line-height: 1;
  margin-bottom: 12px;
}
.text {
  font-size: 18px;
  color: rgba(255,255,255,.7);
  margin-bottom: 8px;
}
.hint {
  font-size: 13px;
  color: rgba(255,255,255,.35);
  margin-bottom: 28px;
}
.back-btn {
  display: inline-block;
  padding: 8px 24px;
  border-radius: 6px;
  background: rgba(99, 102, 241, 0.2);
  color: #a5b4fc;
  text-decoration: none;
  font-size: 14px;
  font-weight: 500;
  border: 1px solid rgba(99, 102, 241, 0.3);
  transition: background .2s;
}
.back-btn:hover {
  background: rgba(99, 102, 241, 0.35);
}
</style>
