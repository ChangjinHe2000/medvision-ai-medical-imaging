<template>
  <div ref="wrapper" class="model-viewer">
    <div ref="host" class="canvas-host"></div>

    <div v-if="modelReady" class="viewer-tools">
      <el-button circle size="small" title="重置视角" @click="resetView">
        <el-icon><Refresh /></el-icon>
      </el-button>
      <el-button circle size="small" title="全屏查看" @click="toggleFullscreen">
        <el-icon><FullScreen /></el-icon>
      </el-button>
    </div>

    <div v-if="loading || errorMessage" class="viewer-state">
      <p>{{ loading ? '正在加载三维模型…' : errorMessage }}</p>
      <div v-if="!loading" class="state-actions">
        <el-button @click="load">重新加载</el-button>
        <el-button v-if="canGenerate" type="primary" :loading="generating" @click="$emit('generate')">
          生成三维模型
        </el-button>
      </div>
    </div>

    <div v-if="modelReady && regions.length" class="region-legend">
      <label v-for="item in regions" :key="item.key" :class="{ disabled: item.empty }">
        <input
          v-model="visibility[item.key]"
          type="checkbox"
          :disabled="item.empty"
          @change="setRegionVisibility(item)"
        />
        <i :style="{ background: item.color }"></i>
        <span>{{ shortRegionName(item) }}</span>
      </label>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { FullScreen, Refresh } from '@element-plus/icons-vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { getMeshFileUrl, getMeshManifest } from '../api/gliomaApi'
import { apiErrorMessage } from '../api/errors'

const props = defineProps({
  caseId: {
    type: String,
    required: true
  },
  canGenerate: {
    type: Boolean,
    default: false
  },
  generating: {
    type: Boolean,
    default: false
  },
  reloadKey: {
    type: Number,
    default: 0
  }
})

defineEmits(['generate'])

const wrapper = ref(null)
const host = ref(null)
const loading = ref(false)
const errorMessage = ref('')
const modelReady = ref(false)
const regions = ref([])
const visibility = reactive({ edema: true, net: true, et: true })
let scene
let camera
let renderer
let controls
let animationId
let model
let modelBounds
let resizeObserver

function init() {
  if (renderer || !host.value) return
  scene = new THREE.Scene()
  scene.background = new THREE.Color(0x02070d)
  camera = new THREE.PerspectiveCamera(45, host.value.clientWidth / host.value.clientHeight, 0.1, 5000)
  camera.position.set(0, 0, 180)
  renderer = new THREE.WebGLRenderer({ antialias: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.setSize(host.value.clientWidth, host.value.clientHeight)
  renderer.outputColorSpace = THREE.SRGBColorSpace
  host.value.appendChild(renderer.domElement)

  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  scene.add(new THREE.HemisphereLight(0xffffff, 0x183044, 2.4))
  const directionalLight = new THREE.DirectionalLight(0xffffff, 2.2)
  directionalLight.position.set(100, 120, 100)
  scene.add(directionalLight)

  const animate = () => {
    animationId = requestAnimationFrame(animate)
    controls.update()
    renderer.render(scene, camera)
  }
  animate()
}

function disposeModel() {
  if (!model) return
  scene?.remove(model)
  model.traverse(node => {
    node.geometry?.dispose?.()
    if (Array.isArray(node.material)) node.material.forEach(material => material.dispose?.())
    else node.material?.dispose?.()
  })
  model = null
  modelBounds = null
}

function setRegionVisibility(item) {
  const node = model?.getObjectByName(item.node_name)
  if (node) node.visible = visibility[item.key]
}

function resetView() {
  if (!modelBounds || !camera || !controls) return
  const size = modelBounds.getSize(new THREE.Vector3())
  const center = modelBounds.getCenter(new THREE.Vector3())
  const span = Math.max(size.x, size.y, size.z, 1)
  controls.target.copy(center)
  camera.position.set(center.x, center.y, center.z + span * 2.2)
  camera.near = Math.max(span / 1000, 0.01)
  camera.far = span * 20
  camera.updateProjectionMatrix()
  controls.update()
}

function resize() {
  if (!renderer || !camera || !host.value) return
  const width = Math.max(host.value.clientWidth, 1)
  const height = Math.max(host.value.clientHeight, 1)
  camera.aspect = width / height
  camera.updateProjectionMatrix()
  renderer.setSize(width, height)
}

async function toggleFullscreen() {
  if (!wrapper.value) return
  if (document.fullscreenElement) await document.exitFullscreen()
  else await wrapper.value.requestFullscreen()
  window.setTimeout(resize, 80)
}

async function load() {
  if (!props.caseId) return
  loading.value = true
  errorMessage.value = ''
  modelReady.value = false
  try {
    await nextTick()
    init()
    const manifest = await getMeshManifest(props.caseId)
    regions.value = manifest.mesh?.regions || []
    const response = await fetch(`${getMeshFileUrl(props.caseId)}?t=${Date.now()}`)
    if (!response.ok) throw new Error(`三维模型下载失败：HTTP ${response.status}`)
    const buffer = await response.arrayBuffer()
    const gltf = await new Promise((resolve, reject) => new GLTFLoader().parse(buffer, '', resolve, reject))
    disposeModel()
    model = gltf.scene
    scene.add(model)
    regions.value.forEach(setRegionVisibility)
    modelBounds = new THREE.Box3().setFromObject(model)
    if (modelBounds.isEmpty()) throw new Error('分割结果为空，当前没有可显示的三维表面')
    resetView()
    modelReady.value = true
  } catch (error) {
    disposeModel()
    errorMessage.value = apiErrorMessage(error, '三维模型尚未生成')
  } finally {
    loading.value = false
  }
}

function shortRegionName(item) {
  return ({ edema: 'ED', net: 'NET', et: 'ET' }[item.key] || item.display_name || item.key)
}

watch(() => props.reloadKey, () => load())

onMounted(() => {
  resizeObserver = new ResizeObserver(resize)
  if (host.value) resizeObserver.observe(host.value)
  document.addEventListener('fullscreenchange', resize)
  load()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  document.removeEventListener('fullscreenchange', resize)
  if (animationId) cancelAnimationFrame(animationId)
  disposeModel()
  controls?.dispose()
  renderer?.dispose()
  renderer?.domElement?.remove()
})

defineExpose({ load, resetView })
</script>

<style scoped>
.model-viewer {
  position: relative;
  min-height: 440px;
  overflow: hidden;
  border-radius: 8px;
  background: #02070d;
}

.model-viewer:fullscreen {
  width: 100vw;
  height: 100vh;
  border-radius: 0;
}

.canvas-host {
  position: absolute;
  inset: 0;
}

.viewer-tools {
  position: absolute;
  z-index: 3;
  top: 12px;
  right: 12px;
  display: flex;
  gap: 7px;
}

.viewer-state {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: rgba(2, 7, 13, 0.88);
  color: var(--ppgl-muted);
  text-align: center;
}

.state-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 10px;
}

.region-legend {
  position: absolute;
  z-index: 3;
  left: 12px;
  bottom: 12px;
  display: flex;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 8px;
  background: rgba(3, 11, 18, 0.82);
  backdrop-filter: blur(8px);
}

.region-legend label {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #cddce6;
  font-size: 12px;
  cursor: pointer;
}

.region-legend label.disabled {
  opacity: 0.4;
}

.region-legend input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.region-legend i {
  width: 9px;
  height: 9px;
  border-radius: 2px;
  box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.08);
}

.region-legend input:not(:checked) + i {
  background: #526373 !important;
}

@media (max-width: 700px) {
  .model-viewer {
    min-height: 360px;
  }
}
</style>

