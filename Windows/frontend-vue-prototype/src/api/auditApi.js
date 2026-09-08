import request from './request.js'

const compact = params => Object.fromEntries(
  Object.entries(params || {}).filter(([, value]) => value !== '' && value !== null && value !== undefined)
)

export const listAuditLogs = params => request.get('/v1/admin/audit', { params: compact(params) })
export const verifyAuditIntegrity = () => request.get('/v1/admin/audit/integrity')
export const exportAuditLogs = params => request.get('/v1/admin/audit/export.csv', {
  params: compact(params),
  responseType: 'blob',
  timeout: 120000
})
