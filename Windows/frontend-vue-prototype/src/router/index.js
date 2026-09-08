import { createRouter, createWebHistory } from 'vue-router'

import Login from '../views/Login.vue'
import AppLayout from '../layout/AppLayout.vue'

const Dashboard = () => import('../views/Dashboard.vue')
const CaseList = () => import('../views/CaseList.vue')
const UploadCase = () => import('../views/UploadCase.vue')
const CaseDetail = () => import('../views/CaseDetail.vue')
const ThreeDViewer = () => import('../views/ThreeDViewer.vue')
const ReportView = () => import('../views/ReportView.vue')
const GliomaUploadCase = () => import('../views/GliomaUploadCase.vue')
const GliomaCaseDetail = () => import('../views/GliomaCaseDetail.vue')
const KnowledgeBaseView = () => import('../views/KnowledgeBaseView.vue')

const routes = [
  {
    path: '/',
    redirect: '/login'
  },
  {
    path: '/login',
    component: Login,
    meta: { guestOnly: true }
  },
  {
    path: '/',
    component: AppLayout,
    meta: { requiresAuth: true },
    children: [
      {
        path: 'dashboard',
        component: Dashboard
      },
      {
        path: 'cases',
        component: CaseList
      },
      {
        path: 'upload',
        component: UploadCase,
        meta: { requiresEditor: true }
      },
      {
        path: 'glioma/cases',
        redirect: { path: '/cases', query: { type: 'mri' } }
      },
      {
        path: 'glioma/upload',
        component: GliomaUploadCase,
        meta: { requiresEditor: true }
      },
      {
        path: 'glioma/cases/:caseId',
        component: GliomaCaseDetail
      },
      {
        path: 'knowledge',
        component: KnowledgeBaseView
      },
      {
        path: 'assistant',
        component: () => import('../views/AiAssistant.vue')
      },
      {
        path: 'messages',
        component: () => import('../views/DirectMessages.vue')
      },
      {
        path: 'admin/users',
        component: () => import('../views/UserManagement.vue'),
        meta: { requiresAdmin: true }
      },
      {
        path: 'admin/audit',
        component: () => import('../views/AuditLogs.vue'),
        meta: { requiresAdmin: true }
      },
      {
        path: 'admin/trash',
        component: () => import('../views/TrashCases.vue'),
        meta: { requiresAdmin: true }
      },
      {
        path: 'admin/knowledge',
        component: () => import('../views/KnowledgeAdmin.vue'),
        meta: { requiresAdmin: true }
      },
      {
        path: 'cases/:caseId/2d',
        redirect: route => `/cases/${route.params.caseId}/3d`
      },
      {
        path: 'cases/:caseId/3d',
        component: ThreeDViewer
      },
      {
        path: 'cases/:caseId/report',
        component: ReportView
      },
      {
        path: 'cases/:caseId',
        component: CaseDetail
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(async to => {
  const { authStore } = await import('../store/authStore.js')
  const user = await authStore.ensureSession()
  if (to.meta.requiresAuth && !user) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.meta.requiresAdmin && user?.role !== 'admin') return '/dashboard'
  if (to.meta.requiresEditor && user?.role === 'viewer') return '/dashboard'
  if (to.meta.guestOnly && user) return '/dashboard'
  return true
})

export default router
