<template>
  <div class="card">
    <h3>{{ $t('shell.title') }}</h3>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <select v-model="selectedServer" style="width:auto">
        <option :value="0">{{ $t('shell.selectServer') }}</option>
        <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.name }} ({{ s.host }})</option>
      </select>
      <button class="btn btn-green btn-sm" @click="connect">{{ $t('shell.connect') }}</button>
      <button class="btn btn-red btn-sm" @click="disconnect">{{ $t('shell.disconnect') }}</button>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
      <input type="file" ref="fileInput" style="display:none" @change="onFileChange">
      <button class="btn btn-blue btn-sm" style="margin:0" @click="triggerFilePicker">{{ $t('shell.selectFileBtn') }}</button>
      <span class="file-name">{{ selectedFile ? selectedFile.name : $t('shell.noFileSelected') }}</span>
      <input v-model="scpPath" :placeholder="$t('shell.remotePath')" style="width:auto;margin:0" value="/tmp">
      <button class="btn btn-green btn-sm" style="margin:0" @click="upload">{{ $t('shell.upload') }}</button>
    </div>
    <div id="terminal-container" ref="termEl" class="terminal-wrap" style="height:550px;background:#000;border-radius:6px;padding:4px"></div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'

const auth = useAuth()
const { t } = useI18n()
const { toast } = useToast()
const { showError } = useError()

const servers = ref([])
const selectedServer = ref(0)
const termEl = ref(null)
const fileInput = ref(null)
const scpPath = ref('/tmp')
const selectedFile = ref(null)

let term = null
let shellWs = null
let xtermLoaded = false

async function loadServers() {
  try {
    const r = await fetch(`/api/servers?_=${Date.now()}`, { headers: auth.A() })
    servers.value = await r.json()
  } catch (e) {}
}

function loadXtermCSS() {
  if (!document.getElementById('xterm-css')) {
    const link = document.createElement('link')
    link.id = 'xterm-css'
    link.rel = 'stylesheet'
    link.href = '/static/xterm/xterm.min.css'
    document.head.appendChild(link)
  }
}

function loadXtermJS() {
  return new Promise((resolve, reject) => {
    if (window.Terminal) return resolve()
    const s = document.createElement('script')
    s.src = '/static/xterm/xterm.min.js'
    s.onload = () => resolve()
    s.onerror = () => reject(new Error('xterm.js load failed'))
    document.head.appendChild(s)
  })
}

function connect() {
  try {
    const sid = selectedServer.value
    if (!sid || sid === 0) return toast(t('shell.selectServerFirst'), false)
    if (shellWs) disconnect()

    if (!window.Terminal) {
      toast(t('shell.terminalLoading'), false)
      return
    }

    if (!term) {
      term = new window.Terminal({
        cursorBlink: true, fontSize: 14, rows: 28, cols: 100,
        theme: { background: '#000' }
      })
      term.open(termEl.value)
    }
    term.clear()
    term.writeln(t('shell.connecting'))

    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    shellWs = new WebSocket(`${proto}://${location.host}/ws/terminal/${sid}?token=${encodeURIComponent(auth.state.token)}`)

    let firstData = false
    const connectTimer = setTimeout(() => {
      if (!firstData && shellWs && shellWs.readyState === WebSocket.OPEN) {
        if (term) term.writeln('\r\n' + t('shell.connectionTimeout'))
        disconnect()
      }
    }, 30000)

    shellWs.onopen = () => {
      term.writeln(t('shell.websocketEstablished'))
      term.focus()
      shellWs.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }
    shellWs.onmessage = (e) => {
      if (!firstData) {
        firstData = true; clearTimeout(connectTimer); term.clear()
        const srv = servers.value.find(x => x.id === selectedServer.value)
        if (srv) term.writeln(t('shell.sshConnected', { host: srv.host, port: srv.port || 22 }))
      }
      if (e.data instanceof Blob) e.data.text().then(d => { if (term) term.write(d) })
      else if (term) term.write(e.data)
    }
    shellWs.onclose = () => { clearTimeout(connectTimer); if (term) term.writeln('\r\n' + t('shell.disconnected')); shellWs = null }
    shellWs.onerror = () => { clearTimeout(connectTimer); if (term) term.writeln('\r\n' + t('shell.connectionFailed')) }

    term.onData(data => {
      if (shellWs && shellWs.readyState === WebSocket.OPEN) shellWs.send(data)
    })
    term.onResize(({ cols, rows }) => {
      if (shellWs && shellWs.readyState === WebSocket.OPEN) {
        shellWs.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })
  } catch (e) {
    console.error('WebShell connect failed:', e)
    toast(t('shell.connectionFailed') + ': ' + (e.message || t('common.unknown')), false)
  }
}

function disconnect() {
  if (shellWs) { shellWs.close(); shellWs = null }
}

function onFileChange(e) {
  selectedFile.value = e.target.files[0]
}

function triggerFilePicker() {
  fileInput.value?.click()
}

async function upload() {
  const sid = selectedServer.value
  if (!sid || sid === 0) return toast(t('shell.selectServerFirst'), false)
  if (!selectedFile.value) return toast(t('shell.selectFile'), false)

  const form = new FormData()
  form.append('file', selectedFile.value)
  form.append('path', scpPath.value || '/tmp')

  try {
    const r = await fetch(`/api/upload/${sid}`, { method: 'POST', headers: auth.A(), body: form })
    const d = await r.json()
    if (d.success) {
      toast(t('shell.uploadSuccess', { path: d.data?.path || '' }), true)
    } else {
      showError(d)
    }
  } catch (e) {
    toast(t('shell.uploadFailed'), false)
  }
}

onMounted(() => {
  loadXtermCSS()
  loadXtermJS().then(() => {
    xtermLoaded = true
  }).catch(e => {
    console.error('xterm.js load failed:', e)
  })
  loadServers()
})

onUnmounted(() => {
  disconnect()
  if (term) { term.dispose(); term = null }
})
</script>

<style scoped>
.file-name {
  font-size: 12px;
  color: #999;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
