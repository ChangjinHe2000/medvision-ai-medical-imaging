import request from './request'

const AI_API = '/ai'
const casePath = caseId => encodeURIComponent(caseId)

export function getReport(caseId) {
  return request.get(`${AI_API}/cases/${casePath(caseId)}/report`)
}

export function getAiReport(caseId) {
  return request.get(`${AI_API}/cases/${casePath(caseId)}/ai-report`)
}

export function generateAiReport(caseId) {
  return request.post(`${AI_API}/cases/${casePath(caseId)}/ai-report/generate`, null, {
    timeout: 0
  })
}

export function getAiReportChat(caseId) {
  return request.get(`${AI_API}/cases/${casePath(caseId)}/ai-report/chat`)
}

export function chatWithAiReport(caseId, question, history = []) {
  return request.post(
    `${AI_API}/cases/${casePath(caseId)}/ai-report/chat`,
    { question, history },
    { timeout: 0 }
  )
}

export async function streamAiReportChat(caseId, question, history = [], onDelta) {
  const response = await fetch(`/api${AI_API}/cases/${casePath(caseId)}/ai-report/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ question, history })
  })

  if (!response.ok) {
    const body = await response.text()
    let detail = 'AI 问答失败'
    try {
      const payload = JSON.parse(body)
      detail = payload?.detail || detail
    } catch {
      detail = body || detail
    }
    throw new Error(detail)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finalPayload = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) {
      break
    }

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (!line.trim()) {
        continue
      }
      const event = JSON.parse(line)
      if (event.type === 'delta') {
        onDelta?.(event.text || '')
      } else if (event.type === 'done') {
        finalPayload = event.chat
      } else if (event.type === 'error') {
        throw new Error(event.detail || 'AI 问答失败')
      }
    }
  }

  if (buffer.trim()) {
    const event = JSON.parse(buffer)
    if (event.type === 'done') {
      finalPayload = event.chat
    } else if (event.type === 'error') {
      throw new Error(event.detail || 'AI 问答失败')
    }
  }

  return finalPayload
}
