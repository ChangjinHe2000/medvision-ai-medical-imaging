import request, { readCookie } from './request.js'
import { buildApiUrl, csrfCookieName } from '../config/runtime.js'

const path = value => encodeURIComponent(String(value))

export const getReport = caseId => request.get(`/v1/cases/${path(caseId)}/report`, { responseType: 'text' })
export const getReportJson = caseId => request.get(`/v1/cases/${path(caseId)}/report/json`)
export const generateReport = caseId => request.post(`/v1/cases/${path(caseId)}/report/generate`)
export const saveReportReview = (caseId, payload) => request.put(`/v1/cases/${path(caseId)}/review`, payload)
export const getReportFileUrl = (caseId, kind = 'markdown') =>
  buildApiUrl(`/v1/cases/${path(caseId)}/report/${encodeURIComponent(kind)}`)

function streamError(status, payload) {
  const detail = payload?.detail
  return new Error(detail?.message || detail || `病例助手返回 HTTP ${status}`)
}

export async function streamCaseAssistant(caseId, payload, { signal, onDelta, onDone } = {}) {
  const csrf = readCookie(csrfCookieName)
  const response = await fetch(buildApiUrl(`/v1/cases/${path(caseId)}/assistant/stream`), {
    method: 'POST', credentials: 'include', cache: 'no-store', signal,
    headers: {
      Accept: 'application/x-ndjson', 'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRF-Token': csrf } : {})
    },
    body: JSON.stringify(payload)
  })
  if (!response.ok) {
    let body = null
    try { body = await response.json() } catch { body = null }
    throw streamError(response.status, body)
  }
  if (!response.body) throw new Error('浏览器没有获得可读取的流式响应')
  const reader = response.body.getReader(), decoder = new TextDecoder('utf-8')
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n'); buffer = lines.pop() || ''
    for (const line of lines) {
      if (!line.trim()) continue
      const event = JSON.parse(line)
      if (event.type === 'delta') onDelta?.(event.text || '')
      if (event.type === 'done') onDone?.(event)
      if (event.type === 'error') throw new Error(event.detail || '病例助手回答失败')
    }
  }
}
