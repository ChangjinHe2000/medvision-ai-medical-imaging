import request, { readCookie } from './request.js'
import { buildApiUrl, csrfCookieName } from '../config/runtime.js'

export const getLlmHealth = () => request.get('/v1/llm/health')

function errorFromPayload(status, payload) {
  const detail = payload?.detail
  const error = new Error(detail?.message || detail || `大模型接口返回 HTTP ${status}`)
  error.detail = typeof detail === 'object' ? detail : { code: `HTTP_${status}`, message: error.message }
  return error
}

export async function streamGeneralChat(payload, { signal, onDelta, onDone } = {}) {
  const csrf = readCookie(csrfCookieName)
  const response = await fetch(buildApiUrl('/v1/llm/chat/stream'), {
    method: 'POST',
    credentials: 'include',
    cache: 'no-store',
    signal,
    headers: {
      Accept: 'application/x-ndjson',
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRF-Token': csrf } : {})
    },
    body: JSON.stringify(payload)
  })
  if (!response.ok) {
    let body = null
    try { body = await response.json() } catch { body = null }
    throw errorFromPayload(response.status, body)
  }
  if (!response.body) throw new Error('浏览器没有获得可读取的流式响应')
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) {
      if (!line.trim()) continue
      const event = JSON.parse(line)
      if (event.type === 'delta') onDelta?.(event.text || '')
      if (event.type === 'done') onDone?.(event)
      if (event.type === 'error') throw new Error(event.detail || '大模型回答失败')
    }
  }
}
