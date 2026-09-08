<template>
  <div class="page-shell detail-page">
    <div class="page-header">
      <div>
        <h2>脑胶质瘤 MRI 分割结果</h2>
        <p>病例编号：{{ caseId }}</p>
      </div>
      <div class="page-header-actions">
        <el-button @click="router.push('/glioma/cases')">返回病例列表</el-button>
        <el-button
          v-if="canStart"
          type="success"
          :loading="actionLoading"
          @click="start"
        >
          {{ caseInfo.status === 'failed' ? '重新分割' : '开始分割' }}
        </el-button>
        <el-button v-if="canEdit && active" type="danger" plain :loading="actionLoading" @click="cancel">
          取消分割
        </el-button>
        <el-button v-if="completed" type="primary" @click="openSegmentation">
          下载分割结果
        </el-button>
      </div>
    </div>

    <el-alert
      title="脑胶质瘤自动分割结果仅供科研和辅助分析，应结合四序列原始 MRI 复核。"
      type="warning"
      show-icon
      class="notice"
    />

    <el-card shadow="never" class="status-card">
      <div class="status-row">
        <div>
          <span>当前状态</span>
          <strong>{{ statusText(caseInfo.status) }}</strong>
          <p>{{ caseInfo.message || '正在读取病例状态' }}</p>
        </div>
        <el-progress type="dashboard" :percentage="caseInfo.progress || 0" :status="progressStatus" />
      </div>
      <div class="modality-row">
        <span
          v-for="item in modalities"
          :key="item.key"
          :class="{ complete: caseInfo.uploaded_modalities?.includes(item.key) }"
        >
          {{ item.name }} {{ caseInfo.uploaded_modalities?.includes(item.key) ? '✓' : '—' }}
        </span>
      </div>
    </el-card>

    <template v-if="completed">
      <section class="metrics-grid">
        <article v-for="item in metricCards" :key="item.label">
          <span>{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
          <small>{{ item.unit }}</small>
        </article>
      </section>

      <section class="result-grid">
        <el-card shadow="never" class="viewer-card">
          <template #header>
            <div class="card-heading">
              <div><strong>MRI 与脑肿瘤分割叠加</strong><p>支持轴位、冠状位和矢状位逐层查看</p></div>
              <el-button v-if="canEdit" size="small" :loading="visualLoading" @click="regenerateVisuals">重新生成</el-button>
            </div>
          </template>

          <div class="layer-switches">
            <el-checkbox v-model="layers.edema">水肿 ED</el-checkbox>
            <el-checkbox v-model="layers.net">非增强肿瘤 NET</el-checkbox>
            <el-checkbox v-model="layers.et">增强肿瘤 ET</el-checkbox>
          </div>

          <div v-if="planes.length" class="slice-grid">
            <figure v-for="plane in planes" :key="plane.plane">
              <div class="slice-stack">
                <img :src="sliceUrl(plane, 'original')" :alt="`${plane.title} MRI`" />
                <img v-show="layers.edema" class="overlay" :src="sliceUrl(plane, 'edema')" alt="ED" />
                <img v-show="layers.net" class="overlay" :src="sliceUrl(plane, 'net')" alt="NET" />
                <img v-show="layers.et" class="overlay" :src="sliceUrl(plane, 'et')" alt="ET" />
              </div>
              <figcaption>{{ plane.title }} · 第 {{ positions[plane.plane] + 1 }} 层</figcaption>
              <el-slider
                v-model="positions[plane.plane]"
                :min="0"
                :max="Math.max(0, plane.slice_count - 1)"
                :show-tooltip="false"
              />
            </figure>
          </div>
          <el-empty v-else description="二维分割结果尚未生成" />
        </el-card>

        <el-card shadow="never" class="model-card">
          <template #header>
            <div class="card-heading"><div><strong>三维脑肿瘤模型</strong><p>独立切换 ED、NET 和 ET</p></div></div>
          </template>
          <GliomaModelViewer
            :case-id="caseId"
            :can-generate="canEdit && completed"
            :generating="visualLoading"
            :reload-key="viewerReloadKey"
            @generate="regenerateVisuals"
          />
        </el-card>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import {
  MRI_MODALITIES,
  cancelSegmentation,
  generateVisualizations,
  getCase,
  getMetrics,
  getSegmentationFileUrl,
  getVisualizationManifest,
  getVisualizationSliceUrl,
  startSegmentation
} from '../api/gliomaApi'
import { apiErrorMessage } from '../api/errors'
import GliomaModelViewer from '../components/GliomaModelViewer.vue'
import { authStore } from '../store/authStore.js'

const route = useRoute()
const router = useRouter()
const caseId = String(route.params.caseId)
const modalities = MRI_MODALITIES
const caseInfo = ref({ status: 'loading', progress: 0, uploaded_modalities: [] })
const metrics = ref(null)
const visualization = ref({ planes: [] })
const actionLoading = ref(false)
const visualLoading = ref(false)
const viewerReloadKey = ref(0)
const layers = reactive({ edema: true, net: true, et: true })
const positions = reactive({ axial: 0, coronal: 0, sagittal: 0 })
let pollTimer = null

const active = computed(() => ['queued', 'running'].includes(caseInfo.value.status))
const completed = computed(() => caseInfo.value.status === 'completed')
const canEdit = computed(() => authStore.state.user?.role !== 'viewer')
const canStart = computed(() => canEdit.value && caseInfo.value.upload_complete && ['uploaded', 'failed'].includes(caseInfo.value.status))
const planes = computed(() => visualization.value.planes || [])
const progressStatus = computed(() => caseInfo.value.status === 'failed' ? 'exception' : completed.value ? 'success' : undefined)

const metricCards = computed(() => {
  const regions = metrics.value?.regions || {}
  const measurements = metrics.value?.measurements || {}
  return [
    { label: '全肿瘤 WT 体积', value: formatNumber(regions.wt?.volume_ml), unit: 'mL' },
    { label: '肿瘤核心 TC 体积', value: formatNumber(regions.tc?.volume_ml), unit: 'mL' },
    { label: '增强肿瘤 ET 体积', value: formatNumber(regions.et?.volume_ml), unit: 'mL' },
    { label: '水肿 ED 体积', value: formatNumber(regions.edema?.volume_ml), unit: 'mL' },
    { label: '最大三维径', value: formatNumber(measurements.maximum_3d_diameter?.value), unit: 'mm' },
    { label: '病灶数量', value: measurements.lesion_component_count?.value ?? '—', unit: '个' }
  ]
})

function statusText(status) {
  return ({
    loading: '读取中', created: '等待上传', uploading: '上传中', validating: '校验中',
    uploaded: '等待分割', queued: '排队中', running: '分割中', completed: '脑胶质瘤分割完成',
    invalid: '序列校验失败', failed: '分割失败', cancelled: '已取消'
  })[status] || status
}

const formatNumber = value => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '—'

function sliceUrl(plane, layer) {
  return getVisualizationSliceUrl(
    caseId,
    plane.plane,
    positions[plane.plane] ?? plane.index,
    layer,
    visualization.value.generated_at || ''
  )
}

async function loadDerived() {
  try { metrics.value = await getMetrics(caseId) } catch { metrics.value = null }
  try {
    visualization.value = await getVisualizationManifest(caseId)
    for (const plane of visualization.value.planes || []) positions[plane.plane] = plane.index
  } catch { visualization.value = { planes: [] } }
}

async function loadCase(silent = false) {
  try {
    caseInfo.value = await getCase(caseId)
    if (caseInfo.value.status === 'completed') await loadDerived()
    if (active.value && !pollTimer) pollTimer = setInterval(() => loadCase(true), 3000)
    if (!active.value && pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  } catch (error) {
    if (!silent) ElMessage.error(apiErrorMessage(error, '病例加载失败'))
  }
}

async function start() {
  actionLoading.value = true
  try {
    await startSegmentation(caseId)
    ElMessage.success('脑胶质瘤分割任务已提交')
    await loadCase()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '分割任务提交失败'))
  } finally { actionLoading.value = false }
}

async function cancel() {
  actionLoading.value = true
  try {
    await cancelSegmentation(caseId)
    ElMessage.success('分割任务已取消')
    await loadCase()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '取消任务失败'))
  } finally { actionLoading.value = false }
}

async function regenerateVisuals() {
  visualLoading.value = true
  try {
    visualization.value = await generateVisualizations(caseId)
    for (const plane of visualization.value.planes || []) positions[plane.plane] = plane.index
    viewerReloadKey.value += 1
    ElMessage.success('二维和三维结果已重新生成')
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '可视化生成失败'))
  } finally { visualLoading.value = false }
}

function openSegmentation() {
  window.open(getSegmentationFileUrl(caseId), '_blank')
}

onMounted(() => loadCase())
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.notice { margin-bottom: 18px; }
.status-card, .viewer-card, .model-card { border-color: rgba(93, 235, 219, 0.22); background: rgba(8, 23, 36, 0.78); }
.status-row { display: flex; align-items: center; justify-content: space-between; gap: 24px; }
.status-row > div:first-child { flex: 1; }
.status-row span, .status-row p { color: var(--ppgl-muted); }
.status-row strong { display: block; margin: 6px 0; color: var(--ppgl-text); font-size: 22px; }
.modality-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
.modality-row span { padding: 6px 10px; border: 1px solid rgba(148,163,184,.2); border-radius: 999px; color: var(--ppgl-muted); }
.modality-row span.complete { border-color: rgba(32,224,196,.45); color: #8de8dd; }
.metrics-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin: 18px 0; }
.metrics-grid article { padding: 16px; border: 1px solid rgba(93,235,219,.18); border-radius: 8px; background: rgba(8,23,36,.78); }
.metrics-grid span, .metrics-grid small { display: block; color: var(--ppgl-muted); }
.metrics-grid strong { display: inline-block; margin: 8px 4px 0 0; color: var(--ppgl-text); font-size: 23px; }
.result-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px, .65fr); gap: 18px; }
.card-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.card-heading p { margin: 4px 0 0; color: var(--ppgl-muted); }
.layer-switches { display: flex; gap: 18px; margin-bottom: 14px; }
.slice-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.slice-grid figure { margin: 0; min-width: 0; }
.slice-stack { position: relative; aspect-ratio: 1; overflow: hidden; border-radius: 7px; background: #02070d; }
.slice-stack img { width: 100%; height: 100%; object-fit: contain; }
.slice-stack img.overlay { position: absolute; inset: 0; }
.slice-grid figcaption { margin: 8px 0; color: var(--ppgl-muted); text-align: center; }
@media (max-width: 1300px) { .metrics-grid { grid-template-columns: repeat(3, 1fr); } .result-grid { grid-template-columns: 1fr; } }
@media (max-width: 800px) { .slice-grid, .metrics-grid { grid-template-columns: 1fr; } }
</style>
