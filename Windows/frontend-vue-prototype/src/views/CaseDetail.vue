<template>
  <div class="page-shell case-detail-page">
    <div class="page-header">
      <div>
        <h2>病例详情</h2>
        <p>病例编号：{{ caseId }}</p>
      </div>

      <div class="page-header-actions">
        <el-button @click="refresh">刷新</el-button>
        <el-button v-if="canStartOrgan" @click="startOrganTask">{{ organStatus.status === 'failed' ? '重试全器官分割' : '启动全器官分割' }}</el-button>
        <el-button v-if="canStartPpgl" type="warning" @click="startPpglTask">{{ ppglStatus.status === 'failed' ? '重试 PPGL 分割' : '启动 PPGL 分割' }}</el-button>
        <el-button @click="go3D">2D / 3D 联合阅片</el-button>
        <el-button @click="goKnowledge">病例 RAG 问答</el-button>
        <el-button type="primary" @click="goReport">查看 AI 报告</el-button>
      </div>
    </div>

    <el-alert
      v-if="organStatus.status === 'failed'"
      class="status-alert"
      :title="organStatus.error || organStatus.message || '全器官分割失败'"
      type="error"
      show-icon
    />
    <el-alert
      v-if="ppglStatus.status === 'failed'"
      class="status-alert"
      :title="ppglStatus.error || ppglStatus.message || 'PPGL 分割失败'"
      type="error"
      show-icon
    />

    <el-row :gutter="20">
      <el-col :xs="24" :lg="16">
        <el-card class="viewer-card" shadow="never">
          <template #header>
            <div class="section-header">
              <div>
                <strong>二维关键切片</strong>
                <p>自动选取分割器官所在的代表性层面</p>
              </div>
            </div>
          </template>

          <div v-if="sliceCards.length" class="slice-gallery">
            <div
              v-for="item in sliceCards"
              :key="item.filename"
              class="slice-tile"
            >
              <img :src="item.url" :alt="item.title" />
              <div class="slice-caption">
                <strong>{{ item.title }}</strong>
                <span>{{ planeText(item.plane) }} · 第 {{ item.index }} 层</span>
              </div>
            </div>
          </div>

          <div v-else-if="overlaySrc" class="image-viewer">
            <img :src="overlaySrc" alt="Segmentation overlay" />
          </div>
          <div v-else class="image-placeholder">
            <div>
              <p>{{ organStatus.message || '等待全器官分割结果' }}</p>
              <el-progress :percentage="organStatus.progress || 0" />
            </div>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="8">
        <el-card class="metric-card" shadow="never">
          <template #header>
            <div class="section-header">
              <div>
                <strong>量化指标</strong>
                <p>由分割结果自动计算</p>
              </div>
            </div>
          </template>

          <el-descriptions :column="1" border>
            <el-descriptions-item label="分割状态">
              <el-tag :type="statusTagType">
                {{ statusText }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="进度">
              {{ organStatus.progress || 0 }}%
            </el-descriptions-item>
            <el-descriptions-item label="分割模型">
              TotalSegmentator
            </el-descriptions-item>
            <el-descriptions-item label="分割任务">
              {{ summary.task || 'total' }}
            </el-descriptions-item>
            <el-descriptions-item label="有效器官数">
              {{ summary.organ_count ?? '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="最大结构">
              {{ largestOrgan.name }}
            </el-descriptions-item>
            <el-descriptions-item label="最大结构体积">
              {{ formatNumber(largestOrgan.volumeMl) }} ml
            </el-descriptions-item>
            <el-descriptions-item label="分割标签数">
              {{ result?.segmentation?.label_count || '-' }}
            </el-descriptions-item>
          </el-descriptions>

          <div class="actions">
            <el-button v-if="organStatus.status === 'completed'" type="primary" @click="downloadMask">
              下载 mask.nii.gz
            </el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="section-card" shadow="never">
      <template #header>
        <div class="section-header">
          <div>
            <strong>PPGL 肿瘤分割</strong>
            <p>ProgressPatchV5 独立推理结果</p>
          </div>
        </div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="任务状态">
          <el-tag :type="ppglStatusTagType">{{ ppglStatusText }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="进度">{{ ppglStatus.progress || 0 }}%</el-descriptions-item>
        <el-descriptions-item label="模型">{{ ppglResult?.model?.name || 'ProgressPatchV5' }}</el-descriptions-item>
        <el-descriptions-item label="检出肿瘤">{{ tumorDetectedText }}</el-descriptions-item>
        <el-descriptions-item label="肿瘤体积">{{ formatNumber(ppglMetrics.tumor_volume_ml) }} ml</el-descriptions-item>
        <el-descriptions-item label="最大径">{{ formatNumber(ppglMetrics.max_diameter_mm) }} mm</el-descriptions-item>
        <el-descriptions-item label="病灶数">{{ ppglMetrics.tumor_component_count ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="侧别">{{ sideText }}</el-descriptions-item>
      </el-descriptions>
      <div class="actions">
        <el-button v-if="ppglStatus.status === 'completed'" type="warning" @click="downloadPpglMask">
          下载 PPGL mask.nii.gz
        </el-button>
      </div>
    </el-card>

    <el-card class="section-card" shadow="never">
      <template #header>
        <div class="section-header">
          <div>
            <strong>处理流程</strong>
            <p>病例从上传到结果整理的当前节点</p>
          </div>
        </div>
      </template>

      <el-steps :active="activeStep" align-center>
        <el-step title="已上传" />
        <el-step title="AI 分割" />
        <el-step title="后处理" />
        <el-step title="结果整理" />
        <el-step title="完成" />
      </el-steps>
    </el-card>

    <el-card v-if="labelNames.length" class="section-card" shadow="never">
      <template #header>
        <div class="section-header">
          <div>
            <strong>分割类别</strong>
            <p>当前病例输出的可展示标签</p>
          </div>
        </div>
      </template>
      <el-tag v-for="name in labelNames" :key="name" class="label-tag">
        {{ name }}
      </el-tag>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import {
  getCaseResult,
  getCaseStatus,
  getPpglMaskUrl,
  getPpglResult,
  getSliceGallery,
  getSliceImageUrl,
  TOTALSEGMENTATOR_PARAMS,
  getMaskUrl,
  getOverlayUrl,
  startOrganSegmentation,
  startPpglSegmentation
} from '../api/caseApi'
import { authStore } from '../store/authStore.js'

const route = useRoute()
const router = useRouter()

const caseId = route.params.caseId
const status = ref({ status: 'loading', message: '正在加载病例状态', progress: 0 })
const result = ref(null)
const ppglResult = ref(null)
const sliceGallery = ref([])
const overlayVersion = ref(Date.now())
let pollTimer = null

const summary = computed(() => result.value?.summary || {})
const organStatus = computed(() => status.value.organ || status.value)
const ppglStatus = computed(() => status.value.ppgl || { status: 'uploaded', progress: 0 })
const ppglMetrics = computed(() => ppglResult.value?.metrics || {})
const statusTagType = computed(() => {
  const map = {
    uploaded: 'info',
    queued: 'warning',
    running: 'warning',
    completed: 'success',
    failed: 'danger',
    loading: 'info'
  }
  return map[organStatus.value.status] || 'info'
})
const statusText = computed(() => {
  const map = {
    uploaded: '已上传',
    queued: '排队中',
    running: '分割中',
    completed: '已完成',
    failed: '失败',
    loading: '加载中'
  }
  return map[organStatus.value.status] || organStatus.value.status
})
const ppglStatusTagType = computed(() => {
  const map = { uploaded: 'info', queued: 'warning', running: 'warning', completed: 'success', failed: 'danger' }
  return map[ppglStatus.value.status] || 'info'
})
const ppglStatusText = computed(() => {
  const map = { uploaded: '未启动', queued: '排队中', running: '分割中', completed: '已完成', failed: '失败' }
  return map[ppglStatus.value.status] || ppglStatus.value.status
})
const activeStep = computed(() => {
  const map = {
    uploaded: 1,
    queued: 2,
    running: 2,
    failed: 2,
    completed: 5
  }
  return map[organStatus.value.status] || 1
})
const canEdit = computed(() => authStore.state.user?.role !== 'viewer')
const canStartOrgan = computed(() => canEdit.value && ['uploaded', 'failed'].includes(organStatus.value.status))
const canStartPpgl = computed(() => canEdit.value && ['uploaded', 'failed'].includes(ppglStatus.value.status))
const overlaySrc = computed(() => {
  if (organStatus.value.status !== 'completed') return ''
  return `${getOverlayUrl(caseId)}?t=${overlayVersion.value}`
})
const sliceCards = computed(() => {
  if (organStatus.value.status !== 'completed') return []
  return sliceGallery.value.map(item => ({
    ...item,
    url: `${getSliceImageUrl(caseId, item.filename)}?t=${overlayVersion.value}`
  }))
})
const largestOrgan = computed(() => {
  const entries = Object.entries(summary.value.organs || {})
  if (!entries.length) return { name: '-', volumeMl: null }
  const [name, values] = entries.sort((left, right) => Number(right[1]?.volume_ml || 0) - Number(left[1]?.volume_ml || 0))[0]
  return { name: name.replaceAll('_', ' '), volumeMl: values?.volume_ml }
})
const labelNames = computed(() => {
  const labelMap = result.value?.segmentation?.label_map || {}
  return Object.values(labelMap).filter(name => name !== 'background')
})
const tumorDetectedText = computed(() => {
  if (ppglStatus.value.status !== 'completed') return '-'
  return ppglResult.value?.segmentation?.tumor_detected ? '是' : '否'
})
const sideText = computed(() => {
  const map = { left: '左侧', right: '右侧' }
  return map[ppglMetrics.value.tumor_side_by_nearest_kidney] || '-'
})

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return Number(value).toFixed(2)
}

function planeText(plane) {
  const map = {
    axial: '轴位',
    coronal: '冠状位',
    sagittal: '矢状位'
  }
  return map[plane] || plane
}

function clearPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function ensurePolling() {
  if (pollTimer) return
  pollTimer = setInterval(loadStatus, 3000)
}

async function loadStatus() {
  try {
    status.value = await getCaseStatus(caseId)
    if (organStatus.value.status === 'completed') {
      await loadResult()
    }
    if (ppglStatus.value.status === 'completed') {
      await loadPpglResult()
    }
    const active = [organStatus.value.status, ppglStatus.value.status].some(value => value === 'queued' || value === 'running')
    if (active) {
      ensurePolling()
    } else {
      clearPoll()
    }
  } catch (err) {
    clearPoll()
    ElMessage.error(err?.response?.data?.detail || '病例状态加载失败')
  }
}

async function loadPpglResult() {
  try {
    ppglResult.value = await getPpglResult(caseId)
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || 'PPGL 分割结果加载失败')
  }
}

async function loadResult() {
  try {
    result.value = await getCaseResult(caseId)
    overlayVersion.value = Date.now()
    await loadSliceGallery()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || '分割结果加载失败')
  }
}

async function loadSliceGallery() {
  try {
    const payload = await getSliceGallery(caseId)
    sliceGallery.value = payload.slices || []
  } catch (err) {
    sliceGallery.value = []
  }
}

async function refresh() {
  await loadStatus()
}

async function startOrganTask() {
  const retry = organStatus.value.status === 'failed'
  try {
    await startOrganSegmentation(caseId, { ...TOTALSEGMENTATOR_PARAMS, force: retry })
    ElMessage.success(retry ? '已重新提交全器官分割任务' : '已启动全器官分割任务')
    await loadStatus()
    ensurePolling()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || '启动全器官分割失败')
  }
}

async function startPpglTask() {
  const retry = ppglStatus.value.status === 'failed'
  try {
    await startPpglSegmentation(caseId, { device: 'cuda:0', force: retry })
    ElMessage.success(retry ? '已重新提交 PPGL 肿瘤分割任务' : '已启动 PPGL 肿瘤分割任务')
    await loadStatus()
    ensurePolling()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || '启动 PPGL 分割失败')
  }
}

function downloadMask() {
  window.open(getMaskUrl(caseId), '_blank')
}

function downloadPpglMask() {
  window.open(getPpglMaskUrl(caseId), '_blank')
}

function go3D() {
  router.push(`/cases/${caseId}/3d`)
}

function goReport() {
  router.push(`/cases/${caseId}/report`)
}

function goKnowledge() {
  router.push({ path: '/knowledge', query: { caseId } })
}

onMounted(loadStatus)
onUnmounted(clearPoll)
</script>

<style scoped>
.case-detail-page {
  min-height: 100%;
}

.status-alert {
  margin-bottom: 18px;
}

.viewer-card,
.metric-card,
.section-card {
  border-radius: var(--ppgl-radius);
  border-color: rgba(93, 235, 219, 0.22);
  background: rgba(8, 23, 36, 0.78);
}

.metric-card {
  min-height: 100%;
}

.image-placeholder,
.image-viewer {
  height: 430px;
  background:
    linear-gradient(90deg, rgba(93, 235, 219, 0.06) 1px, transparent 1px),
    linear-gradient(180deg, rgba(93, 235, 219, 0.045) 1px, transparent 1px),
    #050b13;
  background-size: 24px 24px;
  border-radius: var(--ppgl-radius);
  color: #cbd5e1;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  border: 1px solid rgba(93, 235, 219, 0.24);
  overflow: hidden;
}

.slice-gallery {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.slice-tile {
  overflow: hidden;
  border: 1px solid rgba(93, 235, 219, 0.24);
  border-radius: var(--ppgl-radius);
  background: #050b13;
  box-shadow: 0 16px 34px rgba(0, 0, 0, 0.24), 0 0 18px rgba(32, 224, 196, 0.08);
  transition: transform 0.2s ease, border-color 0.2s ease;
}

.slice-tile:hover {
  transform: translateY(-2px);
  border-color: rgba(93, 235, 219, 0.48);
}

.slice-tile img {
  display: block;
  width: 100%;
  height: 300px;
  object-fit: contain;
  background: #020617;
}

.slice-caption {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  background: rgba(8, 23, 36, 0.96);
  color: #e5edf5;
  border-top: 1px solid rgba(93, 235, 219, 0.18);
}

.slice-caption strong {
  font-size: 14px;
}

.slice-caption span {
  flex: 0 0 auto;
  color: #9fb0c2;
  font-size: 12px;
}

.image-viewer img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.actions {
  margin-top: 18px;
  display: flex;
  justify-content: flex-end;
}

.section-card {
  margin-top: 24px;
}

.label-tag {
  margin: 0 8px 8px 0;
}

:deep(.el-descriptions__label.el-descriptions__cell) {
  width: 112px;
  color: #b9f8f1;
  background: rgba(32, 224, 196, 0.08) !important;
  font-weight: 800;
}

:deep(.el-step__title) {
  font-weight: 700;
  color: var(--ppgl-text);
}

:deep(.el-step__line) {
  background-color: rgba(93, 235, 219, 0.18);
}

:deep(.el-step__head.is-process),
:deep(.el-step__head.is-finish) {
  color: var(--ppgl-primary);
  border-color: var(--ppgl-primary);
}

@media (max-width: 1100px) {
  .slice-gallery {
    grid-template-columns: 1fr;
  }
}
</style>
