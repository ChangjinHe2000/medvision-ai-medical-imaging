<template>
  <div class="page-shell case-list-page">
    <div class="page-header">
      <div>
        <h2>病例管理</h2>
        <p>统一管理 CT 与 MRI 病例，并进入对应的分割和阅片流程</p>
      </div>
      <div class="page-header-actions">
        <el-button :icon="Refresh" @click="loadCases">刷新</el-button>
        <el-button v-if="canEdit" :icon="UploadFilled" @click="goUpload('ct')">上传 CT</el-button>
        <el-button v-if="canEdit" type="primary" :icon="UploadFilled" @click="goUpload('mri')">上传 MRI</el-button>
      </div>
    </div>

    <section class="summary-grid">
      <article>
        <span>全部病例</span>
        <strong>{{ caseSummary.total }}</strong>
        <small>CT 与 MRI</small>
      </article>
      <article>
        <span>CT 病例</span>
        <strong>{{ caseSummary.ct }}</strong>
        <small>全器官 / PPGL</small>
      </article>
      <article>
        <span>MRI 病例</span>
        <strong>{{ caseSummary.mri }}</strong>
        <small>脑胶质瘤</small>
      </article>
      <article>
        <span>进行中</span>
        <strong>{{ caseSummary.active }}</strong>
        <small>排队或分割中</small>
      </article>
    </section>

    <el-card class="case-table-card" shadow="never">
      <div class="table-toolbar">
        <el-radio-group v-model="typeFilter" class="type-filter" @change="changeTypeFilter">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="ct">CT</el-radio-button>
          <el-radio-button value="mri">MRI</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="keyword"
          :prefix-icon="Search"
          clearable
          placeholder="搜索病例编号"
          class="case-search"
        />
      </div>

      <el-table
        v-loading="loading"
        :data="filteredCases"
        :row-key="rowKey"
        empty-text="暂无符合条件的病例"
        style="width: 100%"
      >
        <el-table-column label="影像类型" width="105">
          <template #default="{ row }">
            <el-tag :type="row.imaging_type === 'mri' ? 'success' : 'primary'">
              {{ row.imaging_type === 'mri' ? 'MRI' : 'CT' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="病例编号" min-width="220">
          <template #default="{ row }">
            <div class="case-identity">
              <strong>{{ row.display_name || row.case_id }}</strong>
              <small v-if="row.display_name">{{ row.case_id }}</small>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="上传时间" width="178">
          <template #default="{ row }">{{ formatUploadTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="分析任务" width="215">
          <template #default="{ row }">
            <div v-if="row.imaging_type === 'ct'" class="task-statuses">
              <span>
                全器官
                <el-tag size="small" :type="getStatusType(row.organ_status?.status)">
                  {{ getStatusText(row.organ_status?.status) }}
                </el-tag>
              </span>
              <span>
                PPGL
                <el-tag size="small" :type="getStatusType(row.ppgl_status?.status)">
                  {{ getStatusText(row.ppgl_status?.status) }}
                </el-tag>
              </span>
            </div>
            <div v-else class="task-statuses">
              <span>
                脑胶质瘤
                <el-tag size="small" :type="getStatusType(row.status)">
                  {{ getStatusText(row.status) }}
                </el-tag>
              </span>
              <span>
                MRI 序列
                <el-tag size="small" :type="row.upload_complete ? 'success' : 'info'">
                  {{ row.uploaded_modalities?.length || 0 }}/4
                </el-tag>
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="进度" width="180">
          <template #default="{ row }">
            <div v-if="row.imaging_type === 'ct'" class="task-progresses">
              <span>全器官 <el-progress :percentage="row.organ_status?.progress || 0" :show-text="false" /></span>
              <span>PPGL <el-progress :percentage="row.ppgl_status?.progress || 0" :show-text="false" /></span>
            </div>
            <div v-else class="mri-progress">
              <el-progress :percentage="row.progress || 0" />
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态说明" min-width="260">
          <template #default="{ row }">
            <div v-if="row.imaging_type === 'ct'" class="task-messages">
              <span>全器官：{{ getCtTaskMessage('organ', row.organ_status) }}</span>
              <span>PPGL：{{ getCtTaskMessage('ppgl', row.ppgl_status) }}</span>
            </div>
            <span v-else>{{ getGliomaMessage(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" min-width="650" fixed="right">
          <template #default="{ row }">
            <div class="action-buttons">
              <template v-if="row.imaging_type === 'ct'">
                <el-button size="small" :icon="View" @click="goDetail(row)">结果</el-button>
                <el-button v-if="canEdit" size="small" :icon="Edit" :disabled="isCaseActive(row)" @click="renameCaseDisplayName(row)">
                  重命名
                </el-button>
                <el-button
                  v-if="canEdit && canStartCtTask(row.organ_status?.status)"
                  size="small"
                  :icon="VideoPlay"
                  @click="segmentOrgans(row)"
                >
                  {{ row.organ_status?.status === 'failed' ? '重试全器官' : '全器官' }}
                </el-button>
                <el-button
                  v-if="canEdit && canStartCtTask(row.ppgl_status?.status)"
                  size="small"
                  type="warning"
                  :icon="VideoPlay"
                  @click="segmentPpgl(row)"
                >
                  {{ row.ppgl_status?.status === 'failed' ? '重试 PPGL' : 'PPGL' }}
                </el-button>
                <el-button size="small" type="primary" :icon="DataAnalysis" @click="go3D(row)">联合阅片</el-button>
                <el-button size="small" type="success" :icon="Document" @click="goReport(row)">报告</el-button>
              </template>

              <template v-else>
                <el-button size="small" :icon="View" @click="goDetail(row)">结果</el-button>
                <el-button v-if="canEdit" size="small" :icon="Edit" :disabled="isCaseActive(row)" @click="renameCaseDisplayName(row)">重命名</el-button>
                <el-button
                  v-if="canEdit && !row.upload_complete"
                  size="small"
                  type="primary"
                  :icon="UploadFilled"
                  @click="continueGliomaUpload(row)"
                >
                  继续上传
                </el-button>
                <el-button
                  v-if="canEdit && canStartGlioma(row)"
                  size="small"
                  type="success"
                  :icon="VideoPlay"
                  :loading="startingGliomaCase === row.case_id"
                  @click="segmentGlioma(row)"
                >
                  {{ row.status === 'failed' ? '重试分割' : '开始分割' }}
                </el-button>
                <el-button
                  v-if="canEdit && isGliomaInferenceActive(row.status)"
                  size="small"
                  type="warning"
                  plain
                  @click="cancelGlioma(row)"
                >
                  取消分割
                </el-button>
              </template>

              <el-button
                v-if="canEdit"
                size="small"
                type="danger"
                :icon="Delete"
                :disabled="isDeleteProtected(row)"
                @click="removeCase(row)"
              >
                删除
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import {
  DataAnalysis,
  Delete,
  Document,
  Edit,
  Refresh,
  Search,
  UploadFilled,
  VideoPlay,
  View
} from '@element-plus/icons-vue'
import {
  TOTALSEGMENTATOR_PARAMS,
  deleteCase as deleteCtCase,
  getCaseList as getCtCaseList,
  startOrganSegmentation,
  startPpglSegmentation,
  updateCaseId
} from '../api/caseApi'
import {
  cancelSegmentation as cancelGliomaSegmentation,
  deleteCase as deleteGliomaCase,
  listCases as getGliomaCaseList,
  renameCase as renameGliomaCase,
  startSegmentation as startGliomaSegmentation
} from '../api/gliomaApi'
import { apiErrorMessage } from '../api/errors'
import { authStore } from '../store/authStore.js'

const route = useRoute()
const router = useRouter()
const cases = ref([])
const loading = ref(false)
const keyword = ref('')
const typeFilter = ref(normalizeTypeFilter(route.query.type))
const startingGliomaCase = ref('')
let pollTimer = null
const canEdit = computed(() => authStore.state.user?.role !== 'viewer')

const filteredCases = computed(() => {
  const query = keyword.value.trim().toLowerCase()
  return cases.value.filter(row => {
    const typeMatches = typeFilter.value === 'all' || row.imaging_type === typeFilter.value
    const keywordMatches = !query || [row.case_id, row.display_name]
      .some(value => String(value || '').toLowerCase().includes(query))
    return typeMatches && keywordMatches
  })
})

const caseSummary = computed(() => ({
  total: cases.value.length,
  ct: cases.value.filter(row => row.imaging_type === 'ct').length,
  mri: cases.value.filter(row => row.imaging_type === 'mri').length,
  active: cases.value.filter(isCaseActive).length
}))

function normalizeTypeFilter(value) {
  const normalized = String(value || '').toLowerCase()
  return ['ct', 'mri'].includes(normalized) ? normalized : 'all'
}

function changeTypeFilter(value) {
  const query = { ...route.query }
  if (value === 'all') delete query.type
  else query.type = value
  router.replace({ path: '/cases', query })
}

function getStatusType(status) {
  if (status === 'completed') return 'success'
  if (['failed', 'invalid'].includes(status)) return 'danger'
  if (['queued', 'running', 'uploading', 'validating'].includes(status)) return 'warning'
  return 'info'
}

function getStatusText(status) {
  return ({
    created: '待上传',
    uploading: '上传中',
    validating: '校验中',
    uploaded: '已上传',
    queued: '排队中',
    running: '分割中',
    completed: '已完成',
    invalid: '校验失败',
    failed: '失败',
    cancelled: '已取消'
  })[status] || status || '待启动'
}

function getCtTaskMessage(task, taskStatus) {
  const status = taskStatus?.status
  if (task === 'organ') {
    if (status === 'completed') return '全器官分割完成'
    if (status === 'running') return '正在进行全器官分割'
    if (status === 'queued') return '全器官分割任务已提交'
    if (status === 'failed') return '全器官分割失败，可点击重试'
    return taskStatus?.message || '等待启动全器官分割'
  }
  if (status === 'completed') return '肿瘤分割完成'
  if (status === 'running') return '正在进行肿瘤分割'
  if (status === 'queued') return '肿瘤分割任务已提交'
  if (status === 'failed') return '肿瘤分割失败，可点击重试'
  return taskStatus?.message || '等待启动肿瘤分割'
}

function getGliomaMessage(row) {
  const status = row.status
  if (status === 'completed') return '脑胶质瘤分割完成'
  if (status === 'running') return '正在进行脑胶质瘤分割'
  if (status === 'queued') return '脑胶质瘤分割任务已提交'
  if (status === 'failed') return '脑胶质瘤分割失败，可点击重试'
  if (status === 'invalid') return '四序列校验失败，请重新上传'
  if (!row.upload_complete) return `已上传 ${row.uploaded_modalities?.length || 0}/4 个 MRI 序列`
  return row.message || '等待启动脑胶质瘤分割'
}

function isTaskActive(status) {
  return ['queued', 'running'].includes(status)
}

function isGliomaInferenceActive(status) {
  return ['queued', 'running'].includes(status)
}

function isCaseActive(row) {
  if (row.imaging_type === 'mri') return isGliomaInferenceActive(row.status)
  return isTaskActive(row.organ_status?.status) || isTaskActive(row.ppgl_status?.status)
}

function isDeleteProtected(row) {
  if (row.imaging_type === 'mri') {
    return ['uploading', 'validating', 'queued', 'running'].includes(row.status)
  }
  return isCaseActive(row)
}

function canStartCtTask(status) {
  return !status || status === 'uploaded' || status === 'failed'
}

function canStartGlioma(row) {
  return row.upload_complete && ['uploaded', 'failed'].includes(row.status)
}

function clearPoll() {
  if (!pollTimer) return
  clearInterval(pollTimer)
  pollTimer = null
}

function ensurePolling() {
  if (pollTimer) return
  pollTimer = setInterval(() => loadCases(true), 3000)
}

function rowKey(row) {
  return `${row.imaging_type}:${row.case_id}`
}

function timestamp(value) {
  const parsed = Date.parse(String(value || '').replace(' ', 'T'))
  return Number.isFinite(parsed) ? parsed : 0
}

function formatUploadTime(value) {
  const parsed = timestamp(value)
  if (!parsed) return value || '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  }).format(parsed)
}

async function loadCases(silent = false) {
  if (!silent) loading.value = true
  try {
    const [ctResult, gliomaResult] = await Promise.all([getCtCaseList(), getGliomaCaseList()])
    const ctCases = (ctResult.cases || []).map(item => ({ ...item, imaging_type: 'ct' }))
    const gliomaCases = (gliomaResult.cases || []).map(item => ({ ...item, imaging_type: 'mri' }))
    cases.value = [...ctCases, ...gliomaCases]
      .sort((left, right) => timestamp(right.created_at) - timestamp(left.created_at))
    if (cases.value.some(isCaseActive)) ensurePolling()
    else clearPoll()
  } catch (error) {
    if (!silent) ElMessage.error(apiErrorMessage(error, '病例列表加载失败'))
  } finally {
    if (!silent) loading.value = false
  }
}

function goUpload(type) {
  router.push(type === 'mri' ? '/glioma/upload' : '/upload')
}

function caseRoute(caseId) {
  return encodeURIComponent(caseId)
}

async function renameCaseDisplayName(row) {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的病例显示名称。内部病例编号不会改变。', '重命名病例', {
      customClass: 'rename-case-message-box',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: row.display_name || '未命名病例',
      inputPattern: /^[^/\\]+$/,
      inputErrorMessage: '显示名称不能为空，且不能包含 / 或 \\'
    })
    const displayName = value.trim()
    if (!displayName) return ElMessage.warning('病例显示名称不能为空')
    if (row.imaging_type === 'mri') await renameGliomaCase(row.case_id, displayName)
    else await updateCaseId(row.case_id, displayName)
    ElMessage.success('病例已重命名')
    await loadCases()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(apiErrorMessage(error, '修改病例编号失败'))
  }
}

async function segmentOrgans(row) {
  const retry = row.organ_status?.status === 'failed'
  try {
    await startOrganSegmentation(row.case_id, { ...TOTALSEGMENTATOR_PARAMS, force: retry })
    ElMessage.success(retry ? '已重新提交全器官分割' : '已启动全器官分割')
    await loadCases()
    goDetail(row)
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '启动全器官分割失败'))
  }
}

async function segmentPpgl(row) {
  const retry = row.ppgl_status?.status === 'failed'
  try {
    await startPpglSegmentation(row.case_id, { device: 'cuda:0', force: retry })
    ElMessage.success(retry ? '已重新提交 PPGL 肿瘤分割' : '已启动 PPGL 肿瘤分割')
    await loadCases()
    goDetail(row)
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '启动 PPGL 分割失败'))
  }
}

async function segmentGlioma(row) {
  startingGliomaCase.value = row.case_id
  try {
    await startGliomaSegmentation(row.case_id)
    ElMessage.success('脑胶质瘤分割任务已提交')
    await loadCases()
    goDetail(row)
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '脑胶质瘤分割任务提交失败'))
  } finally {
    startingGliomaCase.value = ''
  }
}

async function cancelGlioma(row) {
  try {
    await cancelGliomaSegmentation(row.case_id)
    ElMessage.success('脑胶质瘤分割任务已取消')
    await loadCases()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '取消脑胶质瘤分割失败'))
  }
}

async function removeCase(row) {
  const typeName = row.imaging_type === 'mri' ? 'MRI' : 'CT'
  try {
    await ElMessageBox.confirm(
      `删除 ${typeName} 病例“${row.display_name || row.case_id}”后将移入回收站，可由管理员恢复。`,
      '确认删除病例',
      { confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning' }
    )
    if (row.imaging_type === 'mri') await deleteGliomaCase(row.case_id)
    else await deleteCtCase(row.case_id)
    ElMessage.success('病例已删除')
    await loadCases()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(apiErrorMessage(error, '删除病例失败'))
  }
}

function continueGliomaUpload(row) {
  router.push({ path: '/glioma/upload', query: { case: row.case_id } })
}

function goDetail(row) {
  const path = row.imaging_type === 'mri'
    ? `/glioma/cases/${caseRoute(row.case_id)}`
    : `/cases/${caseRoute(row.case_id)}`
  router.push(path)
}

function go3D(row) {
  router.push(`/cases/${caseRoute(row.case_id)}/3d`)
}

function goReport(row) {
  router.push(`/cases/${caseRoute(row.case_id)}/report`)
}

watch(
  () => route.query.type,
  value => { typeFilter.value = normalizeTypeFilter(value) }
)

onMounted(() => loadCases())
onUnmounted(clearPoll)
</script>

<style scoped>
.case-list-page { min-height: 100%; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }
.summary-grid article { padding: 16px 18px; border: 1px solid rgba(93,235,219,.18); border-radius: var(--ppgl-radius); background: rgba(8,23,36,.78); }
.summary-grid span, .summary-grid small { display: block; color: var(--ppgl-muted); }
.summary-grid strong { display: block; margin: 7px 0 3px; color: var(--ppgl-text); font-size: 26px; }
.case-table-card { border-radius: var(--ppgl-radius); border-color: rgba(93,235,219,.22); background: rgba(8,23,36,.78); }
.table-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.type-filter { padding: 3px; border: 1px solid rgba(93,235,219,.22); border-radius: 8px; background: rgba(4,16,27,.72); }
.type-filter :deep(.el-radio-button__inner) { min-width: 58px; border: 0 !important; border-radius: 6px !important; background: transparent; color: var(--ppgl-muted); box-shadow: none !important; font-weight: 650; transition: color .18s ease, background-color .18s ease; }
.type-filter :deep(.el-radio-button__inner:hover) { background: rgba(93,235,219,.08); color: #b9f8f1; }
.type-filter :deep(.el-radio-button.is-active .el-radio-button__inner),
.type-filter :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) { background: rgba(32,224,196,.18); color: #d5fffa; box-shadow: inset 0 0 0 1px rgba(93,235,219,.32) !important; }
.case-search { width: min(320px, 100%); }
.case-identity { display: grid; gap: 3px; }
.case-identity strong { color: var(--ppgl-text); word-break: break-all; }
.case-identity small { color: var(--ppgl-muted); word-break: break-all; }
.task-statuses, .task-progresses, .task-messages { display: grid; gap: 7px; }
.task-statuses span { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.task-progresses span { display: grid; grid-template-columns: 48px 1fr; align-items: center; gap: 7px; font-size: 12px; }
.mri-progress { padding-right: 8px; }
.action-buttons { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }
.action-buttons :deep(.el-button + .el-button) { margin-left: 0; }
:deep(.el-progress-bar__outer) { background-color: rgba(93,235,219,.12); }
:deep(.el-progress-bar__inner) { background: linear-gradient(90deg, var(--ppgl-primary), var(--ppgl-accent)); box-shadow: 0 0 16px rgba(32,224,196,.34); }
@media (max-width: 1000px) { .summary-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 700px) { .summary-grid { grid-template-columns: 1fr; } .table-toolbar { align-items: stretch; flex-direction: column; } .case-search { width: 100%; } }
</style>
