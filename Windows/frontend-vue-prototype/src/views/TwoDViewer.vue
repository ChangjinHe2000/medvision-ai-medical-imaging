<template>
  <div class="page-shell slice-viewer-page" :class="{ 'embedded-viewer': embedded }">
    <div v-if="!embedded" class="page-header">
      <div>
        <h2>NiiVue MPR 阅片</h2>
        <p>病例编号：{{ caseId }} · 轴位 / 冠状位 / 矢状位联动</p>
      </div>
      <div class="page-header-actions">
        <el-button @click="resetViewer">重置视图</el-button>
        <el-button @click="go3D">查看三维重建</el-button>
        <el-button @click="goBack">返回病例详情</el-button>
      </div>
    </div>

    <el-card class="viewer-card" shadow="never">
      <div class="viewer-toolbar">
        <strong v-if="embedded" class="pane-title">三轴位切片</strong>

        <div class="toolbar-control">
          <span>分割叠加</span>
          <el-switch v-model="overlayVisible" />
        </div>

        <div class="toolbar-control opacity-control">
          <span>叠加透明度</span>
          <el-slider
            v-model="overlayOpacity"
            :min="0.05"
            :max="1"
            :step="0.05"
            :disabled="!overlayVisible"
            :show-tooltip="false"
          />
          <strong>{{ Math.round(overlayOpacity * 100) }}%</strong>
        </div>

        <div class="viewer-engine">
          <span class="engine-dot"></span>
          NiiVue WebGL2
        </div>

        <el-button v-if="embedded" size="small" @click="resetViewer">重置切片</el-button>
      </div>

      <div ref="viewerHost" class="viewer-host">
        <canvas ref="canvasRef" class="niivue-canvas"></canvas>

        <div v-if="loading" class="state-layer">
          <el-icon class="is-loading"><Loading /></el-icon>
          <strong>正在加载 CT 和 TotalSegmentator mask</strong>
          <span>首次加载需要下载完整 NIfTI 体数据</span>
        </div>

        <div v-if="errorMessage" class="state-layer error-state">
          <strong>{{ errorMessage }}</strong>
          <el-button size="small" @click="initializeViewer">重试</el-button>
        </div>
      </div>

      <div class="viewer-status">
        <span>{{ locationText }}</span>
        <span v-if="intensityText">CT：{{ intensityText }}</span>
        <span v-if="labelText">结构：{{ labelText }}</span>
        <span class="interaction-help">滚轮切层 · 点击移动联动十字线 · 右键拖动调节窗宽窗位</span>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import {
  cmapper,
  DRAG_MODE,
  MULTIPLANAR_TYPE,
  Niivue,
  SHOW_RENDER,
  SLICE_TYPE
} from '@niivue/niivue'
import { useRoute, useRouter } from 'vue-router'

import { getCaseCtUrl, getLabelMap, getMaskUrl } from '../api/caseApi'

const props = defineProps({
  embedded: {
    type: Boolean,
    default: false
  }
})

const embedded = props.embedded

const route = useRoute()
const router = useRouter()
const caseId = route.params.caseId

const viewerHost = ref(null)
const canvasRef = ref(null)
const loading = ref(true)
const errorMessage = ref('')
const overlayVisible = ref(true)
const overlayOpacity = ref(0.5)
const locationText = ref('十字线位置：-')
const intensityText = ref('')
const labelText = ref('')
let niivue = null
const labelNames = new Map()

function hslToRgb(hue, saturation = 0.72, lightness = 0.56) {
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation
  const section = hue * 6
  const second = chroma * (1 - Math.abs((section % 2) - 1))
  let red = 0
  let green = 0
  let blue = 0
  if (section < 1) [red, green] = [chroma, second]
  else if (section < 2) [red, green] = [second, chroma]
  else if (section < 3) [green, blue] = [chroma, second]
  else if (section < 4) [green, blue] = [second, chroma]
  else if (section < 5) [red, blue] = [second, chroma]
  else [red, blue] = [chroma, second]
  const match = lightness - chroma / 2
  return [red, green, blue].map(value => Math.round((value + match) * 255))
}

function buildLabelLut(labelPayload) {
  const labelMap = labelPayload?.label_map || labelPayload || {}
  const entries = Object.entries(labelMap)
    .map(([id, name]) => [Number(id), String(name)])
    .filter(([id]) => Number.isInteger(id) && id >= 0)
    .sort((left, right) => left[0] - right[0])

  const colorMap = { R: [], G: [], B: [], A: [], I: [], labels: [] }
  labelNames.clear()
  for (const [id, rawName] of entries) {
    const name = rawName.replace(/^totalseg:/, '')
    labelNames.set(id, name)
    const [red, green, blue] = id === 0
      ? [0, 0, 0]
      : hslToRgb((id * 0.61803398875) % 1)
    colorMap.R.push(red)
    colorMap.G.push(green)
    colorMap.B.push(blue)
    colorMap.A.push(id === 0 ? 0 : 255)
    colorMap.I.push(id)
    colorMap.labels.push(name)
  }
  return cmapper.makeLabelLut(colorMap)
}

function updateLocation(location) {
  const mm = Array.from(location?.mm || []).slice(0, 3)
  locationText.value = mm.length === 3
    ? `十字线：${mm.map(value => Number(value).toFixed(1)).join(', ')} mm`
    : '十字线位置：-'

  const values = location?.values || []
  const ctValue = values[0]?.value
  intensityText.value = Number.isFinite(Number(ctValue)) ? Number(ctValue).toFixed(1) : ''
  const maskValue = Number(values[1]?.value || 0)
  labelText.value = maskValue > 0 ? (labelNames.get(maskValue) || String(maskValue)) : ''
}

async function initializeViewer() {
  loading.value = true
  errorMessage.value = ''
  locationText.value = '十字线位置：-'
  intensityText.value = ''
  labelText.value = ''
  niivue?.cleanup()
  niivue = null

  try {
    const labelPayload = await getLabelMap(caseId)
    const labelLut = buildLabelLut(labelPayload)
    niivue = new Niivue({
      sliceType: SLICE_TYPE.MULTIPLANAR,
      multiplanarLayout: embedded ? MULTIPLANAR_TYPE.COLUMN : MULTIPLANAR_TYPE.ROW,
      multiplanarShowRender: SHOW_RENDER.NEVER,
      multiplanarEqualSize: true,
      multiplanarPadPixels: 8,
      backColor: [0.004, 0.012, 0.022, 1],
      crosshairColor: [0.12, 0.96, 0.78, 1],
      crosshairWidth: 1,
      show3Dcrosshair: true,
      isColorbar: false,
      isOrientCube: false,
      isOrientationTextVisible: true,
      showAllOrientationMarkers: true,
      dragMode: DRAG_MODE.contrast,
      dragAndDropEnabled: false,
      logLevel: 'warn'
    })
    niivue.onLocationChange = updateLocation
    await niivue.attachToCanvas(canvasRef.value)
    await niivue.loadVolumes([
      {
        url: getCaseCtUrl(caseId),
        name: 'ct.nii.gz',
        colormap: 'gray',
        opacity: 1,
        trustCalMinMax: false,
        percentileFrac: 0.02
      }
    ])
    const maskVolume = await niivue.addVolumeFromUrl({
      url: getMaskUrl(caseId),
      name: 'totalsegmentator.nii.gz',
      opacity: overlayOpacity.value,
      cal_min: Number(labelLut.min || 0),
      cal_max: Number(labelLut.max || 1),
      trustCalMinMax: true,
      ignoreZeroVoxels: true,
      alphaThreshold: true,
      colormapLabel: labelLut
    })
    maskVolume.colormapLabel = labelLut
    niivue.updateGLVolume()
    niivue.setSliceType(SLICE_TYPE.MULTIPLANAR)
    niivue.setMultiplanarLayout(embedded ? MULTIPLANAR_TYPE.COLUMN : MULTIPLANAR_TYPE.ROW)
    niivue.setMultiplanarPadPixels(8)
    loading.value = false
  } catch (error) {
    loading.value = false
    errorMessage.value = error?.response?.data?.detail || error?.message || 'NiiVue 阅片器加载失败'
  }
}

function resetViewer() {
  if (!niivue) return
  niivue.scene.crosshairPos = [0.5, 0.5, 0.5]
  niivue.setPan2Dxyzmm([0, 0, 0, 1])
  niivue.setSliceType(SLICE_TYPE.MULTIPLANAR)
  niivue.drawScene()
}

function go3D() {
  router.push(`/cases/${caseId}/3d`)
}

function goBack() {
  router.push(`/cases/${caseId}`)
}

watch(overlayVisible, visible => {
  if (niivue?.volumes?.length > 1) {
    niivue.setOpacity(1, visible ? overlayOpacity.value : 0)
  }
})

watch(overlayOpacity, opacity => {
  if (overlayVisible.value && niivue?.volumes?.length > 1) {
    niivue.setOpacity(1, opacity)
  }
})

onMounted(initializeViewer)
onBeforeUnmount(() => niivue?.cleanup())
</script>

<style scoped>
.slice-viewer-page {
  min-height: 100%;
}

.embedded-viewer {
  height: 100%;
  min-height: 0;
  padding: 0;
}

.viewer-card {
  border-color: rgba(45, 212, 191, 0.2);
}

.embedded-viewer .viewer-card {
  height: 100%;
  border-color: #1e293b;
  background: #020617;
  box-shadow: 0 18px 48px rgba(2, 6, 23, 0.18);
}

.embedded-viewer .viewer-card :deep(.el-card__body) {
  height: 100%;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
}

.embedded-viewer .viewer-toolbar {
  flex-wrap: wrap;
  gap: 10px 14px;
}

.embedded-viewer .opacity-control {
  order: 3;
  width: 100%;
}

.embedded-viewer .viewer-engine {
  margin-left: auto;
}

.viewer-toolbar {
  min-height: 42px;
  display: flex;
  align-items: center;
  gap: 24px;
  margin-bottom: 12px;
}

.toolbar-control,
.viewer-engine,
.viewer-status {
  display: flex;
  align-items: center;
  gap: 9px;
  color: var(--ppgl-muted);
}

.opacity-control {
  width: min(360px, 36vw);
}

.opacity-control :deep(.el-slider) {
  flex: 1;
}

.opacity-control strong {
  min-width: 42px;
  color: var(--ppgl-text);
  font-variant-numeric: tabular-nums;
}

.viewer-engine {
  margin-left: auto;
  font-size: 13px;
}

.pane-title {
  color: #e2e8f0;
  white-space: nowrap;
}

.engine-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #2dd4bf;
  box-shadow: 0 0 8px rgba(45, 212, 191, 0.75);
}

.viewer-host {
  height: min(70vh, 780px);
  min-height: 500px;
  position: relative;
  overflow: hidden;
  border: 1px solid rgba(45, 212, 191, 0.18);
  border-radius: 8px;
  background: #010408;
}

.niivue-canvas {
  width: 100%;
  height: 100%;
  display: block;
  outline: none;
}

.embedded-viewer .viewer-host {
  height: auto;
  min-height: 0;
  flex: 1;
}

.embedded-viewer .viewer-status {
  color: #94a3b8;
}

.state-layer {
  position: absolute;
  inset: 0;
  z-index: 4;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: #d7e4ef;
  background: rgba(1, 4, 8, 0.88);
}

.state-layer span {
  color: var(--ppgl-muted);
}

.error-state {
  color: #fca5a5;
}

.viewer-status {
  min-height: 30px;
  flex-wrap: wrap;
  padding-top: 10px;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.viewer-status span + span::before {
  content: '·';
  margin-right: 9px;
}

.interaction-help {
  margin-left: auto;
}

@media (max-width: 900px) {
  .viewer-toolbar {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }

  .opacity-control {
    width: 100%;
  }

  .viewer-engine,
  .interaction-help {
    margin-left: 0;
  }

  .viewer-host {
    height: 66vh;
    min-height: 460px;
  }
}
</style>
