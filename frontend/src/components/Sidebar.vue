<template>
  <div class="sidebar">
    <!-- 所有角色可见 -->
    <div class="item" :class="{ active: isActive('/') }" @click="$router.push('/')">
      {{ $t('sidebar.ciBuild') }}
    </div>

    <!-- 部署管理：admin / deployer -->
    <template v-if="auth.canDeploy()">
      <div class="item item-parent" @click="toggleDeploy()">
        {{ $t('sidebar.deployMgmt') }} {{ deployOpen ? '▾' : '▸' }}
      </div>
      <div v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/ssh') }" @click="$router.push('/deploy/ssh')">
        {{ $t('sidebar.sshDeploy') }}
      </div>
      <div v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/docker') }" @click="$router.push('/deploy/docker')">
        {{ $t('sidebar.dockerDeploy') }}
      </div>
      <div v-show="deployOpen" class="item item-sub" :class="{ active: isActive('/deploy/k8s') }" @click="$router.push('/deploy/k8s')">
        {{ $t('sidebar.k8sDeploy') }}
      </div>
    </template>

    <!-- 服务器 / Web Shell：仅 admin -->
    <template v-if="auth.canManage()">
      <div class="item" :class="{ active: isActive('/servers') }" @click="$router.push('/servers')">
        {{ $t('sidebar.servers') }}
      </div>
      <div class="item" :class="{ active: isActive('/shell') }" @click="$router.push('/shell')">
        {{ $t('sidebar.webShell') }}
      </div>
    </template>

    <!-- 部署记录：所有角色可见 -->
    <div class="item" :class="{ active: isActive('/logs') }" @click="$router.push('/logs')">
      {{ $t('sidebar.deployLogs') }}
    </div>

    <!-- 镜像仓库：仅 admin -->
    <template v-if="auth.canManage()">
      <div class="item" :class="{ active: isActive('/registry') }" @click="$router.push('/registry')">
        {{ $t('sidebar.registry') }}
      </div>
    </template>

    <!-- 资源监控：所有角色可见 -->
    <div class="item item-parent" @click="toggleMonitor()">
      {{ $t('sidebar.resourceMonitor') }} {{ monitorOpen ? '▾' : '▸' }}
    </div>
    <div v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/monitor/app') }" @click="$router.push('/monitor/app')">
      {{ $t('sidebar.appResources') }}
    </div>
    <div v-show="monitorOpen" class="item item-sub" :class="{ active: isActive('/monitor/system') }" @click="$router.push('/monitor/system')">
      {{ $t('sidebar.systemResources') }}
    </div>
    <div v-show="monitorOpen && auth.canManage()" class="item item-sub" :class="{ active: isActive('/custom-monitors') }" @click="$router.push('/custom-monitors')">
      {{ $t('sidebar.customMonitor') }}
    </div>
    <div v-show="monitorOpen && auth.canManage()" class="item item-sub" :class="{ active: isActive('/alerts') }" @click="$router.push('/alerts')">
      {{ $t('sidebar.alertRules') }}
    </div>

    <!-- 通知管理：所有角色可见 -->
    <div class="item" :class="{ active: isActive('/bots') }" @click="$router.push('/bots')">
      {{ $t('sidebar.notifications') }}
    </div>

    <!-- 用户管理：仅 admin -->
    <div v-if="auth.isAdmin()" class="item" :class="{ active: isActive('/users') }" @click="$router.push('/users')">
      {{ $t('sidebar.userMgmt') }}
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
