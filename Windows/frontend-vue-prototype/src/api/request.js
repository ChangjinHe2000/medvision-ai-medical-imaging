import axios from 'axios'
import { apiBaseUrl, csrfCookieName } from '../config/runtime.js'

const request = axios.create({
  baseURL: apiBaseUrl,
  timeout: 60000,
  withCredentials: true
})

export function readCookie(name) {
  if (typeof document === 'undefined') return ''
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find(value => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : ''
}

request.interceptors.request.use(config => {
  const method = String(config.method || 'get').toLowerCase()
  if (['post', 'put', 'patch', 'delete'].includes(method)) {
    const csrfToken = readCookie(csrfCookieName)
    if (csrfToken) config.headers['X-CSRF-Token'] = csrfToken
  }
  config.headers.Accept = config.headers.Accept || 'application/json'
  return config
})

request.interceptors.response.use(
  response => response.data,
  error => {
    const requestUrl = String(error.config?.url || '')
    if (error?.response?.status === 401 && !requestUrl.endsWith('/v1/auth/login')) {
      globalThis.dispatchEvent?.(new CustomEvent('medvision:unauthorized'))
    }
    return Promise.reject(error)
  }
)

export default request
