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
    <template v-if="auth.canNotificationManage()">
      <div class="item item-parent" @click="toggleNotify()">
        {{ $t('sidebar.notifications') }} {{ notifyOpen ? '▾' : '▸' }}
      </div>
      <div v-show="notifyOpen" class="item item-sub" :class="{ active: isActive('/bots') }" @click="go('/bots')">
        {{ $t('sidebar.botManage') }}
      </div>
      <div v-show="notifyOpen" class="item item-sub" :class="{ active: isActive('/webhooks') }" @click="go('/webhooks')">
        {{ $t('sidebar.webhookReceiver') }}
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, inject, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

defineProps({
  open: { type: Boolean, default: true }
})
const emit = defineEmits(['close'])

const auth = inject('auth')
const route = useRoute()
const router = useRouter()
const deployOpen = ref(true)
const monitorOpen = ref(false)
const notifyOpen = ref(true)

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
