import request from './request.js'

export function submitKnowledgeDocument(file, metadata, onProgress) {
  const form = new FormData()
  form.append('file', file)
  Object.entries(metadata).forEach(([key, value]) => form.append(key, value))
  return request.post('/v1/knowledge/documents', form, {
    timeout: 180000,
    onUploadProgress: onProgress
  })
}
