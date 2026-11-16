import { computed, ref } from 'vue'
import { getGpuWorkbench } from '../api/gpuWorkbenchApi'

const EMPTY_STATUS = {
  gpu: {},
  models: [],
  activity: {},
  runtime_activities: [],
  active_tasks: [],
  recent_tasks: [],
  summary: {},
  telemetry: { samples: [] },
  capacity_policy: {}
}

const status = ref(EMPTY_STATUS)
const isRefreshing = ref(false)
const lastError = ref('')
const lastSuccessfulAt = ref('')
const nextRefreshAt = ref(0)
const clock = ref(Date.now())

let pollTimer = null
let clockTimer = null
let visibilityHandler = null
let pollingStarted = false
let pendingRefresh = null

const activeTaskCount = computed(() => Number(status.value.summary?.active || 0))
const hasLiveWork = computed(() => activeTaskCount.value > 0 || (status.value.runtime_activities || []).length > 0)
const nextRefreshSeconds = computed(() => {
  if (!nextRefreshAt.value) return 0
  return Math.max(0, Math.ceil((nextRefreshAt.value - clock.value) / 1000))
})

function refreshIntervalMs() {
  if (typeof document !== 'undefined' && document.hidden) return 30000
  return hasLiveWork.value ? 3000 : 10000
}

function clearScheduledRefresh() {
  if (pollTimer) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
  nextRefreshAt.value = 0
}

function scheduleNextRefresh() {
  if (!pollingStarted || typeof window === 'undefined') return
  clearScheduledRefresh()
  const delay = refreshIntervalMs()
  nextRefreshAt.value = Date.now() + delay
  pollTimer = window.setTimeout(async () => {
    try {
      await refresh()
    } catch {
      // Keep the global monitor alive after a temporary gateway or GPU probe failure.
    } finally {
      scheduleNextRefresh()
    }
  }, delay)
}

function refresh() {
  if (pendingRefresh) return pendingRefresh
  isRefreshing.value = true
  pendingRefresh = Promise.resolve()
    .then(() => getGpuWorkbench())
    .then(payload => {
      status.value = payload && typeof payload === 'object' ? payload : EMPTY_STATUS
      lastError.value = ''
      lastSuccessfulAt.value = new Date().toISOString()
      return status.value
    })
    .catch(error => {
      lastError.value = error?.response?.data?.detail || error?.response?.data?.message || 'GPU 状态暂时不可用'
      throw error
    })
    .finally(() => {
      pendingRefresh = null
      isRefreshing.value = false
    })
  return pendingRefresh
}

async function refreshNow() {
  try {
    return await refresh()
  } finally {
    if (pollingStarted) scheduleNextRefresh()
  }
}

function startPolling() {
  if (pollingStarted || typeof window === 'undefined') return
  pollingStarted = true
  clock.value = Date.now()
  clockTimer = window.setInterval(() => {
    clock.value = Date.now()
  }, 1000)
  visibilityHandler = () => {
    if (!document.hidden) {
      refresh().catch(() => undefined).finally(scheduleNextRefresh)
      return
    }
    scheduleNextRefresh()
  }
  document.addEventListener('visibilitychange', visibilityHandler)
  refresh().catch(() => undefined).finally(scheduleNextRefresh)
}

function stopPolling() {
  pollingStarted = false
  clearScheduledRefresh()
  if (clockTimer) {
    window.clearInterval(clockTimer)
    clockTimer = null
  }
  if (visibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler)
  }
  visibilityHandler = null
}

export function useGpuRuntimeStatus() {
  return {
    status,
    isRefreshing,
    lastError,
    lastSuccessfulAt,
    nextRefreshSeconds,
    activeTaskCount,
    hasLiveWork,
    refreshNow,
    startPolling,
    stopPolling
  }
}
