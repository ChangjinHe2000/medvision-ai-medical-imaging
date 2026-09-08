import { reactive } from 'vue'

const STORAGE_PREFIX = 'medvision-conversations:'
const state = reactive({ accounts: {} })
let sequence = 0

function loadAccount(userId) {
  const key = String(userId || 'anonymous')
  if (state.accounts[key]) return state.accounts[key]
  let saved = null
  try { saved = JSON.parse(localStorage.getItem(`${STORAGE_PREFIX}${key}`) || 'null') } catch { saved = null }
  state.accounts[key] = saved || { general: [], cases: {} }
  return state.accounts[key]
}

function persist(userId) {
  const key = String(userId || 'anonymous')
  try { localStorage.setItem(`${STORAGE_PREFIX}${key}`, JSON.stringify(loadAccount(key))) } catch { /* storage unavailable */ }
}

function generalMessages(userId) { return loadAccount(userId).general }
function caseMessages(userId, caseId) {
  const bucket = loadAccount(userId)
  const key = String(caseId || '')
  if (!bucket.cases[key]) bucket.cases[key] = []
  return bucket.cases[key]
}
function nextMessageId() { sequence += 1; return `chat_${Date.now()}_${sequence}` }
function clearAccount(userId) {
  const key = String(userId || 'anonymous')
  delete state.accounts[key]
  try { localStorage.removeItem(`${STORAGE_PREFIX}${key}`) } catch { /* storage unavailable */ }
}

export const conversationStore = { state, generalMessages, caseMessages, nextMessageId, persist, clearAccount }
