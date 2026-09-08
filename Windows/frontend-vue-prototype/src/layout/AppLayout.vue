<template>
  <el-container class="app-layout">
    <el-aside width="240px" class="sidebar">
      <div class="logo">
        <div class="logo-mark">M</div>
        <div>
          <div class="logo-title">MedVision AI</div>
          <div class="logo-subtitle">智能影像工作站</div>
        </div>
      </div>

      <el-menu
        :default-active="activeMenu"
        class="side-menu"
        background-color="transparent"
        text-color="#b8c3cf"
        active-text-color="#ffffff"
        @select="handleMenuSelect"
      >
        <el-menu-item index="/dashboard">
          <el-icon><DataBoard /></el-icon>
          <span>系统首页</span>
        </el-menu-item>

        <el-menu-item index="/cases">
          <el-icon><FolderOpened /></el-icon>
          <span>病例管理</span>
        </el-menu-item>

        <el-sub-menu v-if="authStore.state.user?.role !== 'viewer'" index="case-upload">
          <template #title>
            <el-icon><UploadFilled /></el-icon>
            <span>上传病例</span>
          </template>
          <el-menu-item index="/upload">上传 CT</el-menu-item>
          <el-menu-item index="/glioma/upload">上传 MRI</el-menu-item>
        </el-sub-menu>

        <el-menu-item index="/knowledge">
          <el-icon><Reading /></el-icon>
          <span>医学知识库</span>
        </el-menu-item>

        <el-menu-item index="/assistant">
          <el-icon><ChatDotRound /></el-icon>
          <span>AI 助手</span>
        </el-menu-item>

        <el-menu-item index="/messages">
          <el-icon><Message /></el-icon>
          <span>站内私聊</span>
          <el-badge :value="unreadTotal" :hidden="unreadTotal === 0" :max="99" class="menu-badge" />
        </el-menu-item>

        <el-menu-item v-if="authStore.isAdmin.value" index="/admin/users">
          <el-icon><UserFilled /></el-icon><span>用户与权限</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isAdmin.value" index="/admin/audit">
          <el-icon><DocumentChecked /></el-icon><span>操作审计</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isAdmin.value" index="/admin/trash">
          <el-icon><DeleteFilled /></el-icon><span>病例回收站</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isAdmin.value" index="/admin/knowledge">
          <el-icon><Collection /></el-icon><span>知识文档管理</span>
        </el-menu-item>

      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <div class="topbar-title">
          <strong>医学影像智能分割与辅助分析系统</strong>
          <span>PPGL CT · 脑肿瘤 MRI · 二维/三维阅片 · 医生复核</span>
        </div>
        <el-dropdown trigger="click" @command="handleUserCommand">
          <button type="button" class="user-info">
            <span class="status-dot"></span>
            <span>{{ authStore.state.user?.display_name || authStore.state.user?.username }}</span>
            <span class="role-label">{{ roleLabel }}</span>
            <el-icon><ArrowDown /></el-icon>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>{{ authStore.state.user?.username }}</el-dropdown-item>
              <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import {
  DataBoard,
  ArrowDown,
  ChatDotRound,
  Collection,
  DeleteFilled,
  DocumentChecked,
  FolderOpened,
  Message,
  Reading,
  UploadFilled,
  UserFilled
} from '@element-plus/icons-vue'
import { getUnreadCount } from '../api/messagingApi.js'
import { authStore } from '../store/authStore.js'

const route = useRoute()
const router = useRouter()
const unreadTotal = ref(0)
let unreadTimer = null
const roleLabel = computed(() => ({ admin: '管理员', doctor: '医生', viewer: '只读' }[authStore.state.user?.role] || ''))

const activeMenu = computed(() => {
  if (/^\/glioma\/cases\/[^/]+$/.test(route.path)) return '/cases'
  if (route.path.includes('/cases/')) return '/cases'
  return route.path
})

function handleMenuSelect(index) {
  router.push(index)
}

async function refreshUnread() {
  try { unreadTotal.value = Number((await getUnreadCount()).unread_count || 0) } catch { /* 全局处理登录失效 */ }
}

async function handleUserCommand(command) {
  if (command !== 'logout') return
  await authStore.logout()
  ElMessage.success('已安全退出')
  await router.replace('/login')
}

function handleMessagesUpdated(event) {
  unreadTotal.value = Number(event.detail?.unreadTotal || 0)
}

onMounted(() => {
  refreshUnread()
  unreadTimer = setInterval(refreshUnread, 10000)
  globalThis.addEventListener?.('medvision:messages-updated', handleMessagesUpdated)
})
onUnmounted(() => {
  clearInterval(unreadTimer)
  globalThis.removeEventListener?.('medvision:messages-updated', handleMessagesUpdated)
})
</script>

<style scoped>
.app-layout {
  height: 100vh;
  background: var(--ppgl-bg);
}

.sidebar {
  background: linear-gradient(180deg, #081522 0%, #07111d 100%);
  color: #ffffff;
  border-right: 1px solid rgba(148, 163, 184, 0.13);
  position: relative;
  overflow: hidden;
}

.logo {
  height: 78px;
  padding: 16px 18px;
  box-sizing: border-box;
  border-bottom: 1px solid rgba(148, 163, 184, 0.13);
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo-mark {
  width: 38px;
  height: 38px;
  border-radius: 8px;
  background: #0f766e;
  color: #ffffff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 800;
}

.logo-title {
  font-size: 21px;
  font-weight: 800;
  line-height: 1;
}

.logo-subtitle {
  font-size: 13px;
  color: var(--ppgl-muted);
  margin-top: 6px;
}

.side-menu {
  border-right: none;
  padding: 12px 10px;
}

.side-menu :deep(.el-menu-item) {
  height: 44px;
  margin-bottom: 2px;
  border-radius: 8px;
  font-weight: 650;
  border: 0;
  position: relative;
  z-index: 1;
}

.side-menu :deep(.el-menu-item:hover) {
  background: rgba(148, 163, 184, 0.08);
}

.side-menu :deep(.el-menu-item.is-active) {
  background: rgba(32, 224, 196, 0.1);
  box-shadow: inset 3px 0 0 var(--ppgl-primary);
}

.topbar {
  height: 62px;
  background: rgba(7, 18, 30, 0.94);
  border-bottom: 1px solid rgba(148, 163, 184, 0.13);
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--ppgl-text);
  backdrop-filter: blur(18px);
}

.topbar-title {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.topbar-title strong {
  font-size: 16px;
  font-weight: 800;
  color: var(--ppgl-text);
}

.topbar-title span {
  color: var(--ppgl-muted);
  font-size: 12px;
}

.user-info {
  height: 34px;
  padding: 0 12px;
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.07);
  color: #b9f8f1;
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 650;
  cursor: pointer;
  font-family: inherit;
}

.role-label { color: var(--ppgl-muted); font-size: 12px; }
.menu-badge { margin-left: auto; }

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #10b981;
  box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.1);
}

.main-content {
  background:
    linear-gradient(90deg, rgba(148, 163, 184, 0.022) 1px, transparent 1px),
    linear-gradient(180deg, rgba(148, 163, 184, 0.018) 1px, transparent 1px),
    var(--ppgl-bg);
  background-size: 44px 44px, 44px 44px, auto;
  padding: 24px;
  overflow: auto;
  position: relative;
}

@media (max-width: 900px) {
  .sidebar {
    width: 76px !important;
  }

  .logo {
    justify-content: center;
    padding: 16px 10px;
  }

  .logo > div:last-child,
  .side-menu :deep(.el-menu-item span) {
    display: none;
  }

  .side-menu {
    padding: 10px;
  }

  .side-menu :deep(.el-menu-item) {
    justify-content: center;
    padding: 0;
  }

  .topbar-title span {
    display: none;
  }

  .main-content {
    padding: 16px;
  }

}
</style>
