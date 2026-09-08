import request from './request.js'
import { buildApiUrl } from '../config/runtime.js'

const V1 = '/v1'
const path = value => encodeURIComponent(String(value))

export const TOTALSEGMENTATOR_PARAMS = {
  mode: 'full_total',
  device: 'cuda',
  fast: false,
  fastest: false
}

function taskSummary(task, fallback = {}) {
  return task ? {
    status: task.status,
    message: task.message || fallback.message || '',
    progress: Number(task.progress || 0),
    task_id: task.task_id,
    error_code: task.error_code,
    retryable: Boolean(task.retryable)
  } : fallback
}

export async function getCaseList() {
  const result = await request.get(`${V1}/cases`)
  const records = (result.cases || []).filter(item => item.case_type === 'ppgl_ct')
  const cases = await Promise.all(records.map(async item => {
    try {
      const tasks = await getCaseStatus(item.case_id)
      return { ...item, ...tasks, original_filename: '', imaging_type: 'ct' }
    } catch {
      return {
        ...item,
        imaging_type: 'ct',
        status: item.workflow_status,
        organ_status: { status: item.workflow_status, progress: 0 },
        ppgl_status: { status: 'created', progress: 0 }
      }
    }
  }))
  return { cases, total: cases.length }
}

export async function uploadCT(file, onUploadProgress) {
  const displayName = String(file?.name || 'CT 病例').replace(/\.nii(?:\.gz)?$/i, '') || 'CT 病例'
  const created = await request.post(`${V1}/cases`, { case_type: 'ppgl_ct', display_name: displayName })
  const formData = new FormData()
  formData.append('file', file)
  try {
    const uploaded = await request.put(`${V1}/cases/${path(created.case_id)}/ct/image`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress,
      timeout: 0
    })
    return { ...uploaded, case_id: created.case_id, display_name: created.display_name }
  } catch (error) {
    try {
      await request.delete(`${V1}/cases/${path(created.case_id)}`, {
        data: { reason_code: 'test_cleanup' }
      })
    } catch { /* 保留原始上传错误 */ }
    throw error
  }
}

export const updateCaseId = (caseId, displayName) =>
  request.patch(`${V1}/cases/${path(caseId)}`, { display_name: displayName })
export const deleteCase = (caseId, reasonCode = 'user_request') =>
  request.delete(`${V1}/cases/${path(caseId)}`, { data: { reason_code: reasonCode } })

export const startOrganSegmentation = (caseId, params = {}) =>
  request.post(`${V1}/cases/${path(caseId)}/ct/segment/organs`, {
    ...TOTALSEGMENTATOR_PARAMS,
    ...params,
    fast: params.fast ?? params.totalseg_fast ?? false,
    fastest: params.fastest ?? params.totalseg_fastest ?? false
  })
export const startPpglSegmentation = (caseId, params = {}) =>
  request.post(`${V1}/cases/${path(caseId)}/ct/segment/ppgl`, { device: 'cuda:0', ...params })
export const cancelOrganSegmentation = caseId =>
  request.post(`${V1}/cases/${path(caseId)}/ct/tasks/organs/cancel`)
export const cancelPpglSegmentation = caseId =>
  request.post(`${V1}/cases/${path(caseId)}/ct/tasks/ppgl/cancel`)

export async function getCaseStatus(caseId) {
  const result = await request.get(`${V1}/cases/${path(caseId)}/ct/tasks`)
  const legacy = result.status || {}
  return {
    ...legacy,
    status: legacy.status || result.organ_task?.status || 'created',
    organ_status: taskSummary(result.organ_task, legacy.organ || legacy),
    ppgl_status: taskSummary(result.ppgl_task, legacy.ppgl || {})
  }
}

export const getCaseResult = caseId => request.get(`${V1}/cases/${path(caseId)}/ct/results/organs`)
export const getPpglResult = caseId => request.get(`${V1}/cases/${path(caseId)}/ct/results/ppgl`)
export const getSliceGallery = caseId => request.get(`${V1}/cases/${path(caseId)}/ct/slices`)
export const getMeshManifest = caseId => request.get(`${V1}/cases/${path(caseId)}/ct/mesh-manifest/organs`)
export const getPpglMeshManifest = caseId => request.get(`${V1}/cases/${path(caseId)}/ct/mesh-manifest/ppgl`)
export const getLabelMap = caseId => request.get(`${V1}/cases/${path(caseId)}/ct/label-map`)

export const getCaseCtUrl = caseId => buildApiUrl(`${V1}/cases/${path(caseId)}/ct/assets/ct`)
export const getMaskUrl = caseId => buildApiUrl(`${V1}/cases/${path(caseId)}/ct/assets/organ-mask`)
export const getOverlayUrl = caseId => buildApiUrl(`${V1}/cases/${path(caseId)}/ct/assets/overlay`)
export const getMeshUrl = caseId => buildApiUrl(`${V1}/cases/${path(caseId)}/ct/assets/organ-mesh`)
export const getPpglMaskUrl = caseId => buildApiUrl(`${V1}/cases/${path(caseId)}/ct/assets/ppgl-mask`)
export const getPpglMeshUrl = caseId => buildApiUrl(`${V1}/cases/${path(caseId)}/ct/assets/ppgl-mesh`)
export const getSliceImageUrl = (caseId, filename) =>
  buildApiUrl(`${V1}/cases/${path(caseId)}/ct/slices/${encodeURIComponent(filename)}`)
export const getOrganMeshUrl = (caseId, labelId) =>
  buildApiUrl(`${V1}/cases/${path(caseId)}/ct/meshes/organs/${labelId}`)
export const getPpglOrganMeshUrl = (caseId, labelId) =>
  buildApiUrl(`${V1}/cases/${path(caseId)}/ct/meshes/ppgl/${labelId}`)
