<template>
  <div class="page-shell dashboard-page">
    <section class="hero">
      <div><span class="kicker">医学影像智能工作站</span><h1>从 CT 或四序列 MRI 开始一次分析</h1><p>统一管理 PPGL CT 与脑肿瘤 MRI 病例，完成分割、量化、二维/三维阅片和医生复核。</p>
        <div class="hero-actions"><el-button v-if="canEdit" type="primary" size="large" @click="router.push('/upload')">上传 CT</el-button><el-button v-if="canEdit" size="large" @click="router.push('/glioma/upload')">上传 MRI</el-button><el-button size="large" @click="router.push('/cases')">病例管理</el-button></div>
      </div>
      <div class="capabilities"><div><small>CT 流程</small><strong>TotalSegmentator → ProgressPatchV5</strong></div><div><small>MRI 流程</small><strong>四序列 → nnU-Net</strong></div><div><small>分析结果</small><strong>指标 · 二维 · 三维 · 报告</strong></div></div>
    </section>

    <section class="stats">
      <article><strong>{{ summary.total }}</strong><span>累计病例</span></article><article><strong>{{ summary.ct }}</strong><span>PPGL CT</span></article><article><strong>{{ summary.mri }}</strong><span>脑肿瘤 MRI</span></article><article><strong>{{ summary.active }}</strong><span>处理中任务</span></article>
    </section>

    <section class="content-grid">
      <el-card class="section-card recent-card">
        <template #header><div class="section-header"><div><strong>最近病例</strong><p>真实病例与任务状态</p></div><el-button text type="primary" @click="router.push('/cases')">查看全部</el-button></div></template>
        <el-table :data="recentCases" v-loading="loading" empty-text="暂无病例">
          <el-table-column label="病例" min-width="250"><template #default="{ row }"><div class="case-name"><strong>{{ row.display_name || '未命名病例' }}</strong><small>{{ row.case_id }}</small></div></template></el-table-column>
          <el-table-column label="类型" width="130"><template #default="{ row }"><el-tag>{{ row.imaging_type === 'mri' ? '脑肿瘤 MRI' : 'PPGL CT' }}</el-tag></template></el-table-column>
          <el-table-column label="状态" width="120"><template #default="{ row }"><span class="status-dot" :class="statusClass(row)"></span>{{ statusText(row) }}</template></el-table-column>
          <el-table-column label="更新时间" min-width="180"><template #default="{ row }">{{ formatTime(row.updated_at || row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="100"><template #default="{ row }"><el-button size="small" @click="openCase(row)">打开</el-button></template></el-table-column>
        </el-table>
      </el-card>
      <aside class="side-stack">
        <el-card class="section-card"><template #header><div class="section-header"><strong>快捷入口</strong></div></template><button class="quick" @click="router.push('/assistant')">AI 医学助手<span>知识与工作辅助</span></button><button class="quick" @click="router.push('/messages')">站内私聊<span>账号间工作沟通</span></button></el-card>
        <el-card class="section-card"><template #header><div class="section-header"><strong>使用边界</strong></div></template><p class="notice">自动分割和大模型内容均为辅助结果。诊断、分级和治疗决策必须由具备资质的医生结合原始影像与临床资料完成。</p></el-card>
      </aside>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { getCaseList } from '../api/caseApi.js'
import { apiErrorMessage } from '../api/errors.js'
import { listCases as getMriCases } from '../api/gliomaApi.js'
import { authStore } from '../store/authStore.js'

const router = useRouter(), cases = ref([]), loading = ref(false)
const canEdit = computed(() => authStore.state.user?.role !== 'viewer')
const isActive = row => ['queued','running','uploading','validating'].includes(row.status) || ['queued','running'].includes(row.organ_status?.status) || ['queued','running'].includes(row.ppgl_status?.status)
const summary = computed(() => ({ total: cases.value.length, ct: cases.value.filter(row => row.imaging_type === 'ct').length, mri: cases.value.filter(row => row.imaging_type === 'mri').length, active: cases.value.filter(isActive).length }))
const recentCases = computed(() => cases.value.slice(0, 5))
const stamp = value => Date.parse(String(value || '').replace(' ', 'T')) || 0
const formatTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-'
function statusText(row) { if (isActive(row)) return '处理中'; if (row.status === 'completed' || row.organ_status?.status === 'completed' || row.ppgl_status?.status === 'completed') return '已完成'; if (row.status === 'failed' || row.organ_status?.status === 'failed' || row.ppgl_status?.status === 'failed') return '失败'; return '待处理' }
function statusClass(row) { return ({ '处理中': 'active', '已完成': 'done', '失败': 'failed' })[statusText(row)] || 'waiting' }
function openCase(row) { router.push(row.imaging_type === 'mri' ? `/glioma/cases/${encodeURIComponent(row.case_id)}` : `/cases/${encodeURIComponent(row.case_id)}`) }
async function load() { loading.value = true; try { const [ct,mri] = await Promise.all([getCaseList(), getMriCases()]); cases.value = [...(ct.cases || []), ...(mri.cases || [])].sort((a,b) => stamp(b.updated_at || b.created_at) - stamp(a.updated_at || a.created_at)) } catch (error) { ElMessage.error(apiErrorMessage(error, '首页病例加载失败')) } finally { loading.value = false } }
onMounted(load)
</script>

<style scoped>
.dashboard-page { max-width: 1780px; margin: 0 auto; }.hero { padding: 34px 38px; border: 1px solid var(--ppgl-border); border-radius: 12px; background: linear-gradient(120deg,rgba(15,118,110,.18),rgba(8,23,36,.86)); display:grid; grid-template-columns:1fr 420px; gap:36px; }.kicker { color:var(--ppgl-primary); font-weight:800; }.hero h1 { margin:8px 0; font-size:36px; }.hero p { color:var(--ppgl-muted); max-width:780px; }.hero-actions { display:flex; gap:10px; margin-top:20px; }.capabilities { border-left:1px solid var(--ppgl-border); padding-left:28px; display:grid; gap:15px; }.capabilities div { display:grid; }.capabilities small,.case-name small,.quick span { color:var(--ppgl-muted); }.stats { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:16px 0; }.stats article { padding:18px 22px; background:var(--ppgl-surface); border:1px solid var(--ppgl-border); border-radius:9px; display:flex; align-items:center; gap:18px; }.stats strong { font-size:28px; }.stats span { color:var(--ppgl-muted); }.content-grid { display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:16px; }.case-name { display:grid; }.status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px; background:var(--ppgl-warning); }.status-dot.done { background:var(--ppgl-success); }.status-dot.active { background:var(--ppgl-accent); }.status-dot.failed { background:var(--ppgl-danger); }.side-stack { display:grid; gap:16px; align-content:start; }.quick { width:100%; padding:12px 4px; border:0; border-bottom:1px solid var(--ppgl-border); background:transparent; color:var(--ppgl-text); text-align:left; cursor:pointer; display:grid; font-weight:700; }.notice { color:var(--ppgl-muted); line-height:1.75; margin:0; }
@media(max-width:1100px){.hero,.content-grid{grid-template-columns:1fr}.capabilities{border-left:0;border-top:1px solid var(--ppgl-border);padding:20px 0 0}.stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:650px){.hero{padding:24px}.hero h1{font-size:28px}.stats{grid-template-columns:1fr}.hero-actions{flex-wrap:wrap}}
</style>
