function normalizeBaseUrl(value) {
  const normalized = String(value || '/api').trim().replace(/\/+$/, '')
  return normalized === '/' ? '' : normalized
}

export const apiBaseUrl = normalizeBaseUrl(import.meta.env?.VITE_API_BASE_URL)
export const csrfCookieName = String(import.meta.env?.VITE_CSRF_COOKIE_NAME || 'medvision_csrf')

export function buildApiUrl(path) {
  const normalizedPath = String(path).startsWith('/') ? String(path) : `/${path}`
  return `${apiBaseUrl}${normalizedPath}`
}
