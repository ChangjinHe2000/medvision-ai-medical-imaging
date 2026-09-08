import request from './request.js'

export const getSetupStatus = () => request.get('/v1/auth/setup/status')
export const login = credentials => request.post('/v1/auth/login', credentials)
export const getCurrentUser = () => request.get('/v1/auth/me')
export const logout = () => request.post('/v1/auth/logout')

export const listUsers = () => request.get('/v1/admin/users')
export const createUser = payload => request.post('/v1/admin/users', payload)
export const setUserActive = (userId, active) =>
  request.patch(`/v1/admin/users/${encodeURIComponent(userId)}/status`, { active })

export const listCaseGrants = caseId =>
  request.get(`/v1/cases/${encodeURIComponent(caseId)}/grants`)
export const setCaseGrant = (caseId, payload) =>
  request.put(`/v1/cases/${encodeURIComponent(caseId)}/grants`, payload)
export const revokeCaseGrant = (caseId, userId) =>
  request.delete(`/v1/cases/${encodeURIComponent(caseId)}/grants/${encodeURIComponent(userId)}`)
