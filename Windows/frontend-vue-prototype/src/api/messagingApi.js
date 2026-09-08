import request from './request.js'

export const getMessageContacts = () => request.get('/v1/messages/contacts')
export const getUnreadCount = () => request.get('/v1/messages/unread')
export const getDirectMessages = (userId, limit = 100) =>
  request.get(`/v1/messages/${encodeURIComponent(userId)}`, { params: { limit } })
export const sendDirectMessage = (userId, content) =>
  request.post('/v1/messages', { recipient_id: userId, content })
export const markDirectMessagesRead = userId =>
  request.post(`/v1/messages/${encodeURIComponent(userId)}/read`)
