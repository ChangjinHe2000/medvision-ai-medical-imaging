<template>
  <div class="page-shell">
    <header class="page-header">
      <div><h2>用户与权限</h2><p>管理员创建账号并控制账号状态；密码不会在浏览器中保存。</p></div>
      <el-button :loading="loading" @click="loadUsers">刷新</el-button>
    </header>

    <el-card class="section-card create-card">
      <template #header><div class="section-header"><strong>创建内部账号</strong><span>密码至少 12 个字符</span></div></template>
      <el-form :model="form" label-position="top" @submit.prevent="submitUser">
        <div class="form-grid">
          <el-form-item label="用户名"><el-input v-model.trim="form.username" maxlength="64" /></el-form-item>
          <el-form-item label="显示名称"><el-input v-model.trim="form.display_name" maxlength="80" /></el-form-item>
          <el-form-item label="角色">
            <el-select v-model="form.role" style="width:100%"><el-option label="医生" value="doctor" /><el-option label="只读用户" value="viewer" /><el-option label="管理员" value="admin" /></el-select>
          </el-form-item>
          <el-form-item label="初始密码"><el-input v-model="form.password" type="password" show-password maxlength="256" /></el-form-item>
        </div>
        <el-button type="primary" native-type="submit" :loading="creating">创建账号</el-button>
      </el-form>
    </el-card>

    <el-card class="section-card list-card">
      <template #header><div class="section-header"><strong>账号列表</strong><span>共 {{ users.length }} 个账号</span></div></template>
      <el-table :data="users" v-loading="loading">
        <el-table-column prop="username" label="用户名" min-width="150" />
        <el-table-column prop="display_name" label="显示名称" min-width="160" />
        <el-table-column label="角色" width="120"><template #default="{ row }"><el-tag>{{ roleText(row.role) }}</el-tag></template></el-table-column>
        <el-table-column prop="organization_id" label="组织" min-width="130"><template #default="{ row }">{{ row.organization_id || '默认组织' }}</template></el-table-column>
        <el-table-column prop="created_at" label="创建时间" min-width="190"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
        <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '启用' : '停用' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="120" fixed="right"><template #default="{ row }"><el-switch :model-value="row.is_active" :disabled="row.id === authStore.state.user?.id" @change="value => changeStatus(row, value)" /></template></el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createUser, listUsers, setUserActive } from '../api/authApi.js'
import { apiErrorMessage } from '../api/errors.js'
import { authStore } from '../store/authStore.js'

const users = ref([])
const loading = ref(false)
const creating = ref(false)
const form = reactive({ username: '', display_name: '', password: '', role: 'doctor' })
const roleText = role => ({ admin: '管理员', doctor: '医生', viewer: '只读用户' }[role] || role)
const formatTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-'

async function loadUsers() {
  loading.value = true
  try { users.value = (await listUsers()).users || [] } catch (error) { ElMessage.error(apiErrorMessage(error, '用户列表加载失败')) } finally { loading.value = false }
}

async function submitUser() {
  if (!form.username || !form.display_name || form.password.length < 12) return ElMessage.warning('请填写完整信息，密码至少 12 个字符')
  creating.value = true
  try {
    await createUser({ ...form })
    Object.assign(form, { username: '', display_name: '', password: '', role: 'doctor' })
    ElMessage.success('账号已创建')
    await loadUsers()
  } catch (error) { ElMessage.error(apiErrorMessage(error, '创建账号失败')) } finally { creating.value = false }
}

async function changeStatus(row, active) {
  try { await setUserActive(row.id, active); row.is_active = active; ElMessage.success(active ? '账号已启用' : '账号已停用') }
  catch (error) { ElMessage.error(apiErrorMessage(error, '修改状态失败')) }
}

onMounted(loadUsers)
</script>

<style scoped>
.create-card { margin-bottom: 18px; }
.form-grid { display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 14px; }
.section-header span { color: var(--ppgl-muted); font-size: 13px; }
@media (max-width: 1100px) { .form-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 680px) { .form-grid { grid-template-columns: 1fr; } }
</style>
