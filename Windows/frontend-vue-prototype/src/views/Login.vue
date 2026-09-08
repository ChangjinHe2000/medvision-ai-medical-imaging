<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-mark">MedVision AI</div>
      <h1>医学影像智能分割与辅助分析系统</h1>
      <p class="subtitle">
        统一病例 · 双模型工作流 · 企业级访问控制
      </p>

      <el-alert
        v-if="setupRequired"
        class="setup-alert"
        title="系统尚未创建管理员"
        description="请由部署负责人运行 scripts/windows/initialize_enterprise_database.py --create-admin 创建初始管理员，然后刷新本页。"
        type="warning"
        :closable="false"
        show-icon
      />

      <el-form v-if="!checkingSetup" class="login-form" @submit.prevent="handleSubmit">
        <el-form-item>
          <el-input v-model="username" placeholder="请输入账号" size="large" maxlength="32" />
        </el-form-item>

        <el-form-item>
          <el-input
            v-model="password"
            placeholder="请输入密码"
            type="password"
            size="large"
            show-password
          />
        </el-form-item>

        <el-button
          native-type="submit"
          type="primary"
          size="large"
          class="login-button"
          :loading="submitting"
          :disabled="setupRequired"
        >
          登录系统
        </el-button>
      </el-form>

      <div v-else class="checking-text">正在检查系统状态...</div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getSetupStatus } from '../api/authApi.js'
import { apiErrorMessage } from '../api/errors.js'
import { authStore } from '../store/authStore.js'

const router = useRouter()

const username = ref('')
const password = ref('')
const submitting = ref(false)
const checkingSetup = ref(true)
const setupRequired = ref(false)

async function loadSetupStatus() {
  checkingSetup.value = true
  try {
    const response = await getSetupStatus()
    setupRequired.value = Boolean(response.required)
  } catch (error) {
    setupRequired.value = false
    ElMessage.error(apiErrorMessage(error, '无法连接 FastAPI，请确认 8000 端口服务已经启动'))
  } finally {
    checkingSetup.value = false
  }
}

async function handleSubmit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning('请输入账号和密码')
    return
  }

  if (setupRequired.value) return ElMessage.warning('请先由部署负责人创建初始管理员')

  submitting.value = true
  try {
    await authStore.login({ username: username.value.trim(), password: password.value })
    const target = typeof router.currentRoute.value.query.redirect === 'string'
      ? router.currentRoute.value.query.redirect
      : '/dashboard'
    await router.replace(target)
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '账号或密码错误'))
  } finally {
    submitting.value = false
  }
}

onMounted(loadSetupStatus)
</script>

<style scoped>
.login-page {
  height: 100vh;
  background:
    linear-gradient(90deg, rgba(32, 224, 196, 0.055) 1px, transparent 1px),
    linear-gradient(180deg, rgba(106, 169, 255, 0.045) 1px, transparent 1px),
    linear-gradient(135deg, rgba(32, 224, 196, 0.12), transparent 36%),
    #0b1724;
  background-size: 42px 42px, 42px 42px, auto, auto;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.login-card {
  width: 460px;
  padding: 38px;
  background: rgba(8, 23, 36, 0.86);
  border: 1px solid rgba(93, 235, 219, 0.24);
  border-radius: 8px;
  box-shadow: 0 26px 70px rgba(0, 0, 0, 0.42), 0 0 32px rgba(32, 224, 196, 0.12);
  text-align: center;
  backdrop-filter: blur(18px);
}

.login-mark {
  width: fit-content;
  margin: 0 auto 18px;
  padding: 7px 12px;
  border-radius: 999px;
  background: rgba(32, 224, 196, 0.12);
  border: 1px solid rgba(93, 235, 219, 0.28);
  color: #b9f8f1;
  font-weight: 800;
}

.login-card h1 {
  font-size: 24px;
  margin-bottom: 8px;
  color: var(--ppgl-text);
}

.subtitle {
  color: var(--ppgl-muted);
  margin-bottom: 24px;
}

.setup-alert {
  margin-bottom: 20px;
  text-align: left;
}

.login-form {
  margin-top: 20px;
}

.login-button {
  width: 100%;
}

.checking-text {
  padding: 28px 0 12px;
  color: var(--ppgl-muted);
}
</style>
