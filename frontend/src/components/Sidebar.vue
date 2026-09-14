<template>
  <div class="sidebar" :class="{ open }" :aria-expanded="open">
    <!-- 仪表盘：所有角色可见 -->
    <div class="item" :class="{ active: isActive('/') }" @click="go('/')">
      {{ $t('sidebar.ciBuild') }}
    </div>

    <!-- 权限未配置提示（非 super_admin 且 permissions 为空） -->
    <div v-if="showEmptyPermsHint" class="item item-hint">
      {{ $t('sidebar.noPermissions') }}
    </div>

    <!-- 构建管理 -->
    <div v-if="auth.canBuildManage()" class="item" :class="{ active: isActive('/ci-build') }" @click="go('/ci-build')">
      {{ $t('sidebar.ciBuildManage') }}
    </div>

    <!-- 部署管理 -->
    <template v-if="auth.canDeployManage()">
      <div class="item item-parent" @click="toggleDeploy()">
        {{ $t('sidebar.deployMgmt') }} {{ deployOpen ? '▾' : '▸' }}
      </div>
      <div v-if="auth.canDeploySingle()" v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/ssh') }" @click="go('/deploy/ssh')">
        {{ $t('sidebar.sshDeploy') }}
      </div>
      <div v-if="auth.canDeployDocker()" v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/docker') }" @click="go('/deploy/docker')">
        {{ $t('sidebar.dockerDeploy') }}
      </div>
      <div v-if="auth.canDeployK8s()" v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/k8s') }" @click="go('/deploy/k8s')">
        {{ $t('sidebar.k8sDeploy') }}
      </div>
    </template>

    <!-- 审批中心（审批人看「审批中心」；仅部署权限的申请人看「我的部署申请」） -->
    <div v-if="auth.canViewApprovals()" class="item" :class="{ active: isActive('/approvals') }" @click="go('/approvals')">
      {{ approvalMenuText }}
      <span
        v-if="badgeTotal > 0"
        class="badge-dot"
        :class="{ 'badge-dot--amber': toApprove === 0 }"
        :title="$t('approvals.badgeTip', { approve: toApprove, execute: toExecute })"
      >{{ badgeTotal > 99 ? '99+' : badgeTotal }}</span>
    </div>

    <!-- 服务器管理 -->
    <div v-if="auth.canServerManage()" class="item" :class="{ active: isActive('/servers') }" @click="go('/servers')">
      {{ $t('sidebar.servers') }}
    </div>

    <!-- Web Shell -->
    <div v-if="auth.canWebshell()" class="item" :class="{ active: isActive('/shell') }" @click="go('/shell')">
      {{ $t('sidebar.webShell') }}
    </div>

    <!-- 部署记录 -->
    <div v-if="auth.canDeployRecord()" class="item" :class="{ active: isActive('/logs') }" @click="go('/logs')">
      {{ $t('sidebar.deployLogs') }}
    </div>

    <!-- 镜像仓库 -->
    <div v-if="auth.canImageRegistry()" class="item" :class="{ active: isActive('/registry') }" @click="go('/registry')">
      {{ $t('sidebar.registry') }}
    </div>

    <!-- 资源监控 -->
    <template v-if="auth.canResourceMonitor()">
      <div class="item item-parent" @click="toggleMonitor()">
        {{ $t('sidebar.resourceMonitor') }} {{ monitorOpen ? '▾' : '▸' }}
      </div>
      <div v-if="auth.canMonitorApp()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/monitor/app') }" @click="go('/monitor/app')">
        {{ $t('sidebar.appResources') }}
      </div>
      <div v-if="auth.canMonitorSystem()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/monitor/system') }" @click="go('/monitor/system')">
        {{ $t('sidebar.systemResources') }}
      </div>
      <div v-if="auth.canMonitorCustom()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/custom-monitors') }" @click="go('/custom-monitors')">
        {{ $t('sidebar.customMonitor') }}
      </div>
      <div v-if="auth.canMonitorAlert()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/alerts') }" @click="go('/alerts')">
        {{ $t('sidebar.alertRules') }}
      </div>
    </template>

    <!-- 通知管理（折叠组：Bot 管理 + WebHook） -->
    <template v-if="auth.canNotificationManage() && (auth.canBot() || auth.canWebhook())">
      <div class="item item-parent" @click="toggleNotify()">
        {{ $t('sidebar.notifications') }} {{ notifyOpen ? '▾' : '▸' }}
      </div>
      <div v-if="auth.canBot()" v-show="notifyOpen" class="item item-sub" :class="{ active: isActive('/bots') }" @click="go('/bots')">
        {{ $t('sidebar.botManage') }}
      </div>
      <div v-if="auth.canWebhook()" v-show="notifyOpen" class="item item-sub" :class="{ active: isActive('/webhooks') }" @click="go('/webhooks')">
        {{ $t('sidebar.webhookReceiver') }}
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, inject, computed, watch, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

defineProps({
  open: { type: Boolean, default: true }
})
const emit = defineEmits(['close'])

const auth = inject('auth')
const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const deployOpen = ref(false)
const monitorOpen = ref(false)
const notifyOpen = ref(false)

// 审批菜单文案：具备审批权限 → 审批中心；否则（仅有部署权限的申请人）→ 我的部署申请
const approvalMenuText = computed(() =>
  auth.canApprove() ? t('sidebar.approvals') : t('sidebar.myApprovals')
)

// ── 审批菜单红点：待我审批（红）+ 我的待执行（琥珀），与 bot 通知互补的站内提醒 ──
const toApprove = ref(0)
const toExecute = ref(0)
const badgeTotal = computed(() => toApprove.value + toExecute.value)
let badgeTimer = null

async function loadBadge() {
  try {
    const r = await fetch('/api/approvals/badge', { headers: auth.A() })
    if (auth.handle401(r)) { stopBadgePolling(); return }
    const d = await r.json()
    toApprove.value = d.to_approve || 0
    toExecute.value = d.to_execute || 0
  } catch (e) {}
}

function startBadgePolling() {
  if (badgeTimer || !auth.canViewApprovals()) return
  loadBadge()
  badgeTimer = setInterval(loadBadge, 30000)
}

function stopBadgePolling() {
  if (badgeTimer) { clearInterval(badgeTimer); badgeTimer = null }
}

watch(() => auth.state?.user, (u) => {
  if (u && auth.canViewApprovals()) startBadgePolling()
  else { stopBadgePolling(); toApprove.value = 0; toExecute.value = 0 }
}, { immediate: true })

onUnmounted(stopBadgePolling)

// 用户已登录但未分配任何权限（非 super_admin）时显示提示
const showEmptyPermsHint = computed(() => {
  const u = auth.state?.user
  return u && u.role !== 'super_admin' && Array.isArray(u.permissions) && u.permissions.length === 0
})

function isActive(path) {
  return route.path === path
}

function go(path) {
  emit('close')
  router.push(path)
}

function toggleDeploy() {
  deployOpen.value = !deployOpen.value
}

function toggleMonitor() {
  monitorOpen.value = !monitorOpen.value
}

function toggleNotify() {
  notifyOpen.value = !notifyOpen.value
}
</script>

<style scoped>
/* 审批菜单红点：待审批（红）/ 仅待执行（琥珀），与 bot 通知互补的站内提醒 */
.badge-dot {
  display: inline-block;
  min-width: 16px;
  margin-left: auto;
  padding: 0 5px;
  font-size: 10px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
  border-radius: 8px;
  color: #fff;
  background: #e5484d;
  cursor: help;
  white-space: nowrap;
}

.badge-dot--amber {
  background: #f59e0b;
}
</style>
