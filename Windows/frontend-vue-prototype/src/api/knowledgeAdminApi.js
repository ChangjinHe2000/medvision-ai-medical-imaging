import request from './request.js'

const id = value => encodeURIComponent(String(value))

export const getKnowledgeStatus = () => request.get('/v1/admin/knowledge/status')
export const listKnowledgeDocuments = () => request.get('/v1/admin/knowledge/documents')
export function uploadKnowledgeDocument(file, metadata) {
  const form = new FormData()
  form.append('file', file)
  Object.entries(metadata).forEach(([key, value]) => form.append(key, value))
  return request.post('/v1/admin/knowledge/documents', form, { timeout: 180000 })
}
export const parseKnowledgeDocument = documentId => request.post(`/v1/admin/knowledge/documents/${id(documentId)}/parse`, {}, { timeout: 180000 })
export const indexKnowledgeDocument = documentId => request.post(`/v1/admin/knowledge/documents/${id(documentId)}/index`, {}, { timeout: 600000 })
export const enableKnowledgeDocument = documentId => request.post(`/v1/admin/knowledge/documents/${id(documentId)}/enable`)
export const disableKnowledgeDocument = documentId => request.post(`/v1/admin/knowledge/documents/${id(documentId)}/disable`)
export const deleteKnowledgeDocument = documentId => request.delete(`/v1/admin/knowledge/documents/${id(documentId)}`)
