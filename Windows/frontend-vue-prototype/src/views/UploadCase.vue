<template>
  <div class="page-shell upload-page">
    <div class="page-header">
      <div>
        <h2>上传病例</h2>
        <p>上传 CT NIfTI 文件后，可独立启动全器官分割或 PPGL 肿瘤分割</p>
      </div>
    </div>

    <el-row :gutter="20">
      <el-col :xs="24" :lg="16">
        <el-card class="upload-card" shadow="never">
          <template #header>
            <div class="section-header">
              <div>
                <strong>CT 文件</strong>
                <p>建议上传 .nii.gz 格式影像文件</p>
              </div>
            </div>
          </template>

          <el-alert
            title="当前 MVP 版本建议先上传 .nii.gz 格式 CT 文件，DICOM 文件夹后续再支持。"
            type="info"
            show-icon
            class="tip-alert"
          />

          <el-upload
            ref="uploadRef"
            drag
            action="#"
            accept=".nii.gz"
            :auto-upload="false"
            :on-change="handleFileChange"
            class="upload-box"
          >
            <el-icon class="el-icon--upload">
              <UploadFilled />
            </el-icon>
            <div class="el-upload__text">
              将 CT 文件拖到此处，或 <em>点击选择</em>
            </div>
            <template #tip>
              <div class="el-upload__tip">
                支持 .nii.gz 文件
              </div>
            </template>
          </el-upload>

          <div v-if="selectedFile" class="file-strip">
            <span class="file-dot"></span>
            <div>
              <strong>{{ selectedFile.name }}</strong>
              <p>{{ uploadMessage }}</p>
            </div>
          </div>

          <div class="actions">
            <el-button
              type="primary"
              :disabled="uploading"
              :loading="uploading"
              @click="handleUploadButton"
            >
              {{ selectedFile ? '上传病例' : '选择 CT 文件' }}
            </el-button>
            <el-button
              :disabled="!caseId || organSegmenting"
              :loading="organSegmenting"
              @click="startOrganTask"
            >
              启动全器官分割
            </el-button>
            <el-button
              type="warning"
              :disabled="!caseId || ppglSegmenting"
              :loading="ppglSegmenting"
              @click="startPpglTask"
            >
              启动 PPGL 分割
            </el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="8">
        <el-card class="status-card" shadow="never">
          <template #header>
            <div class="section-header">
              <div>
                <strong>任务状态</strong>
                <p>上传完成后生成病例编号</p>
              </div>
            </div>
          </template>

          <div class="progress-box">
            <div class="progress-title">
              {{ uploadMessage }}
            </div>
            <el-progress
              :percentage="uploadProgress"
              :status="uploadProgressStatus"
            />
          </div>

          <el-descriptions v-if="caseId" :column="1" border class="result-box">
            <el-descriptions-item label="病例编号">
              {{ caseId }}
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              {{ statusText }}
            </el-descriptions-item>
          </el-descriptions>

          <div v-else class="empty-status">
            请选择 CT 文件并上传
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { UploadFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import {
  TOTALSEGMENTATOR_PARAMS,
  startOrganSegmentation,
  startPpglSegmentation,
  uploadCT
} from '../api/caseApi'

const router = useRouter()

const uploadRef = ref(null)
const selectedFile = ref(null)
const caseId = ref('')
const uploading = ref(false)
const organSegmenting = ref(false)
const ppglSegmenting = ref(false)
const statusText = ref('')
const uploadProgress = ref(0)
const uploadMessage = ref('等待上传')
const uploadFailed = ref(false)
const uploadProgressStatus = computed(() => {
  if (uploadFailed.value) return 'exception'
  if (caseId.value && uploadProgress.value === 100) return 'success'
  return undefined
})

function handleFileChange(file) {
  selectedFile.value = file
  caseId.value = ''
  statusText.value = ''
  uploadProgress.value = 0
  uploadMessage.value = '等待上传'
  uploadFailed.value = false
}

function handleUploadButton() {
  if (selectedFile.value) {
    uploadCase()
    return
  }
  uploadRef.value?.$el?.querySelector('input[type="file"]')?.click()
}

async function uploadCase() {
  if (!selectedFile.value?.raw) {
    ElMessage.warning('请先选择 .nii.gz 文件')
    return
  }
  uploading.value = true
  uploadProgress.value = 0
  uploadMessage.value = '正在上传 CT 文件'
  uploadFailed.value = false
  try {
    const res = await uploadCT(selectedFile.value.raw, event => {
      if (!event.total) return
      uploadProgress.value = Math.min(99, Math.round((event.loaded * 100) / event.total))
    })
    uploadProgress.value = 100
    caseId.value = res.case_id
    statusText.value = res.message || 'CT 文件上传成功'
    uploadMessage.value = '上传完成'
    ElMessage.success(`病例上传成功：${res.case_id}`)
  } catch (err) {
    uploadFailed.value = true
    uploadMessage.value = err?.code === 'ECONNABORTED' ? '上传超时' : '上传失败'
    ElMessage.error(err?.response?.data?.detail || err?.message || '病例上传失败')
  } finally {
    uploading.value = false
  }
}

async function startOrganTask() {
  if (!caseId.value) return
  organSegmenting.value = true
  try {
    await startOrganSegmentation(caseId.value, TOTALSEGMENTATOR_PARAMS)
    ElMessage.success('已启动全器官分割')
    router.push(`/cases/${caseId.value}`)
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || '启动全器官分割失败')
  } finally {
    organSegmenting.value = false
  }
}

async function startPpglTask() {
  if (!caseId.value) return
  ppglSegmenting.value = true
  try {
    await startPpglSegmentation(caseId.value, { device: 'cuda:0' })
    ElMessage.success('已启动 PPGL 肿瘤分割')
    router.push(`/cases/${caseId.value}`)
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || '启动 PPGL 分割失败')
  } finally {
    ppglSegmenting.value = false
  }
}
</script>

<style scoped>
.upload-page {
  min-height: 100%;
}

.tip-alert {
  margin-bottom: 20px;
}

.upload-box {
  margin-top: 12px;
}

.upload-box :deep(.el-upload-dragger) {
  position: relative;
  border-radius: var(--ppgl-radius);
  border-color: rgba(93, 235, 219, 0.32);
  background:
    linear-gradient(90deg, rgba(93, 235, 219, 0.08) 1px, transparent 1px),
    linear-gradient(180deg, rgba(93, 235, 219, 0.06) 1px, transparent 1px),
    rgba(5, 16, 26, 0.7);
  background-size: 22px 22px;
  padding: 48px 24px;
  overflow: hidden;
  color: var(--ppgl-text);
}

.upload-box :deep(.el-upload-dragger::after) {
  content: "";
  position: absolute;
  left: -40%;
  top: 0;
  width: 40%;
  height: 100%;
  background: linear-gradient(90deg, transparent, rgba(32, 224, 196, 0.14), transparent);
  animation: upload-scan 3.8s linear infinite;
}

.upload-box :deep(.el-icon--upload) {
  color: var(--ppgl-primary);
  filter: drop-shadow(0 0 14px rgba(32, 224, 196, 0.34));
}

.upload-box :deep(.el-upload__text) {
  color: var(--ppgl-text);
}

.upload-box :deep(.el-upload__tip) {
  color: var(--ppgl-muted);
}

.file-strip {
  margin-top: 18px;
  padding: 14px 16px;
  border: 1px solid rgba(93, 235, 219, 0.22);
  border-radius: var(--ppgl-radius);
  background: rgba(8, 23, 36, 0.72);
  display: flex;
  gap: 12px;
  align-items: center;
}

.file-strip strong {
  color: var(--ppgl-text);
  word-break: break-all;
}

.file-strip p {
  margin: 3px 0 0;
  color: var(--ppgl-muted);
  font-size: 13px;
}

.file-dot {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: var(--ppgl-primary);
  box-shadow: 0 0 0 5px var(--ppgl-primary-soft), 0 0 20px rgba(32, 224, 196, 0.36);
  flex: 0 0 auto;
}

.actions {
  margin-top: 24px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.progress-box {
  margin-top: 4px;
}

.progress-title {
  margin-bottom: 8px;
  color: var(--ppgl-muted);
  font-size: 14px;
}

.result-box {
  margin-top: 24px;
}

.status-card {
  min-height: 100%;
}

.empty-status {
  margin-top: 24px;
  padding: 18px;
  border-radius: var(--ppgl-radius);
  background: rgba(5, 16, 26, 0.58);
  border: 1px dashed rgba(93, 235, 219, 0.2);
  color: var(--ppgl-muted);
  text-align: center;
}

@keyframes upload-scan {
  from {
    transform: translateX(0);
  }
  to {
    transform: translateX(360%);
  }
}
</style>
