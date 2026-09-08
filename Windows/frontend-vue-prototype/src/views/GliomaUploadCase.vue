<template>
  <div class="page-shell upload-page">
    <div class="page-header">
      <div>
        <h2>脑胶质瘤 MRI 病例上传</h2>
        <p>一次病例必须提供已经配准的 FLAIR、T1、T1CE、T2 四个三维 NIfTI 序列</p>
      </div>
    </div>

    <el-alert
      title="第一版只接收 .nii.gz；四序列的 shape、spacing、方向和 affine 必须一致。"
      type="info"
      show-icon
      class="tip-alert"
    />

    <div class="modality-grid">
      <el-card v-for="item in modalityCards" :key="item.key" class="modality-card" shadow="never">
        <div class="modality-heading">
          <div><strong>{{ item.name }}</strong><span>_{{ item.channel }}</span></div>
          <el-tag :type="stateTagType(item.state)">{{ stateText(item.state) }}</el-tag>
        </div>
        <p>{{ item.description }}</p>
        <el-upload
          action="#"
          :auto-upload="false"
          :show-file-list="false"
          :disabled="busy"
          :on-change="file => selectFile(item.key, file)"
        >
          <el-button :disabled="busy">{{ item.file ? '重新选择' : '选择文件' }}</el-button>
        </el-upload>
        <div class="filename">{{ item.file?.name || '尚未选择 .nii.gz' }}</div>
        <el-progress :percentage="item.progress" :status="progressStatus(item.state)" />
        <div v-if="item.totalBytes" class="transfer-details">
          <span>{{ formatBytes(item.loadedBytes) }} / {{ formatBytes(item.totalBytes) }}</span>
          <span v-if="item.speedBytes">{{ formatBytes(item.speedBytes) }}/秒 · 约 {{ formatDuration(item.etaSeconds) }}</span>
          <span v-else-if="item.retryCount">已自动重试 {{ item.retryCount }} 次</span>
        </div>
        <div v-if="item.error" class="modality-error">{{ item.error }}</div>
        <div v-else-if="item.metadata" class="metadata">
          {{ item.metadata.shape?.join(' × ') }} · {{ item.metadata.spacing?.map(formatSpacing).join(' / ') }} mm
        </div>
      </el-card>
    </div>

    <el-card class="workflow-card" shadow="never">
      <div class="workflow-summary">
        <div>
          <strong>病例编号</strong>
          <p>{{ caseId || '上传时由后端生成，不使用患者姓名' }}</p>
        </div>
        <div>
          <strong>四序列校验</strong>
          <p>{{ validationMessage }}</p>
          <p v-if="capacityMessage" class="capacity-message">{{ capacityMessage }}</p>
        </div>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="uploading" :disabled="!hasPendingUpload || !allPendingFilesSelected || busy" @click="uploadAll">
          {{ caseId ? '继续上传未完成序列' : '创建病例并上传四序列' }}
        </el-button>
        <el-button v-if="uploading" type="danger" plain @click="cancelUpload">取消当前上传</el-button>
        <el-button :loading="validating" :disabled="!allUploaded || busy" @click="validateUploads">
          校验四序列
        </el-button>
        <el-button type="success" :loading="segmenting" :disabled="!readyForSegmentation || busy" @click="startCaseSegmentation">
          开始脑胶质瘤分割
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import {
  MRI_MODALITIES,
  completeUploads,
  createCase,
  getUploadState,
  preflightUploads,
  startSegmentation,
  uploadModality
} from '../api/gliomaApi'
import { apiErrorMessage } from '../api/errors'

const router = useRouter()
const route = useRoute()
const caseId = ref('')
const uploading = ref(false)
const validating = ref(false)
const segmenting = ref(false)
const readyForSegmentation = ref(false)
const validationMessage = ref('等待上传四个 MRI 序列')
const capacityMessage = ref('')
const currentController = ref(null)
const cancelRequested = ref(false)
const cards = reactive(Object.fromEntries(MRI_MODALITIES.map(item => [item.key, {
  ...item,
  file: null,
  progress: 0,
  state: 'empty',
  error: '',
  metadata: null,
  loadedBytes: 0,
  totalBytes: 0,
  speedBytes: 0,
  etaSeconds: 0,
  retryCount: 0
}])))

const modalityCards = computed(() => MRI_MODALITIES.map(item => cards[item.key]))
const busy = computed(() => uploading.value || validating.value || segmenting.value)
const allPendingFilesSelected = computed(() => modalityCards.value.every(
  item => item.state === 'uploaded' || item.file?.raw
))
const hasPendingUpload = computed(() => modalityCards.value.some(item => item.state !== 'uploaded'))
const allUploaded = computed(() => modalityCards.value.every(item => item.state === 'uploaded'))

function selectFile(key, file) {
  const item = cards[key]
  const name = file?.name || ''
  if (!name.toLowerCase().endsWith('.nii.gz')) {
    item.file = null
    item.state = 'error'
    item.error = '必须选择 .nii.gz 文件'
    return
  }
  item.file = file
  item.progress = 0
  item.loadedBytes = 0
  item.totalBytes = Number(file.raw?.size || 0)
  item.speedBytes = 0
  item.etaSeconds = 0
  item.retryCount = 0
  item.state = 'selected'
  item.error = ''
  item.metadata = null
  readyForSegmentation.value = false
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))

function retryableUploadError(error, stalled) {
  if (stalled) return true
  if (error?.code === 'ERR_CANCELED') return false
  const status = Number(error?.response?.status || 0)
  return !status || [408, 425, 429, 500, 502, 503, 504].includes(status)
}

async function uploadOne(item) {
  const maximumAttempts = 3
  for (let attempt = 1; attempt <= maximumAttempts; attempt += 1) {
    const controller = new AbortController()
    currentController.value = controller
    let lastProgressAt = Date.now()
    let lastSampleAt = Date.now()
    let lastSampleBytes = 0
    let stalled = false
    const stallTimer = setInterval(() => {
      if (Date.now() - lastProgressAt >= 120000) {
        stalled = true
        controller.abort()
      }
    }, 5000)

    item.state = attempt === 1 ? 'uploading' : 'retrying'
    item.retryCount = attempt - 1
    item.error = attempt === 1 ? '' : `连接中断，正在进行第 ${attempt - 1} 次自动重试`
    item.speedBytes = 0
    item.etaSeconds = 0
    try {
      const result = await uploadModality(caseId.value, item.key, item.file.raw, {
        signal: controller.signal,
        onUploadProgress: event => {
          const now = Date.now()
          lastProgressAt = now
          item.loadedBytes = Number(event.loaded || 0)
          item.totalBytes = Number(event.total || item.file.raw.size || 0)
          item.progress = item.totalBytes
            ? Math.min(99, Math.round(item.loadedBytes * 100 / item.totalBytes))
            : item.progress
          const elapsedSeconds = Math.max((now - lastSampleAt) / 1000, 0.001)
          const sampledSpeed = Math.max(0, item.loadedBytes - lastSampleBytes) / elapsedSeconds
          item.speedBytes = Number(event.rate || sampledSpeed || 0)
          item.etaSeconds = Number(
            event.estimated || (item.speedBytes ? (item.totalBytes - item.loadedBytes) / item.speedBytes : 0)
          )
          lastSampleAt = now
          lastSampleBytes = item.loadedBytes
        }
      })
      item.progress = 100
      item.loadedBytes = item.totalBytes
      item.speedBytes = 0
      item.etaSeconds = 0
      item.state = 'uploaded'
      item.error = ''
      item.metadata = result.file?.nifti || null
      return
    } catch (error) {
      if (cancelRequested.value) {
        item.state = 'selected'
        item.progress = 0
        item.loadedBytes = 0
        item.error = '上传已取消，可以重新开始'
        throw error
      }
      if (attempt < maximumAttempts && retryableUploadError(error, stalled)) {
        item.progress = 0
        item.loadedBytes = 0
        await wait(1500 * (2 ** (attempt - 1)))
        if (cancelRequested.value) throw error
        continue
      }
      item.state = 'error'
      item.error = stalled
        ? `${item.name} 上传长时间没有进度，已停止重试`
        : apiErrorMessage(error, `${item.name} 上传失败`)
      throw error
    } finally {
      clearInterval(stallTimer)
      if (currentController.value === controller) currentController.value = null
    }
  }
}

function cancelUpload() {
  cancelRequested.value = true
  currentController.value?.abort()
}

async function restoreUploadState(showMessage = false) {
  if (!caseId.value) return
  const state = await getUploadState(caseId.value)
  for (const item of modalityCards.value) {
    const saved = state.modalities?.[item.key]
    if (saved?.uploaded) {
      item.state = 'uploaded'
      item.progress = 100
      item.loadedBytes = Number(saved.size_bytes || 0)
      item.totalBytes = Number(saved.size_bytes || 0)
      item.metadata = saved.nifti || null
      item.error = ''
    }
  }
  readyForSegmentation.value = Boolean(state.ready_for_segmentation)
  validationMessage.value = state.complete
    ? '四序列已经校验完成，可以提交脑胶质瘤分割'
    : `已恢复病例上传状态，当前已上传 ${modalityCards.value.filter(item => item.state === 'uploaded').length}/4 个序列`
  if (showMessage) ElMessage.success('已恢复该病例的上传状态')
}

async function uploadAll() {
  if (!allPendingFilesSelected.value || !hasPendingUpload.value) return
  uploading.value = true
  cancelRequested.value = false
  capacityMessage.value = ''
  validationMessage.value = '正在检查存储容量'
  try {
    if (!caseId.value) {
      const created = await createCase()
      caseId.value = created.case_id
      await router.replace({ path: route.path, query: { ...route.query, case: caseId.value } })
    }
    const pendingSizes = Object.fromEntries(
      modalityCards.value
        .filter(item => item.state !== 'uploaded')
        .map(item => [item.key, Number(item.file.raw.size)])
    )
    const capacity = await preflightUploads(caseId.value, pendingSizes)
    capacityMessage.value = `容量检查通过：本次 ${formatBytes(capacity.incoming_bytes)}，磁盘可用 ${formatBytes(capacity.free_bytes)}`
    validationMessage.value = '正在顺序上传四个 MRI 序列'
    for (const item of modalityCards.value) {
      if (item.state === 'uploaded') continue
      await uploadOne(item)
    }
    validationMessage.value = '四个文件上传完成，请执行空间一致性校验'
    ElMessage.success('四个 MRI 序列上传完成')
  } catch (error) {
    if (cancelRequested.value) {
      validationMessage.value = '上传已取消；后端已完成的序列会保留，未完成序列可以重新上传'
      ElMessage.info(validationMessage.value)
    } else {
      validationMessage.value = apiErrorMessage(error, '上传中断，可重新选择失败序列后重试')
      ElMessage.error(validationMessage.value)
    }
  } finally {
    uploading.value = false
  }
}

async function validateUploads() {
  validating.value = true
  validationMessage.value = '正在校验 NIfTI Header 和四序列空间一致性'
  try {
    await completeUploads(caseId.value)
    readyForSegmentation.value = true
    validationMessage.value = '校验通过，可以提交脑胶质瘤分割'
    ElMessage.success('四序列校验通过')
  } catch (error) {
    readyForSegmentation.value = false
    validationMessage.value = apiErrorMessage(error, '四序列校验失败')
    ElMessage.error(validationMessage.value)
  } finally {
    validating.value = false
  }
}

async function startCaseSegmentation() {
  segmenting.value = true
  try {
    await startSegmentation(caseId.value)
    ElMessage.success('脑胶质瘤分割任务已提交')
    router.push(`/glioma/cases/${caseId.value}`)
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '分割任务提交失败'))
  } finally {
    segmenting.value = false
  }
}

const stateText = state => ({ empty: '未选择', selected: '待上传', uploading: '上传中', retrying: '重试中', uploaded: '已上传', error: '失败' }[state] || state)
const stateTagType = state => ({ uploaded: 'success', uploading: 'warning', retrying: 'warning', error: 'danger', selected: 'info' }[state] || 'info')
const progressStatus = state => state === 'uploaded' ? 'success' : state === 'error' ? 'exception' : undefined
const formatSpacing = value => Number(value).toFixed(2)
const formatBytes = value => {
  const bytes = Number(value || 0)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}
const formatDuration = value => {
  const seconds = Math.max(0, Math.round(Number(value || 0)))
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

onMounted(async () => {
  const restoredCaseId = String(route.query.case || '').trim()
  if (!restoredCaseId) return
  caseId.value = restoredCaseId
  try {
    await restoreUploadState(true)
  } catch (error) {
    caseId.value = ''
    await router.replace({ path: route.path, query: {} })
    ElMessage.warning(apiErrorMessage(error, '无法恢复该病例的上传状态'))
  }
})

onBeforeUnmount(() => {
  if (uploading.value) cancelUpload()
})
</script>

<style scoped>
.tip-alert { margin-bottom: 20px; }
.modality-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.modality-card, .workflow-card { border-color: rgba(93, 235, 219, 0.22); background: rgba(8, 23, 36, 0.78); }
.modality-heading, .workflow-summary, .actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.modality-heading strong { font-size: 20px; color: var(--ppgl-text); }
.modality-heading span { margin-left: 8px; color: var(--ppgl-muted); }
.modality-card p, .workflow-summary p { color: var(--ppgl-muted); }
.filename { min-height: 38px; margin: 12px 0; padding: 9px 11px; border-radius: 6px; background: rgba(5,16,26,.72); color: var(--ppgl-text); word-break: break-all; }
.metadata { margin-top: 9px; color: #8de8dd; font-size: 12px; }
.transfer-details { min-height: 20px; margin-top: 7px; display: flex; justify-content: space-between; gap: 8px; color: var(--ppgl-muted); font-size: 12px; }
.modality-error { margin-top: 9px; color: #f87171; font-size: 12px; }
.capacity-message { color: #8de8dd !important; font-size: 12px; }
.workflow-card { margin-top: 20px; }
.workflow-summary { align-items: flex-start; }
.workflow-summary > div { flex: 1; }
.workflow-summary strong { color: var(--ppgl-text); }
.actions { margin-top: 18px; justify-content: flex-end; flex-wrap: wrap; }
@media (max-width: 800px) { .modality-grid { grid-template-columns: 1fr; } .workflow-summary { flex-direction: column; } }
</style>
