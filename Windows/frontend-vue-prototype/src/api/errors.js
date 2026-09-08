export function apiErrorMessage(error, fallback = '请求失败') {
  if (error?.name === 'AbortError' || error?.code === 'ERR_CANCELED') return '请求已停止'
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail?.message) return detail.message
  if (error?.detail?.message) return error.detail.message
  if (error?.response?.data?.message) return error.response.data.message
  if (error?.code === 'ECONNABORTED') return '请求超时，请稍后重试'
  if (error?.message === 'Network Error') return '无法连接后端服务，请确认 FastAPI 已启动'
  return error?.message || fallback
}

export function apiErrorCode(error) {
  return error?.response?.data?.detail?.code || error?.detail?.code || error?.code || ''
}
