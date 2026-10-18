<template>
  <canvas ref="canvasRef" class="particle-bg"></canvas>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

const canvasRef = ref(null)
let animId = null
let particles = []
let W, H

function resize() {
  const c = canvasRef.value
  if (!c) return
  W = c.width = window.innerWidth
  H = c.height = window.innerHeight
}

function initParticles() {
  particles = []
  for (let i = 0; i < 60; i++) {
    particles.push({
      x: Math.random() * W,
      y: Math.random() * H,
      r: Math.random() * 1.5 + 0.5,
      vx: (Math.random() - 0.5) * 0.3,
      vy: (Math.random() - 0.5) * 0.3,
      c: `rgba(0,${180 + Math.random() * 75},${120 + Math.random() * 136},${0.15 + Math.random() * 0.25})`,
    })
  }
}

function draw() {
  const c = canvasRef.value
  if (!c) return
  const ctx = c.getContext('2d')
  ctx.clearRect(0, 0, W, H)
  particles.forEach((p) => {
    ctx.beginPath()
    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
    ctx.fillStyle = p.c
    ctx.fill()
    p.x += p.vx
    p.y += p.vy
    if (p.x < 0) p.x = W
    if (p.x > W) p.x = 0
    if (p.y < 0) p.y = H
    if (p.y > H) p.y = 0
  })
  animId = requestAnimationFrame(draw)
}

function onResize() {
  resize()
  initParticles()
}

function onVisibility() {
  if (document.hidden) {
    cancelAnimationFrame(animId)
    animId = null
  } else if (!animId) {
    draw()
  }
}

onMounted(() => {
  resize()
  initParticles()
  draw()
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  cancelAnimationFrame(animId)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<style scoped>
.particle-bg {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  z-index: 0;
  pointer-events: none;
  display: block;
}
</style>
