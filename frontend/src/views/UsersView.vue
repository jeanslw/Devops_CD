<template>
  <div class="page">
    <div class="app-card">
      <div class="card-header">
        <h3>{{ $t('users.title') }}</h3>
        <button class="btn btn-primary" @click="$router.push('/users/create')">{{ $t('users.createUser') }}</button>
      </div>

      <table class="table" v-if="visibleUsers.length">
        <thead>
          <tr>
            <th>{{ $t('users.username') }}</th>
            <th>{{ $t('users.role') }}</th>
            <th>{{ $t('common.action') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in visibleUsers" :key="u.username">
            <td>{{ u.username }}</td>
            <td>
              <span v-if="u.role === 'super_admin'" class="badge badge-super">{{ $t('users.role_super_admin') }}</span>
              <select
                v-else
                class="role-select"
                :value="u.role"
                @change="e => changeRole(u, e.target.value)"
                :disabled="u.username === auth.state.user?.username"
              >
                <option v-if="isSuperAdmin" value="admin">{{ $t('users.role_admin') }}</option>
                <option value="deployer">{{ $t('users.role_deployer') }}</option>
                <option value="viewer">{{ $t('users.role_viewer') }}</option>
              </select>
            </td>
            <td>
              <button
                class="btn btn-xs btn-danger"
                v-if="canDelete(u)"
                @click="confirmDelete(u)"
              >{{ $t('common.delete') }}</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">{{ $t('common.noData') }}</div>
    </div>

    <!-- 修改密码弹窗 -->
    <div class="modal-overlay" v-if="showPwd" @click.self="showPwd = false">
      <div class="modal">
        <h4>{{ $t('users.changePasswordFor', { user: pwdTarget?.username }) }}</h4>
        <div v-if="isSelf" class="form-group">
          <label>{{ $t('users.oldPassword') }}</label>
          <input v-model="pwdForm.old_password" type="password" :placeholder="$t('users.oldPasswordPlaceholder')" />
        </div>
        <div class="form-group">
          <label>{{ $t('users.newPassword') }}</label>
          <input v-model="pwdForm.new_password" type="password" :placeholder="$t('users.passwordPlaceholder')" />
        </div>
        <div class="modal-actions">
          <button class="btn" @click="showPwd = false">{{ $t('common.cancel') }}</button>
          <button class="btn btn-primary" @click="doChangePwd">{{ $t('common.confirm') }}</button>
        </div>
      </div>
    </div>

    <!-- 删除确认 -->
    <div class="modal-overlay" v-if="showDelConfirm" @click.self="showDelConfirm = false">
      <div class="modal">
        <h4>{{ $t('users.confirmDelete', { user: delTarget?.username }) }}</h4>
        <div class="modal-actions">
          <button class="btn" @click="showDelConfirm = false">{{ $t('common.cancel') }}</button>
          <button class="btn btn-danger" @click="doDelete">{{ $t('common.delete') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { useToast } from '@/composables/useToast'
import { useError } from '@/composables/useError'

const auth = inject('auth')
const { t } = useI18n()
const { toast } = useToast()
const { showError } = useError()

const users = ref([])
const showPwd = ref(false)
const showDelConfirm = ref(false)
const pwdTarget = ref(null)
const delTarget = ref(null)

// 只显示部署者和只读账号
const visibleUsers = computed(() => users.value.filter(u => u.role === 'deployer' || u.role === 'viewer'))

// 当前用户是否是 super_admin
const isSuperAdmin = computed(() => auth.isSuperAdmin())
// 当前用户是否是 admin（不含 super_admin）
const isAdmin = computed(() => auth.isAdmin() && !auth.isSuperAdmin())

// 是否可以删除该用户：只能删除下级
function canDelete(u) {
  if (u.username === auth.state.user?.username) return false
  if (isSuperAdmin.value) return true
  if (isAdmin.value && (u.role === 'deployer' || u.role === 'viewer')) return true
  return false
}

const pwdForm = ref({ old_password: '', new_password: '' })

const isSelf = computed(() => pwdTarget.value?.username === auth.state.user?.username)

async function loadUsers() {
  try {
    const r = await fetch('/api/users', { headers: auth.A() })
    if (auth.handle401(r)) return
    users.value = await r.json()
  } catch { }
}

function openChangePwd(u) {
  pwdTarget.value = u
  pwdForm.value = { old_password: '', new_password: '' }
  showPwd.value = true
}

async function doChangePwd() {
  if (!pwdForm.value.new_password) {
    toast(t('users.fillPassword'))
    return
  }
  const r = await fetch(`/api/users/${pwdTarget.value.username}/password`, {
    method: 'PUT',
    headers: { ...auth.A(), 'Content-Type': 'application/json' },
    body: JSON.stringify(pwdForm.value),
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    toast(t('users.passwordChanged'))
    showPwd.value = false
    if (isSelf.value) auth.fetchMe()
  } else {
    await showError(r)
  }
}

function confirmDelete(u) {
  delTarget.value = u
  showDelConfirm.value = true
}

async function doDelete() {
  const r = await fetch(`/api/users/${delTarget.value.username}`, {
    method: 'DELETE',
    headers: auth.A(),
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    toast(t('users.deleted'))
    showDelConfirm.value = false
    loadUsers()
  } else {
    await showError(r)
  }
}

async function changeRole(user, newRole) {
  const r = await fetch(`/api/users/${user.username}/role`, {
    method: 'PUT',
    headers: { ...auth.A(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ role: newRole }),
  })
  if (auth.handle401(r)) return
  if (r.ok) {
    user.role = newRole
    toast(t('users.roleChanged'))
  } else {
    await showError(r)
  }
}

loadUsers()
</script>
