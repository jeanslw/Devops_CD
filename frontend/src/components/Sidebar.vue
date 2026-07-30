<template>
  <div class="sidebar">
    <!-- 仪表盘：所有角色可见 -->
    <div class="item" :class="{ active: isActive('/') }" @click="$router.push('/')">
      {{ $t('sidebar.ciBuild') }}
    </div>

    <!-- 构建管理 -->
    <div v-if="auth.canBuildManage()" class="item" :class="{ active: isActive('/ci-build') }" @click="$router.push('/ci-build')">
      {{ $t('sidebar.ciBuildManage') }}
    </div>

    <!-- 部署管理 -->
    <template v-if="auth.canDeployManage()">
      <div class="item item-parent" @click="toggleDeploy()">
        {{ $t('sidebar.deployMgmt') }} {{ deployOpen ? '▾' : '▸' }}
      </div>
      <div v-if="auth.canDeploySingle()" v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/ssh') }" @click="$router.push('/deploy/ssh')">
        {{ $t('sidebar.sshDeploy') }}
      </div>
      <div v-if="auth.canDeployDocker()" v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/docker') }" @click="$router.push('/deploy/docker')">
        {{ $t('sidebar.dockerDeploy') }}
      </div>
      <div v-if="auth.canDeployK8s()" v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/k8s') }" @click="$router.push('/deploy/k8s')">
        {{ $t('sidebar.k8sDeploy') }}
      </div>
    </template>

    <!-- 服务器管理 -->
    <div v-if="auth.canServerManage()" class="item" :class="{ active: isActive('/servers') }" @click="$router.push('/servers')">
      {{ $t('sidebar.servers') }}
    </div>

    <!-- Web Shell -->
    <div v-if="auth.canWebshell()" class="item" :class="{ active: isActive('/shell') }" @click="$router.push('/shell')">
      {{ $t('sidebar.webShell') }}
    </div>

    <!-- 部署记录 -->
    <div v-if="auth.canDeployRecord()" class="item" :class="{ active: isActive('/logs') }" @click="$router.push('/logs')">
      {{ $t('sidebar.deployLogs') }}
    </div>

    <!-- 镜像仓库 -->
    <div v-if="auth.canImageRegistry()" class="item" :class="{ active: isActive('/registry') }" @click="$router.push('/registry')">
      {{ $t('sidebar.registry') }}
    </div>

    <!-- 资源监控 -->
    <template v-if="auth.canResourceMonitor()">
      <div class="item item-parent" @click="toggleMonitor()">
        {{ $t('sidebar.resourceMonitor') }} {{ monitorOpen ? '▾' : '▸' }}
      </div>
      <div v-if="auth.canMonitorApp()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/monitor/app') }" @click="$router.push('/monitor/app')">
        {{ $t('sidebar.appResources') }}
      </div>
      <div v-if="auth.canMonitorSystem()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/monitor/system') }" @click="$router.push('/monitor/system')">
        {{ $t('sidebar.systemResources') }}
      </div>
      <div v-if="auth.canMonitorCustom()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/custom-monitors') }" @click="$router.push('/custom-monitors')">
        {{ $t('sidebar.customMonitor') }}
      </div>
      <div v-if="auth.canMonitorAlert()" v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/alerts') }" @click="$router.push('/alerts')">
        {{ $t('sidebar.alertRules') }}
      </div>
    </template>

    <!-- 通知管理 -->
    <div v-if="auth.canNotificationManage()" class="item" :class="{ active: isActive('/bots') }" @click="$router.push('/bots')">
      {{ $t('sidebar.notifications') }}
    </div>
  </div>
</template>

<script setup>
import { ref, inject } from 'vue'
import { useRoute } from 'vue-router'

const auth = inject('auth')
const route = useRoute()
const deployOpen = ref(true)
const monitorOpen = ref(false)

function isActive(path) {
  return route.path === path
}

function toggleDeploy() {
  deployOpen.value = !deployOpen.value
}

function toggleMonitor() {
  monitorOpen.value = !monitorOpen.value
}
</script>
