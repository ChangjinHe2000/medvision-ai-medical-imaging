<template>
  <div class="page-shell report-page">
    <header class="page-header">
      <div><h2>辅助分析报告</h2><p>病例内部编号：{{ caseId }}</p></div>
      <div class="page-header-actions">
        <el-button @click="goBack">返回病例</el-button>
        <el-dropdown v-if="reportMeta" @command="download"><el-button>下载报告</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="markdown">Markdown</el-dropdown-item><el-dropdown-item command="json">JSON</el-dropdown-item><el-dropdown-item command="pdf">PDF</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
        <el-button type="primary" :loading="generating" @click="createReport">{{ reportMeta ? '重新生成' : '生成报告' }}</el-button>
      </div>
    </header>
    <el-alert title="自动分割、定量指标和 AI 文本仅用于影像辅助分析，必须由医生结合完整原始影像与临床资料复核。" type="warning" show-icon :closable="false" class="warning" />

    <el-card v-if="reportMeta" class="section-card summary-card" shadow="never">
      <template #header><div class="section-header"><div><strong>确定性定量结果</strong><p>由分割与量化程序生成，大模型不参与数值计算</p></div><el-tag>{{ reportMeta.imaging_type }}</el-tag></div></template>
      <div class="metric-grid">
        <div v-for="(value, key) in metrics" :key="key" class="metric-item"><span>{{ metricLabel(key) }}</span><strong>{{ metricValue(value) }}</strong></div>
      </div>
      <div class="meta-line">输入：{{ reportMeta.inputs?.join('、') }}　模板版本：{{ reportMeta.template_version }}　生成时间：{{ reportMeta.generated_at }}</div>
    </el-card>

    <div class="report-grid">
      <el-card class="section-card report-card" v-loading="loading" shadow="never">
        <template #header><div class="section-header"><div><strong>统一报告正文</strong><p>自动结果、规则说明、AI 辅助和医生复核分区展示</p></div></div></template>
        <div v-if="report" class="report-content" v-html="renderMarkdown(report)"></div>
        <el-empty v-else description="报告尚未生成，请点击右上角生成报告" />
      </el-card>

      <el-card class="section-card review-card" shadow="never">
        <template #header><div class="section-header"><div><strong>医生复核</strong><p>医生、日期与复核意见将写入审计记录</p></div></div></template>
        <el-form label-position="top" :model="review">
          <el-form-item label="复核意见"><el-input v-model="review.opinion" type="textarea" :rows="8" maxlength="4000" show-word-limit placeholder="请输入影像复核意见，不得填写患者身份信息" /></el-form-item>
          <el-form-item label="复核医生"><el-input v-model.trim="review.doctor_name" maxlength="80" /></el-form-item>
          <el-form-item label="复核日期"><el-date-picker v-model="review.review_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
          <el-button v-if="canReview" type="primary" :loading="saving" @click="saveReview">保存复核记录</el-button>
          <el-alert v-else title="当前账号为只读角色，不能提交复核记录。" type="info" :closable="false" />
        </el-form>
      </el-card>
    </div>

    <el-card class="section-card assistant-card" shadow="never">
      <template #header><div class="section-header"><div><strong>病例上下文助手</strong><p>只读取本报告的脱敏结构化指标，重点提示复核方向与就诊路径</p></div><el-tag type="success">Ollama 真流式</el-tag></div></template>
      <div ref="chatBox" class="chat-list">
        <el-empty v-if="!messages.length" description="可询问影像负荷、复核重点、建议补充资料或就诊科室路径" />
        <div v-for="item in messages" :key="item.id" class="chat-row" :class="item.role"><span>{{ item.role === 'user' ? '我' : '助手' }}</span><div>{{ item.content }}<i v-if="item.streaming" class="cursor"></i></div></div>
      </div>
      <el-input v-model="question" type="textarea" :rows="3" maxlength="4000" show-word-limit placeholder="请勿输入患者姓名、病例号、手机号或本机路径" :disabled="sending" @keydown.ctrl.enter.prevent="sendQuestion" />
      <div class="assistant-actions"><span>按 Ctrl + Enter 发送；对话按当前账号保留，退出登录时清除。</span><el-button v-if="sending" @click="stop">停止生成</el-button><el-button type="primary" :disabled="!question.trim() || !reportMeta" :loading="sending" @click="sendQuestion">发送</el-button></div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import { useRoute, useRouter } from 'vue-router'
import { apiErrorMessage } from '../api/errors.js'
import { generateReport, getReport, getReportFileUrl, getReportJson, saveReportReview, streamCaseAssistant } from '../api/reportApi.js'
import { authStore } from '../store/authStore.js'
import { conversationStore } from '../store/conversationStore.js'

const route = useRoute(), router = useRouter(), markdown = new MarkdownIt({ html: false, breaks: true, linkify: false })
const caseId = String(route.params.caseId), report = ref(''), reportMeta = ref(null), loading = ref(false), generating = ref(false), saving = ref(false)
const question = ref(''), sending = ref(false), chatBox = ref(null)
const review = reactive({ opinion: '', doctor_name: authStore.state.user?.display_name || '', review_date: new Date().toISOString().slice(0, 10) })
const userId = computed(() => authStore.state.user?.id || 'anonymous')
const messages = computed(() => conversationStore.caseMessages(userId.value, caseId))
const metrics = computed(() => reportMeta.value?.sections?.deterministic_metrics || {})
const canReview = computed(() => ['admin', 'doctor'].includes(authStore.state.user?.role))
let controller = null

const labels = { edema_ml: '水肿 ED 体积', net_ml: '非增强肿瘤 NET 体积', et_ml: '增强肿瘤 ET 体积', tc_ml: '肿瘤核心 TC 体积', wt_ml: '全肿瘤 WT 体积', maximum_3d_diameter_mm: '最大三维径', lesion_count: '病灶数量', tumor_volume_ml: 'PPGL 体积', maximum_diameter_mm: 'PPGL 最大径', side: '空间侧别', centroid_mm: '中心坐标', organ_relations: '器官关系', organ_count: '解剖结构数量' }
const metricLabel = key => labels[key] || key
function metricValue(value) { if (value === null || value === undefined) return '未生成'; if (Array.isArray(value)) return value.join('、'); if (typeof value === 'object') return JSON.stringify(value); return String(value) }
const renderMarkdown = value => markdown.render(String(value || ''))
function scrollBottom() { nextTick(() => { if (chatBox.value) chatBox.value.scrollTop = chatBox.value.scrollHeight }) }

async function loadReport() {
  loading.value = true
  try {
    const [text, json] = await Promise.all([getReport(caseId), getReportJson(caseId)])
    report.value = text; reportMeta.value = json
    if (json.review) Object.assign(review, { opinion: json.review.opinion || '', doctor_name: json.review.doctor_name || '', review_date: json.review.review_date || review.review_date })
  } catch (error) {
    report.value = ''; reportMeta.value = null
    if (error?.response?.status !== 404) ElMessage.error(apiErrorMessage(error, '报告加载失败'))
  } finally { loading.value = false }
}
async function createReport() { generating.value = true; try { await generateReport(caseId); await loadReport(); ElMessage.success('统一报告已生成') } catch (error) { ElMessage.error(apiErrorMessage(error, '报告生成失败')) } finally { generating.value = false } }
async function saveReview() { if (!review.opinion.trim() || !review.doctor_name || !review.review_date) return ElMessage.warning('请完整填写复核意见、医生和日期'); saving.value = true; try { await saveReportReview(caseId, { ...review, opinion: review.opinion.trim() }); ElMessage.success('复核记录已保存'); await loadReport() } catch (error) { ElMessage.error(apiErrorMessage(error, '复核记录保存失败')) } finally { saving.value = false } }
function download(kind) { window.open(getReportFileUrl(caseId, kind), '_blank', 'noopener') }

async function sendQuestion() {
  const text = question.value.trim(); if (!text || sending.value || !reportMeta.value) return
  const history = messages.value.map(item => ({ role: item.role, content: item.content })).slice(-10)
  const user = { id: conversationStore.nextMessageId(), role: 'user', content: text }
  const answer = { id: conversationStore.nextMessageId(), role: 'assistant', content: '', streaming: true }
  messages.value.push(user, answer); question.value = ''; sending.value = true; controller = new AbortController(); scrollBottom()
  try {
    await streamCaseAssistant(caseId, { question: text, history, max_tokens: 900 }, { signal: controller.signal, onDelta: delta => { answer.content += delta; scrollBottom() } })
  } catch (error) {
    if (error?.name !== 'AbortError') { if (!answer.content) answer.content = `生成失败：${apiErrorMessage(error)}`; ElMessage.error(apiErrorMessage(error, '病例助手回答失败')) }
  } finally { answer.streaming = false; sending.value = false; controller = null; conversationStore.persist(userId.value); scrollBottom() }
}
function stop() { controller?.abort() }
function goBack() { router.back() }
onMounted(() => { loadReport(); scrollBottom() })
</script>

<style scoped>
.warning,.summary-card,.report-grid{margin-bottom:16px}.summary-card,.report-card,.review-card,.assistant-card{border-color:var(--ppgl-border)}.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}.metric-item{padding:13px;border:1px solid var(--ppgl-border);border-radius:8px;background:rgba(4,15,25,.45)}.metric-item span{display:block;color:var(--ppgl-muted);font-size:12px;margin-bottom:5px}.metric-item strong{word-break:break-word}.meta-line{margin-top:12px;color:var(--ppgl-muted);font-size:12px}.report-grid{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(330px,.55fr);gap:16px}.report-content{min-height:450px;padding:20px;border:1px solid var(--ppgl-border);border-radius:8px;background:rgba(4,15,25,.55);line-height:1.75}.report-content :deep(h1),.report-content :deep(h2){color:var(--ppgl-text);margin:18px 0 10px}.report-content :deep(p),.report-content :deep(ul),.report-content :deep(ol){margin:0 0 12px}.review-card{align-self:start}.chat-list{min-height:160px;max-height:390px;overflow:auto;margin-bottom:14px;padding:12px;border:1px solid var(--ppgl-border);border-radius:8px}.chat-row{display:grid;grid-template-columns:52px minmax(0,1fr);gap:8px;margin-bottom:12px}.chat-row>span{color:var(--ppgl-muted);font-weight:700}.chat-row>div{white-space:pre-wrap;line-height:1.7;padding:10px 12px;border-radius:8px;background:rgba(106,169,255,.1)}.chat-row.user>div{background:var(--ppgl-primary-soft)}.assistant-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:10px}.assistant-actions span{margin-right:auto;color:var(--ppgl-muted);font-size:12px}.cursor{display:inline-block;width:2px;height:1em;margin-left:3px;background:var(--ppgl-primary);animation:blink .8s infinite}@keyframes blink{50%{opacity:0}}@media(max-width:1050px){.report-grid{grid-template-columns:1fr}}
</style>
