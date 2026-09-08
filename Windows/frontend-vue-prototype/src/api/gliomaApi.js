import request from './request.js'
import { buildApiUrl } from '../config/runtime.js'

const V1 = '/v1'
const path = value => encodeURIComponent(String(value))

export const MRI_MODALITIES = [
  { key: 'flair', name: 'FLAIR', channel: '0000', description: '液体衰减反转恢复序列' },
  { key: 't1', name: 'T1', channel: '0001', description: 'T1 加权序列' },
  { key: 't1ce', name: 'T1CE', channel: '0002', description: '增强 T1 加权序列' },
  { key: 't2', name: 'T2', channel: '0003', description: 'T2 加权序列' }
]

export async function listCases() {
  const result = await request.get(`${V1}/cases`)
  const records = (result.cases || []).filter(item => item.case_type === 'brain_mri')
  const cases = await Promise.all(records.map(async item => {
    try { return { ...item, ...(await getCase(item.case_id)), imaging_type: 'mri' } }
    catch { return { ...item, status: item.workflow_status, uploaded_modalities: [], upload_complete: false, imaging_type: 'mri' } }
  }))
  return { cases, total: cases.length }
}

export async function getCase(caseId) {
  const [record, uploads, task] = await Promise.all([
    request.get(`${V1}/cases/${path(caseId)}`),
    getUploadState(caseId),
    getSegmentationStatus(caseId).catch(() => null)
  ])
  const uploadedModalities = Object.entries(uploads.modalities || {}).filter(([, value]) => value.uploaded).map(([key]) => key)
  return {
    ...record,
    status: task?.status || uploads.status || record.workflow_status,
    message: task?.message || uploads.message || '',
    progress: Number(task?.progress || 0),
    active: Boolean(task?.active),
    upload_complete: Boolean(uploads.complete),
    ready_for_segmentation: Boolean(uploads.complete && uploads.status === 'uploaded'),
    uploaded_modalities: uploadedModalities,
    required_modalities: MRI_MODALITIES.map(item => item.key)
  }
}

export const createCase = (displayName = 'MRI 病例') =>
  request.post(`${V1}/cases`, { case_type: 'brain_mri', display_name: displayName })
export const renameCase = (caseId, displayName) =>
  request.patch(`${V1}/cases/${path(caseId)}`, { display_name: displayName })
export const deleteCase = (caseId, reasonCode = 'user_request') =>
  request.delete(`${V1}/cases/${path(caseId)}`, { data: { reason_code: reasonCode } })

export async function getUploadState(caseId) {
  const manifest = await request.get(`${V1}/cases/${path(caseId)}/mri/uploads`)
  return {
    ...manifest,
    status: manifest.complete ? 'uploaded' : 'uploading',
    message: manifest.complete ? '四序列空间一致性校验已通过' : '等待上传四序列 MRI',
    progress: Object.values(manifest.modalities || {}).filter(item => item.uploaded).length * 25,
    ready_for_segmentation: Boolean(manifest.complete)
  }
}
export const preflightUploads = (caseId, modalities) =>
  request.post(`${V1}/cases/${path(caseId)}/mri/uploads/preflight`, { modalities })
export function uploadModality(caseId, modality, file, options = {}) {
  const formData = new FormData()
  formData.append('file', file)
  return request.put(`${V1}/cases/${path(caseId)}/mri/images/${encodeURIComponent(modality)}`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: options.onUploadProgress,
    signal: options.signal,
    timeout: 0
  })
}
export const completeUploads = caseId => request.post(`${V1}/cases/${path(caseId)}/mri/uploads/complete`)
export const startSegmentation = caseId => request.post(`${V1}/cases/${path(caseId)}/mri/segment`)
export const getSegmentationStatus = caseId => request.get(`${V1}/cases/${path(caseId)}/mri/task`)
export const cancelSegmentation = caseId => request.post(`${V1}/cases/${path(caseId)}/mri/task/cancel`)
export const getMetrics = caseId => request.get(`${V1}/cases/${path(caseId)}/mri/metrics`)
export const getModelStatus = () => request.get(`${V1}/cases/models/brain`)
export const getVisualizationManifest = caseId => request.get(`${V1}/cases/${path(caseId)}/mri/visualizations`)
export const generateVisualizations = caseId => request.post(`${V1}/cases/${path(caseId)}/mri/visualizations`, null, { timeout: 0 })
export const getMeshManifest = caseId => getVisualizationManifest(caseId)

export const getSegmentationFileUrl = caseId => buildApiUrl(`${V1}/cases/${path(caseId)}/mri/segmentation`)
export const getVisualizationSliceUrl = (caseId, plane, index, layer, version = '') => {
  const url = buildApiUrl(`${V1}/cases/${path(caseId)}/mri/visualizations/slices/${encodeURIComponent(plane)}/${index}/${encodeURIComponent(layer)}.png`)
  return version ? `${url}?v=${encodeURIComponent(version)}` : url
}
export const getMeshFileUrl = (caseId, filename = 'scene.glb') =>
  buildApiUrl(`${V1}/cases/${path(caseId)}/mri/visualizations/meshes/${encodeURIComponent(filename)}`)
