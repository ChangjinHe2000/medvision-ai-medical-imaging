<template>
  <section class="page-shell gpu-workbench-page">
    <div class="page-heading">
      <div>
        <div class="eyebrow">本地 GPU 资源调度</div>
        <h2>GPU 工作台</h2>
        <p>监控本地显存、模型驻留状态与影像分割队列。显存不足时，分割任务会先释放本地语言模型并等待可用资源。</p>
      </div>
      <div class="heading-actions">
        <span class="updated-at">
          <strong>自动更新已开启</strong>
          · {{ refreshCadence }} · 最近成功同步 {{ formatTime(lastSuccessfulAt || data.generated_at) }}
        </span>
        <el-button :loading="manualRefreshing" @click="refresh">立即刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="!gpu.available"
      type="warning"
      :closable="false"
      show-icon
      :title="`未读取到 NVIDIA GPU 遥测：${gpu.reason || '请确认 nvidia-smi 可用'}`"
    />

    <section class="runtime-strip" :class="`runtime-strip--${activityTone}`">
      <div class="runtime-primary">
        <span class="runtime-dot"></span>
        <div>
          <span class="runtime-label">GPU 当前状态</span>
          <strong>{{ activity.label || '正在读取状态' }}</strong>
          <p>{{ activity.detail || '正在从本地 GPU 和任务队列读取运行信息。' }}</p>
        </div>
      </div>
      <div class="runtime-meta">
        <span v-if="activity.case_id">病例：{{ activity.case_id }}</span>
        <span v-else-if="activity.model">模型：{{ activity.model }}</span>
        <span>下次同步 {{ nextRefreshSeconds }} 秒</span>
      </div>
    </section>

    <el-row :gutter="16" class="metric-grid">
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never" class="metric-card">
          <div class="metric-label">显存总量</div>
          <div class="metric-value">{{ formatMemory(primary.memory_total_mb) }}</div>
          <div class="metric-note">{{ primary.name || 'GPU 未连接' }}</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never" class="metric-card">
          <div class="metric-label">已用显存</div>
          <div class="metric-value">{{ formatMemory(primary.memory_used_mb) }}</div>
          <el-progress :percentage="usagePercent" :stroke-width="8" :show-text="false" />
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never" class="metric-card">
          <div class="metric-label">可用显存</div>
          <div class="metric-value emphasis">{{ formatMemory(primary.memory_free_mb) }}</div>
          <div class="metric-note">GPU 利用率 {{ primary.utilization_percent ?? '—' }}%</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never" class="metric-card">
          <div class="metric-label">分割队列</div>
          <div class="metric-value">{{ data.summary?.active ?? 0 }}</div>
          <div class="metric-note">历史成功率 {{ formatPercent(data.summary?.success_rate) }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="content-row">
      <el-col :xs="24" :lg="14">
        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="card-title-row">
              <span>运行中的本地模型</span>
              <el-tag type="success" effect="plain">{{ data.models?.length || 0 }} 个模型驻留</el-tag>
            </div>
          </template>
          <el-empty v-if="!data.models?.length" description="当前没有 Ollama 模型驻留" :image-size="74" />
          <div v-else class="model-list">
            <div v-for="model in data.models" :key="model.name" class="model-item">
              <div>
                <strong>{{ model.name }}</strong>
                <span>预计显存占用 {{ formatMemory(model.size_vram_mb) }}</span>
              </div>
              <el-tag type="info" effect="plain">已驻留</el-tag>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="10">
        <el-card shadow="never" class="panel-card policy-card">
          <template #header><span>显存准入策略</span></template>
          <p>{{ data.capacity_policy?.admission_rule || '正在读取策略。' }}</p>
          <div class="policy-list">
            <div v-for="item in estimateRows" :key="item.key">
              <span>{{ item.label }}</span>
              <strong>需 {{ formatMemory(item.memory) }} + {{ formatMemory(data.capacity_policy?.system_reserve_mb) }} 余量</strong>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel-card trend-card">
      <template #header>
        <div class="card-title-row">
          <span>近 {{ data.telemetry?.window_minutes || 30 }} 分钟显存趋势</span>
          <span class="muted">{{ data.telemetry?.sample_interval_seconds || 5 }} 秒采样一次，采样数据保存在本机运行目录</span>
        </div>
      </template>
      <el-empty
        v-if="telemetrySamples.length < 2"
        description="正在积累显存趋势数据，请保持系统运行一小段时间"
        :image-size="66"
      />
      <div v-else ref="trendHost" class="trend-chart"></div>
    </el-card>

    <el-card shadow="never" class="panel-card">
      <template #header>
        <div class="card-title-row">
          <span>任务队列</span>
          <span class="muted">影像分割任务按提交顺序获取 GPU；知识库问答可在显存满足时并行运行。</span>
        </div>
      </template>
      <el-table :data="data.active_tasks || []" empty-text="当前没有等待或运行中的分割任务">
        <el-table-column label="队列" width="76">
          <template #default="scope">#{{ scope.row.queue_position }}</template>
        </el-table-column>
        <el-table-column prop="task_label" label="任务类型" min-width="130" />
        <el-table-column prop="case_id" label="病例编号" min-width="170" show-overflow-tooltip />
        <el-table-column label="状态" width="130">
          <template #default="scope"><el-tag :type="statusType(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="预计显存" width="130">
          <template #default="scope">{{ formatMemory(scope.row.estimated_vram_mb) }}</template>
        </el-table-column>
        <el-table-column label="已等待" width="130">
          <template #default="scope">{{ formatDuration(scope.row.queue_age_seconds) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="panel-card">
      <template #header><span>最近任务指标</span></template>
      <el-table :data="data.recent_tasks || []" empty-text="尚无已完成的 GPU 分割任务">
        <el-table-column prop="task_label" label="任务类型" min-width="130" />
        <el-table-column prop="case_id" label="病例编号" min-width="170" show-overflow-tooltip />
        <el-table-column label="结果" width="110">
          <template #default="scope"><el-tag :type="statusType(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="队列等待" width="130">
          <template #default="scope">{{ formatDuration(scope.row.queue_wait_seconds) }}</template>
        </el-table-column>
        <el-table-column label="推理耗时" width="130">
          <template #default="scope">{{ formatDuration(scope.row.run_seconds) }}</template>
        </el-table-column>
        <el-table-column label="显存占用（运行前 → 结束）" min-width="210">
          <template #default="scope">
            {{ formatMemory(scope.row.gpu_before?.primary?.memory_used_mb) }} → {{ formatMemory(scope.row.gpu_after?.primary?.memory_used_mb) }}
          </template>
        </el-table-column>
        <el-table-column label="提交时间" min-width="170">
          <template #default="scope">{{ formatTime(scope.row.submitted_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { useGpuRuntimeStatus } from '../composables/useGpuRuntimeStatus'

const {
  status: data,
  lastSuccessfulAt,
  nextRefreshSeconds,
  hasLiveWork,
  refreshNow
} = useGpuRuntimeStatus()

const manualRefreshing = ref(false)
const trendHost = ref(null)
let trendChart = null

const gpu = computed(() => data.value.gpu || {})
const primary = computed(() => gpu.value.primary || {})
const activity = computed(() => data.value.activity || {})
const telemetrySamples = computed(() => data.value.telemetry?.samples || [])
const refreshCadence = computed(() => hasLiveWork.value ? '任务进行中，每 3 秒同步' : '空闲状态，每 10 秒同步')
const activityTone = computed(() => ({
  running: 'running',
  waiting_for_gpu: 'waiting',
  queued: 'waiting',
  ready: 'ready',
  idle: 'idle'
}[activity.value.state] || 'idle'))
const usagePercent = computed(() => {
  const total = Number(primary.value.memory_total_mb || 0)
  const used = Number(primary.value.memory_used_mb || 0)
  return total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0
})
const estimateRows = computed(() => {
  const values = data.value.capacity_policy?.task_estimates_mb || {}
  return [
    { key: 'organ', label: '全器官分割', memory: values.organ },
    { key: 'ppgl', label: 'PPGL 肿瘤分割', memory: values.ppgl },
    { key: 'glioma', label: '脑胶质瘤分割', memory: values.glioma }
  ]
})

function formatMemory(value) {
  const memory = Number(value)
  if (!Number.isFinite(memory) || memory < 0) return '—'
  return memory >= 1024 ? `${(memory / 1024).toFixed(1)} GB` : `${memory} MB`
}

function formatDuration(value) {
  const seconds = Number(value)
  if (!Number.isFinite(seconds)) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`
}

function formatPercent(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '—'
}

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function statusText(status) {
  return {
    queued: '排队中',
    waiting_for_gpu: '等待显存',
    running: '运行中',
    completed: '完成',
    failed: '失败',
    cancelled: '已取消'
  }[status] || status || '未知'
}

function statusType(status) {
  return {
    queued: 'warning',
    waiting_for_gpu: 'warning',
    running: 'primary',
    completed: 'success',
    failed: 'danger',
    cancelled: 'info'
  }[status] || 'info'
}

async function refresh(silent = false) {
  manualRefreshing.value = true
  try {
    await refreshNow()
  } catch (error) {
    if (!silent) ElMessage.error(error?.response?.data?.detail || 'GPU 工作台数据读取失败')
  } finally {
    manualRefreshing.value = false
  }
}

function formatTrendTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

function renderTrend() {
  if (trendChart && trendChart.getDom() !== trendHost.value) {
    trendChart.dispose()
    trendChart = null
  }
  if (!trendHost.value || telemetrySamples.value.length < 2) return
  if (!trendChart) trendChart = echarts.init(trendHost.value)
  const samples = telemetrySamples.value
  trendChart.setOption({
    animationDuration: 260,
    backgroundColor: 'transparent',
    color: ['#20e0c4', '#6aa9ff'],
    grid: { left: 50, right: 22, top: 40, bottom: 36 },
    legend: {
      top: 2,
      textStyle: { color: '#b9f8f1', fontSize: 12 },
      data: ['已用显存', '可用显存']
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(7, 22, 34, 0.96)',
      borderColor: 'rgba(93, 235, 219, 0.35)',
      textStyle: { color: '#e9fbff' },
      valueFormatter: value => `${Number(value || 0).toFixed(1)} GB`
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: samples.map(item => formatTrendTime(item.captured_at)),
      axisLine: { lineStyle: { color: 'rgba(142, 169, 189, 0.28)' } },
      axisLabel: { color: '#8ea9bd', fontSize: 11, hideOverlap: true }
    },
    yAxis: {
      type: 'value',
      name: 'GB',
      nameTextStyle: { color: '#8ea9bd', padding: [0, 0, 0, -28] },
      axisLine: { show: false },
      axisLabel: { color: '#8ea9bd', formatter: value => `${value} GB` },
      splitLine: { lineStyle: { color: 'rgba(93, 235, 219, 0.1)' } }
    },
    series: [
      {
        name: '已用显存',
        type: 'line',
        smooth: true,
        showSymbol: false,
        areaStyle: { color: 'rgba(32, 224, 196, 0.12)' },
        data: samples.map(item => Number(item.memory_used_mb || 0) / 1024)
      },
      {
        name: '可用显存',
        type: 'line',
        smooth: true,
        showSymbol: false,
        areaStyle: { color: 'rgba(106, 169, 255, 0.07)' },
        data: samples.map(item => Number(item.memory_free_mb || 0) / 1024)
      }
    ]
  }, true)
}

function resizeTrend() {
  trendChart?.resize()
}

onMounted(() => {
  refresh(true)
  window.addEventListener('resize', resizeTrend)
})

watch(telemetrySamples, () => {
  nextTick(renderTrend)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeTrend)
  trendChart?.dispose()
  trendChart = null
})
</script>

<style scoped>
.gpu-workbench-page {
  display: grid;
  gap: 16px;
  max-width: 1560px;
  min-height: 100%;
  margin: 0 auto;
}

.page-heading, .card-title-row, .model-item, .heading-actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-heading { align-items: flex-start; }
.eyebrow { color: #7fb4ad; font-size: 12px; font-weight: 800; letter-spacing: .08em; }
h2 { margin: 5px 0 7px; font-size: 28px; color: var(--ppgl-text); }
p, .metric-note, .muted, .updated-at { color: var(--ppgl-muted); line-height: 1.6; }
.page-heading p { max-width: 760px; margin: 0; }
.heading-actions { flex-wrap: wrap; justify-content: flex-end; }
.updated-at strong { color: #b9f8f1; font-weight: 750; }
.runtime-strip {
  min-height: 74px;
  padding: 14px 16px;
  border: 1px solid rgba(93, 235, 219, 0.24);
  border-radius: var(--ppgl-radius);
  background: linear-gradient(90deg, rgba(13, 55, 66, 0.66), rgba(8, 23, 36, 0.72));
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}
.runtime-primary { min-width: 0; display: flex; align-items: center; gap: 12px; }
.runtime-dot {
  width: 10px;
  height: 10px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: var(--ppgl-primary);
  box-shadow: 0 0 0 5px rgba(32, 224, 196, 0.1);
}
.runtime-primary > div { min-width: 0; display: grid; gap: 2px; }
.runtime-label { color: var(--ppgl-muted); font-size: 12px; }
.runtime-primary strong { color: var(--ppgl-text); font-size: 16px; }
.runtime-primary p { overflow: hidden; max-width: 780px; margin: 0; text-overflow: ellipsis; white-space: nowrap; }
.runtime-meta { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; color: #b9f8f1; font-size: 12px; }
.runtime-meta span { padding: 5px 8px; border: 1px solid rgba(93, 235, 219, 0.16); border-radius: 999px; background: rgba(6, 17, 29, 0.36); }
.runtime-strip--running { border-color: rgba(106, 169, 255, 0.42); background: linear-gradient(90deg, rgba(24, 55, 94, 0.67), rgba(8, 23, 36, 0.72)); }
.runtime-strip--running .runtime-dot { background: var(--ppgl-accent); box-shadow: 0 0 0 5px rgba(106, 169, 255, 0.1), 0 0 16px rgba(106, 169, 255, 0.72); }
.runtime-strip--waiting { border-color: rgba(255, 209, 102, 0.45); background: linear-gradient(90deg, rgba(73, 55, 20, 0.52), rgba(8, 23, 36, 0.72)); }
.runtime-strip--waiting .runtime-dot { background: var(--ppgl-warning); box-shadow: 0 0 0 5px rgba(255, 209, 102, 0.1), 0 0 16px rgba(255, 209, 102, 0.62); }
.runtime-strip--ready .runtime-dot { background: var(--ppgl-success); box-shadow: 0 0 0 5px rgba(77, 241, 161, 0.1); }
.metric-grid, .content-row { margin: 0 !important; }
.metric-card, .panel-card { border-color: var(--ppgl-border); }
.metric-label { color: var(--ppgl-muted); font-size: 13px; }
.metric-value { margin: 8px 0; color: var(--ppgl-text); font-size: 28px; font-weight: 760; }
.metric-value.emphasis { color: var(--ppgl-primary); }
.metric-note { min-height: 22px; font-size: 13px; }
.panel-card { margin-top: 0; }
.card-title-row { color: var(--ppgl-text); font-weight: 750; }
.model-list, .policy-list { display: grid; gap: 10px; }
.model-item, .policy-list > div {
  padding: 10px 12px;
  border: 1px solid rgba(93, 235, 219, 0.16);
  border-radius: 8px;
  background: var(--ppgl-panel);
}
.model-item > div { display: grid; gap: 3px; }
.model-item strong { color: var(--ppgl-text); }
.model-item span { color: var(--ppgl-muted); font-size: 13px; }
.policy-card p { margin-top: 0; }
.policy-list > div { align-items: center; display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
.policy-list span { color: var(--ppgl-text); }
.policy-list strong { color: #b9f8f1; }
.trend-card { overflow: hidden; }
.trend-chart { width: 100%; height: 270px; }
@media (max-width: 768px) {
  .page-heading, .card-title-row, .policy-list > div, .runtime-strip { align-items: flex-start; flex-direction: column; }
  .heading-actions { justify-content: flex-start; }
  .runtime-meta { justify-content: flex-start; }
  .runtime-primary p { white-space: normal; }
}
</style>
