<template>
  <div class="clinical-preview">
    <div class="preview-header">
      <span>CT Preview</span>
      <strong>{{ imageUrl ? 'REAL CASE SLICE' : statusText }}</strong>
    </div>
    <div class="preview-body">
      <div class="scan-plane">
        <template v-if="imageUrl">
          <img class="real-slice" :src="imageUrl" :alt="caseId ? `Case ${caseId} preview` : 'Case preview'" />
          <div class="real-slice-overlay">
            <span>{{ caseId || 'Latest case' }}</span>
            <span>{{ sliceTitle || 'Key slice' }}</span>
          </div>
        </template>

        <template v-else>
          <div class="body-outline"></div>
          <div class="spine"></div>
          <div class="kidney kidney-left"></div>
          <div class="kidney kidney-right"></div>
          <div class="aorta"></div>
          <div class="segmentation tumor"></div>
          <div class="scan-line"></div>
          <div class="preview-legend">
            <span><i class="kidney-dot"></i>Organ</span>
            <span><i class="tumor-dot"></i>Tumor</span>
          </div>
        </template>

        <div v-if="loading" class="preview-state">
          正在加载病例切片
        </div>
      </div>
      <div class="preview-scale">
        <span></span>
        <span></span>
        <span></span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  imageUrl: {
    type: String,
    default: ''
  },
  caseId: {
    type: String,
    default: ''
  },
  sliceTitle: {
    type: String,
    default: ''
  },
  loading: {
    type: Boolean,
    default: false
  }
})

const statusText = computed(() => (props.loading ? 'LOADING' : 'SEGMENTATION READY'))
</script>

<style scoped>
.clinical-preview {
  min-width: 320px;
  padding: 14px;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-radius: 8px;
  background: rgba(5, 16, 26, 0.48);
}

.preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  color: #7f94a7;
  font-size: 11px;
  font-weight: 800;
}

.preview-header strong {
  color: #8de8dd;
  font-size: 11px;
}

.preview-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 42px;
  gap: 12px;
}

.scan-plane {
  position: relative;
  height: 184px;
  border-radius: 8px;
  background:
    linear-gradient(90deg, rgba(148, 163, 184, 0.08) 1px, transparent 1px),
    linear-gradient(180deg, rgba(148, 163, 184, 0.06) 1px, transparent 1px),
    radial-gradient(ellipse at center, rgba(148, 163, 184, 0.11), transparent 62%),
    #071522;
  background-size: 22px 22px;
  overflow: hidden;
}

.real-slice {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #020711;
}

.real-slice-overlay {
  position: absolute;
  left: 10px;
  right: 10px;
  bottom: 10px;
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #dbe7f0;
  font-size: 11px;
  font-weight: 800;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.75);
}

.preview-state {
  position: absolute;
  inset: 0;
  background: rgba(5, 16, 26, 0.72);
  color: #8de8dd;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 800;
}

.scan-plane::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, transparent, rgba(32, 224, 196, 0.08), transparent);
  transform: translateY(-100%);
  animation: preview-scan 4.2s ease-in-out infinite;
}

.body-outline,
.spine,
.kidney,
.aorta,
.segmentation {
  position: absolute;
}

.body-outline {
  left: 16%;
  right: 16%;
  top: 21%;
  height: 58%;
  border: 1px solid rgba(226, 232, 240, 0.28);
  border-radius: 48% 48% 42% 42%;
  background: rgba(148, 163, 184, 0.08);
}

.spine {
  left: 48.5%;
  top: 61%;
  width: 30px;
  height: 22px;
  border: 1px solid rgba(226, 232, 240, 0.42);
  border-radius: 10px;
  background: rgba(226, 232, 240, 0.18);
}

.kidney {
  width: 76px;
  height: 46px;
  border: 1px solid rgba(125, 211, 252, 0.56);
  background: rgba(56, 189, 248, 0.13);
  box-shadow: inset 0 0 18px rgba(125, 211, 252, 0.08);
}

.kidney-left {
  left: 31%;
  top: 40%;
  border-radius: 52% 42% 48% 45%;
  transform: rotate(-10deg);
}

.kidney-right {
  right: 28%;
  top: 39%;
  border-radius: 42% 52% 45% 48%;
  transform: rotate(10deg);
}

.aorta {
  left: 51%;
  top: 45%;
  width: 14px;
  height: 14px;
  border: 1px solid rgba(248, 113, 113, 0.58);
  border-radius: 999px;
  background: rgba(248, 113, 113, 0.18);
}

.tumor {
  right: 31%;
  top: 43%;
  width: 26px;
  height: 24px;
  border-radius: 999px;
  border: 2px solid rgba(255, 209, 102, 0.9);
  background: rgba(255, 209, 102, 0.3);
  box-shadow: 0 0 18px rgba(255, 209, 102, 0.34);
}

.scan-line {
  position: absolute;
  left: 8%;
  right: 8%;
  top: 50%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(32, 224, 196, 0.72), transparent);
  opacity: 0.78;
}

.preview-legend {
  position: absolute;
  left: 12px;
  bottom: 10px;
  display: flex;
  gap: 12px;
  color: #8ea3b4;
  font-size: 11px;
  font-weight: 700;
}

.preview-legend span {
  display: flex;
  align-items: center;
  gap: 5px;
}

.preview-legend i {
  width: 8px;
  height: 8px;
  border-radius: 999px;
}

.kidney-dot {
  background: #38bdf8;
}

.tumor-dot {
  background: #ffd166;
}

.preview-scale {
  display: grid;
  align-content: space-between;
}

.preview-scale span {
  display: block;
  height: 1px;
  background: rgba(148, 163, 184, 0.36);
}

@keyframes preview-scan {
  0%,
  100% {
    transform: translateY(-100%);
  }
  50% {
    transform: translateY(100%);
  }
}
</style>
