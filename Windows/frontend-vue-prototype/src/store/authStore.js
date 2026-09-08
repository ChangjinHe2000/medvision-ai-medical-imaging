import { computed, reactive } from 'vue'
import * as authApi from '../api/authApi.js'
import { conversationStore } from './conversationStore.js'

const state = reactive({ user: null, initialized: false, loading: false })
let restorePromise = null

function clearSession({ clearConversations = false } = {}) {
  const userId = state.user?.id
  if (clearConversations && userId) conversationStore.clearAccount(userId)
  state.user = null
  state.initialized = true
}

async function ensureSession({ force = false } = {}) {
  if (state.initialized && !force) return state.user
  if (restorePromise) return restorePromise
  state.loading = true
  restorePromise = authApi.getCurrentUser()
    .then(payload => {
      state.user = payload.user
      state.initialized = true
      return state.user
    })
    .catch(() => { clearSession(); return null })
    .finally(() => { state.loading = false; restorePromise = null })
  return restorePromise
}

async function login(credentials) {
  state.loading = true
  try {
    const payload = await authApi.login(credentials)
    state.user = payload.user
    state.initialized = true
    return payload.user
  } finally { state.loading = false }
}

async function logout() {
  try { await authApi.logout() } finally { clearSession({ clearConversations: true }) }
}

globalThis.addEventListener?.('medvision:unauthorized', () => clearSession())

export const authStore = {
  state,
  user: computed(() => state.user),
  isAuthenticated: computed(() => Boolean(state.user)),
  isAdmin: computed(() => state.user?.role === 'admin'),
  ensureSession,
  login,
  logout,
  clearSession
}
