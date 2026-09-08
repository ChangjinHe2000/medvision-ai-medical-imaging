import request from './request.js'

const path = value => encodeURIComponent(String(value))

export const listTrashCases = () => request.get('/v1/trash')
export const restoreTrashCase = caseId => request.post(`/v1/trash/${path(caseId)}/restore`)
export const purgeTrashCase = caseId => request.delete(`/v1/trash/${path(caseId)}`, {
  data: { reason_code: 'approved_purge', confirmation: String(caseId) }
})
