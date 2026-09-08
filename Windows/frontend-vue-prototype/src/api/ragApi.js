import request from './request'

export function searchKnowledge(query, topK = 5) {
  return request.post('/v1/rag/search', {
    query,
    top_k: topK,
    retrieval_mode: 'hybrid_rerank'
  })
}

export function queryKnowledge(question, topK = 6) {
  return request.post('/v1/rag/query', {
    question,
    top_k: topK,
    retrieval_mode: 'hybrid_rerank',
    max_tokens: 900
  }, {
    timeout: 300000
  })
}

export function queryCaseKnowledge(caseId, question, topK = 6) {
  return request.post(`/v1/cases/${encodeURIComponent(caseId)}/rag/query`, {
    question,
    top_k: topK,
    retrieve_k: 20,
    retrieval_mode: 'hybrid_rerank',
    max_tokens: 900
  }, {
    timeout: 300000
  })
}
