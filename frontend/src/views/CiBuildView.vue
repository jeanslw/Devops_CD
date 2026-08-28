<template>
  <div class="card">
    <h3>{{ $t('ciBuild.title') }}</h3>

    <!-- 项目选择 + 触发按钮 -->
    <div class="ci-build-toolbar">
      <select v-model="selectedProject" @change="onProjectChange" class="project-select">
        <option value="">{{ $t('ciBuild.selectProject') }}</option>
        <option v-for="p in projects" :key="p.job_name" :value="p.job_name">
          {{ p.job_name }}
        </option>
      </select>
      <button v-if="auth.canTriggerBuild() && !isCustomPush" class="btn btn-green" :disabled="!selectedProject" @click="showTrigger = true">
        {{ $t('ciBuild.triggerBuild') }}
      </button>
      <button class="btn btn-sm" @click="refreshAll" :disabled="loading || buildsLoading">
        {{ $t('ciBuild.refresh') }}
      </button>
    </div>

    <!-- 错误提示 -->
    <div v-if="error" class="error-banner">{{ error }}</div>

    <!-- 构建历史列表（带分页）-->
    <div v-if="selectedProject">
      <div v-if="buildsLoading" class="loading-text">{{ $t('common.loading') }}</div>
      <template v-else>
        <table v-if="builds.length">
          <thead>
            <tr>
              <th>{{ $t('ciBuild.pipelineId') }}</th>
              <th>{{ $t('ciBuild.ref') }}</th>
              <th>{{ $t('ciBuild.status') }}</th>
              <th>{{ $t('ciBuild.created') }}</th>
              <th>{{ $t('common.action') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in pagedBuilds" :key="b.id">
              <td><strong>#{{ b.id || b.iid }}</strong></td>
              <td>{{ b.ref || b.branch || '—' }}</td>
              <td><span class="badge" :class="statusClass(b.status)">{{ b.status }}</span></td>
              <td class="time-cell">{{ formatTime(b.updated_at) }}</td>
              <td class="action-cell">
                <!-- custom_push：log_url 非必报，有则点链接，空则显示「日志无」 -->
                <template v-if="buildProvider === 'custom_push'">
                  <a v-if="b.log_url" :href="b.log_url" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-blue">{{ $t('ciBuild.viewLog') }}</a>
                  <span v-else class="no-log">{{ $t('ciBuild.logEmpty') }}</span>
                </template>
                <button v-else class="btn btn-sm btn-blue" @click="viewLog(b)">{{ $t('ciBuild.viewLog') }}</button>
                <template v-if="buildProvider === 'gitlab_ci' && auth.canTriggerBuild()">
                  <button class="btn btn-sm" @click="retryBuild(b)" :disabled="actionLoading === b.id">
                    🔄 {{ $t('ciBuild.retry') }}
                  </button>
                  <button class="btn btn-sm btn-red" @click="cancelBuild(b)" :disabled="actionLoading === b.id">
                    ⏹ {{ $t('ciBuild.cancel') }}
                  </button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-hint">{{ $t('ciBuild.noBuilds') }}</div>

        <!-- 分页 -->
        <div v-if="builds.length > pageSize" class="pagination">
          <span class="page-info">
            {{ $t('ciBuild.pageInfo', { page: currentPage, total: totalPages, count: builds.length }) }}
          </span>
          <button class="btn btn-sm" :disabled="currentPage <= 1" @click="currentPage--">‹</button>
          <button class="btn btn-sm" :disabled="currentPage >= totalPages" @click="currentPage++">›</button>
        </div>
      </template>
    </div>
    <div v-else class="empty-hint">{{ $t('ciBuild.hint') }}</div>

    <!-- 触发构建弹窗 -->
    <div v-if="showTrigger" class="modal-overlay" @click.self="showTrigger = false">
      <div class="modal">
        <h4>{{ $t('ciBuild.triggerTitle') }}</h4>
        <!-- 顶部"分支"选择器：仅 GitLab CI 显示（Jenkins 的 ref 不生效，分支由参数中的 git 类型参数指定） -->
        <div v-if="selectedProjectProvider !== 'jenkins'" class="form-group">
          <label>{{ $t('ciBuild.branch') }} <span class="required">*</span></label>
          <select v-model="triggerRef" class="field">
            <option value="">{{ branches.length ? $t('ciBuild.pleaseSelect') : $t('ciBuild.loadingBranch') }}</option>
            <option v-for="b in branches" :key="b" :value="b">{{ b }}</option>
          </select>
        </div>
        <div class="form-group">
          <label>{{ $t('ciBuild.parameters') }} <span class="required">*</span></label>
          <div v-if="loadingParams" class="loading-text-sm">{{ $t('common.loading') }}</div>
          <template v-else>
            <div v-if="triggerVars.length === 0" class="hint">{{ $t('ciBuild.noParams') }}</div>
            <div v-for="(p, i) in triggerVars" :key="i" class="var-row">
              <span class="param-name-col">
                <code class="param-key">{{ p.key }}</code>
                <span v-if="p.type" class="param-type-tag">{{ p.type }}</span>
                <span v-if="p.description" class="param-desc" :title="p.description">{{ p.description }}</span>
              </span>
              <!-- select → 下拉框（有 choices） -->
              <select v-if="p.type === 'select' && p.choices?.length" v-model="p.value" class="field" style="flex:2">
                <option value="">{{ $t('ciBuild.pleaseSelect') }}</option>
                <option v-for="c in p.choices" :key="c" :value="c">{{ c }}</option>
              </select>
              <!-- git → 分支下拉 -->
              <select v-else-if="p.type === 'git'" v-model="p.value" class="field" style="flex:2">
                <option value="">{{ $t('ciBuild.pleaseSelect') }}</option>
                <option v-for="b in branches" :key="b" :value="b">{{ b }}</option>
              </select>
              <!-- boolean → 复选框 -->
              <label v-else-if="p.type === 'boolean'" class="field checkbox-label" style="flex:2">
                <input type="checkbox" v-model="p.value" true-value="true" false-value="false" />
                {{ p.value === 'true' ? 'true' : 'false' }}
              </label>
              <!-- text → 多行文本 -->
              <textarea v-else-if="p.type === 'text'" v-model="p.value" rows="3"
                :placeholder="$t('ciBuild.pleaseSelect')" class="field" style="flex:2"></textarea>
              <!-- password → 密码输入 -->
              <input v-else-if="p.type === 'password'" type="password" v-model="p.value"
                :placeholder="$t('ciBuild.pleaseSelect')" class="field" style="flex:2" />
              <!-- dynamic → 文本输入 + 提示 -->
              <span v-else-if="p.type === 'dynamic'" style="flex:2; display:flex; flex-direction:column; gap:2px;">
                <input v-model="p.value" :placeholder="$t('ciBuild.pleaseSelect')" class="field" />
                <span class="dynamic-hint">{{ $t('ciBuild.dynamicHint') }}</span>
              </span>
              <!-- string / 默认 → 单行输入 -->
              <input v-else v-model="p.value" :placeholder="$t('ciBuild.pleaseSelect')" class="field" style="flex:2" />
              <button class="btn btn-sm btn-red" @click="triggerVars.splice(i, 1)" :title="$t('ciBuild.removeParam')">×</button>
            </div>
          </template>
        </div>
        <div class="modal-actions">
          <button class="btn" @click="showTrigger = false">{{ $t('common.cancel') }}</button>
          <button class="btn btn-green" @click="doTrigger" :disabled="triggering">
            {{ triggering ? $t('ciBuild.triggering') : $t('ciBuild.confirmTrigger') }}
          </button>
        </div>
      </div>
    </div>

    <!-- 构建日志弹窗 -->
    <div v-if="showLog" class="modal-overlay" @click.self="showLog = false">
      <div class="modal modal-wide">
        <h4>
          {{ $t('ciBuild.buildLog') }} #{{ logBuildId }}
          <button class="btn btn-sm" @click="refreshLog" style="margin-left:8px">🔄</button>
        </h4>
        <pre class="log-viewer" v-text="logContent || $t('ciBuild.logEmpty')"></pre>
        <div class="modal-actions">
          <button class="btn" @click="showLog = false">{{ $t('common.close') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { useAuth } from '@/composables/useAuth'
import { useToast } from '@/composables/useToast'
import { useI18n } from 'vue-i18n'

const auth = useAuth()
const { toast } = useToast()
const { t } = useI18n()

const projects = ref([])
const selectedProject = ref('')
const loading = ref(false)
const error = ref('')

// 构建历史
const builds = ref([])
const buildsLoading = ref(false)
const buildProvider = ref('')
const actionLoading = ref(null)  // 正在执行重试/取消的 build id
// 分页
const currentPage = ref(1)
const pageSize = ref(10)
const totalPages = computed(() => Math.max(1, Math.ceil(builds.value.length / pageSize.value)))
const pagedBuilds = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return builds.value.slice(start, start + pageSize.value)
})

// 触发构建
const showTrigger = ref(false)
const triggerRef = ref('')
const triggerVars = reactive([])
const triggering = ref(false)
const branches = ref([])
const loadingParams = ref(true)  // 初始为 true，避免弹窗首帧闪现"未定义参数"
// 当前项目的 CI 提供商（jenkins / gitlab_ci / custom_push）
const selectedProjectProvider = computed(() => {
  const p = projects.value.find(p => p.job_name === selectedProject.value)
  return p?.ci_provider || ''
})
// custom_push 项目完全以用户上报为准，CD 端只读、不触发
const isCustomPush = computed(() => selectedProjectProvider.value === 'custom_push')

// 构建日志
const showLog = ref(false)
const logBuildId = ref('')
const logContent = ref('')

// ── 加载项目列表 ──
async function loadProjects() {
  loading.value = true
  error.value = ''
  try {
    const r = await fetch('/api/ci/projects', { headers: auth.A() })
    if (auth.handle401(r)) return
    if (!r.ok) {
      const body = await r.text()
      // 503 表示未配置 CI，给出友好提示
      if (r.status === 503) {
        error.value = body
      } else {
        error.value = `[${r.status}] ${body}`
      }
      return
    }
    const data = await r.json()
    // CI 返回 [{job_name, ci_provider, project_id, current_path}]
    projects.value = data
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

// ── 刷新（项目列表 + 当前项目的构建历史）──
async function refreshAll() {
  await loadProjects()
  if (selectedProject.value) await onProjectChange()
}

// ── 切换项目 → 加载构建历史 ──
async function onProjectChange() {
  builds.value = []
  currentPage.value = 1
  if (!selectedProject.value) return
  buildsLoading.value = true
  error.value = ''
  try {
    const r = await fetch(`/api/ci/projects/${encodeURIComponent(selectedProject.value)}/builds`, {
      headers: auth.A()
    })
    if (auth.handle401(r)) return
    if (!r.ok) { error.value = `[${r.status}]`; return }
    const data = await r.json()
    // CI 返回 {build_provider, project_id, pipelines: [{id, iid, ref, status, created_at}]}
    buildProvider.value = data.build_provider || ''
    builds.value = data.pipelines || []
  } catch (e) {
    error.value = e.message
  } finally {
    buildsLoading.value = false
  }
}

// ── 打开触发弹窗时加载分支列表和参数 ──
watch(showTrigger, async (val) => {
  if (val && selectedProject.value) {
    triggerRef.value = ''
    triggerVars.splice(0, triggerVars.length)
    branches.value = []

    // 并行加载分支和参数（先标 loading，避免瞬时闪现"未定义参数"）
    loadingParams.value = true
    const proj = encodeURIComponent(selectedProject.value)
    const [branchR, paramR] = await Promise.allSettled([
      fetch(`/api/ci/projects/${proj}/branches`, { headers: auth.A() }),
      fetch(`/api/ci/projects/${proj}/variables`, { headers: auth.A() }),
    ])

    // 分支
    if (branchR.status === 'fulfilled' && branchR.value.ok) {
      branches.value = await branchR.value.json()
      // CI 返回 ["main", "master", ...]
    }

    // 参数（预设 CI 定义的变量，含 type/choices/defaultValue/description）
    if (paramR.status === 'fulfilled' && paramR.value.ok) {
      try {
        const data = await paramR.value.json()
        const vars = data.variables || data
        // boolean 类型默认值处理：校验不允许空，默认 false
        const initVal = (v, type) => type === 'boolean' ? (v.defaultValue ?? 'false') : (v.defaultValue ?? '')
        if (Array.isArray(vars)) {
          vars.forEach(v => {
            if (typeof v === 'object') {
              const t = v.type || 'string'
              triggerVars.push({
                key: v.key || v.name || '',
                value: initVal(v, t),
                type: t,
                choices: v.choices || [],
                description: v.description || ''
              })
            } else {
              triggerVars.push({ key: String(v), value: '', type: 'string', choices: [], description: '' })
            }
          })
        } else if (typeof vars === 'object' && vars !== null) {
          Object.entries(vars).forEach(([k, v]) => {
            if (k !== 'build_provider') {
              const t = v?.type || 'string'
              triggerVars.push({
                key: k,
                value: t === 'boolean' ? (v?.defaultValue ?? 'false') : (v?.defaultValue ?? (typeof v === 'string' ? v : '')),
                type: t,
                choices: v?.choices || [],
                description: v?.description || ''
              })
            }
          })
        }
      } catch (e) { /* ignore parse error */ }
    }
    loadingParams.value = false
  }
})

// ── 执行触发 ──
async function doTrigger() {
  // custom_push 项目不触发：以用户上报为准
  if (isCustomPush.value) return
  // GitLab CI 必须有 ref；Jenkins 不需要（分支由参数中的 git 参数指定）
  if (selectedProjectProvider.value === 'gitlab_ci' && !triggerRef.value) {
    toast(t('ciBuild.needBranch'), 'warn')
    return
  }
  // 所有参数必填
  const missing = triggerVars.filter(v => v.key && (v.value === '' || v.value === null || v.value === undefined))
  if (missing.length) {
    toast(`${t('ciBuild.needParams')}：${missing.map(v => v.key).join(', ')}`, 'warn')
    return
  }

  triggering.value = true
  try {
    // Jenkins 模式下不发送 ref（CI 那边不生效），只发 variables
    const body = {}
    if (selectedProjectProvider.value !== 'jenkins') {
      body.ref = triggerRef.value
    }
    const varsObj = {}
    triggerVars.forEach(v => { if (v.key) varsObj[v.key] = v.value })
    if (Object.keys(varsObj).length) body.variables = varsObj

    const r = await fetch(
      `/api/ci/projects/${encodeURIComponent(selectedProject.value)}/build`,
      { method: 'POST', headers: { ...auth.A(), 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    )
    if (auth.handle401(r)) return
    if (!r.ok) {
      const msg = await r.text()
      toast(`[${r.status}] ${msg}`, 'error')
      return
    }
    toast(t('ciBuild.triggerSuccess'), 'success')
    showTrigger.value = false
    onProjectChange()
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    triggering.value = false
  }
}

// ── 查看日志 ──
async function viewLog(b) {
  logBuildId.value = b.id || b.iid
  showLog.value = true
  logContent.value = ''
  await loadLog()
}

async function loadLog() {
  try {
    const r = await fetch(`/api/ci/projects/${encodeURIComponent(selectedProject.value)}/builds/${encodeURIComponent(logBuildId.value)}/log`, {
      headers: auth.A()
    })
    if (auth.handle401(r)) return
    logContent.value = await r.text()
  } catch (e) {
    logContent.value = `Error: ${e.message}`
  }
}

function refreshLog() { loadLog() }

// ── 重试 / 取消 Pipeline（仅 GitLab CI）──
async function retryBuild(b) {
  const id = b.id || b.iid
  actionLoading.value = id
  try {
    const r = await fetch(`/api/ci/projects/${encodeURIComponent(selectedProject.value)}/builds/${encodeURIComponent(id)}/retry`, {
      method: 'POST', headers: auth.A()
    })
    if (auth.handle401(r)) return
    if (!r.ok) { toast(`[${r.status}] ${await r.text()}`, 'error'); return }
    toast('✅ 已触发重试', 'success')
    onProjectChange()
  } catch (e) { toast(e.message, 'error') }
  finally { actionLoading.value = null }
}

async function cancelBuild(b) {
  const id = b.id || b.iid
  actionLoading.value = id
  try {
    const r = await fetch(`/api/ci/projects/${encodeURIComponent(selectedProject.value)}/builds/${encodeURIComponent(id)}/cancel`, {
      method: 'POST', headers: auth.A()
    })
    if (auth.handle401(r)) return
    if (!r.ok) { toast(`[${r.status}] ${await r.text()}`, 'error'); return }
    toast('⏹ 已取消', 'success')
    onProjectChange()
  } catch (e) { toast(e.message, 'error') }
  finally { actionLoading.value = null }
}

// ── 辅助 ──
function statusClass(status) {
  if (!status) return ''
  const s = status.toLowerCase()
  if (s === 'success' || s === 'passed') return 'badge-green'
  if (s === 'failed' || s === 'error') return 'badge-red'
  if (s === 'running' || s === 'pending') return 'badge-blue'
  return 'badge-gray'
}

function formatTime(t) {
  if (!t) return ''
  return t.replace('T', ' ').substring(0, 19)
}

onMounted(loadProjects)
</script>

<style scoped>
.ci-build-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.project-select {
  flex: 1;
  min-width: 200px;
  max-width: 400px;
  padding: 8px 12px;
  border: 1px solid #444;
  border-radius: 6px;
  background: #1a1a2e;
  color: #e0e0e0;
  font-size: 14px;
}
.error-banner {
  background: #3e1a1a;
  border: 1px solid #c62828;
  color: #ef9a9a;
  padding: 10px 14px;
  border-radius: 6px;
  margin-bottom: 12px;
  font-size: 13px;
  word-break: break-all;
}
.loading-text, .empty-hint {
  text-align: center;
  color: #888;
  padding: 40px 0;
  font-size: 14px;
}
.time-cell { font-size: 12px; color: #999; }
.action-cell { display: flex; gap: 4px; flex-wrap: wrap; }
.badge-green { background: #2e7d32; }
.badge-red { background: #c62828; }
.badge-blue { background: #1565c0; }
.badge-gray { background: #555; }

/* 弹窗 */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.modal {
  background: #1a1a2e; border: 1px solid #333; border-radius: 10px;
  padding: 24px; width: 480px; max-height: 80vh; overflow-y: auto;
}
.modal-wide { width: 720px; }
.form-group { margin-bottom: 14px; }
.form-group label { display: block; margin-bottom: 4px; font-size: 13px; color: #aaa; }
.required { color: #ef5350; margin-left: 2px; }
.var-row { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; }
.param-name-col {
  flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;
}
.param-key { font-size: 12px; color: #e0e0e0; background: #2a2a40; padding: 1px 6px; border-radius: 3px; }
.param-type-tag { font-size: 10px; color: #888; text-transform: uppercase; }
.param-desc { font-size: 10px; color: #777; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.loading-text-sm { font-size: 12px; color: #888; padding: 4px 0; }
.checkbox-label { display: flex; align-items: center; gap: 6px; padding: 8px 12px; cursor: pointer; min-height: 36px; }
.checkbox-label input[type="checkbox"] { width: 16px; height: 16px; cursor: pointer; accent-color: #388e3c; }
.dynamic-hint { font-size: 11px; color: #f0a030; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
textarea.field { resize: vertical; min-height: 50px; }
.log-viewer {
  background: #0d1117; color: #c9d1d9; padding: 14px; border-radius: 6px;
  max-height: 450px; overflow: auto; font-size: 12px; line-height: 1.5;
  white-space: pre-wrap; word-break: break-all; margin-top: 10px;
}

/* 按钮 */
.btn-green { background: #388e3c; color: #fff; }
.btn-red { background: #c62828; color: #fff; }
.btn-blue { background: #1565c0; color: #fff; }
.no-log { color: #888; font-size: 12px; }
.action-cell a.btn { text-decoration: none; }
</style>
