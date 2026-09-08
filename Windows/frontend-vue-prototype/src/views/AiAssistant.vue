<template>
  <div class="page-shell assistant-page">
    <header class="page-header">
      <div><h2>AI 医学助手</h2><p>用于医学知识梳理与工作辅助，不替代医生诊断和治疗决策。</p></div>
      <el-tag :type="healthType">{{ healthText }}</el-tag>
    </header>
    <el-card class="section-card chat-card" body-class="chat-body">
      <div ref="chatBox" class="chat-list">
        <div v-if="!messages.length" class="welcome">
          <strong>可以询问医学知识或影像分析流程</strong>
          <p>请勿输入患者姓名、手机号、证件号、住院号或其他身份信息。</p>
        </div>
        <div v-for="item in messages" :key="item.id" class="chat-row" :class="item.role">
          <span class="speaker">{{ item.role === 'user' ? '我' : '助手' }}</span>
          <div class="chat-bubble">{{ item.content }}<span v-if="item.streaming" class="cursor"></span></div>
        </div>
      </div>
      <div class="privacy-note">对话按当前账号保留；只有主动退出登录时才会清除该账号的对话。</div>
      <div class="composer">
        <el-input v-model="question" type="textarea" :rows="4" maxlength="4000" show-word-limit placeholder="输入问题，请勿包含患者身份信息" :disabled="sending" @keydown.ctrl.enter.prevent="send" />
        <div class="composer-actions"><span>按 Ctrl + Enter 发送</span><el-button v-if="sending" @click="stop">停止生成</el-button><el-button type="primary" :disabled="!question.trim()" :loading="sending" @click="send">发送</el-button></div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiErrorMessage } from '../api/errors.js'
import { getLlmHealth, streamGeneralChat } from '../api/llmApi.js'
import { authStore } from '../store/authStore.js'
import { conversationStore } from '../store/conversationStore.js'

const question = ref(''), sending = ref(false), health = ref('checking'), chatBox = ref(null)
let controller = null
const userId = computed(() => authStore.state.user?.id || 'anonymous')
const messages = computed(() => conversationStore.generalMessages(userId.value))
const healthText = computed(() => ({ checking: '正在检查大模型', ready: '大模型可用', unavailable: '大模型未就绪' }[health.value]))
const healthType = computed(() => health.value === 'ready' ? 'success' : health.value === 'checking' ? 'info' : 'warning')

function scrollBottom() { nextTick(() => { if (chatBox.value) chatBox.value.scrollTop = chatBox.value.scrollHeight }) }

async function send() {
  const text = question.value.trim()
  if (!text || sending.value) return
  const previous = messages.value.map(item => ({ role: item.role, content: item.content })).slice(-12)
  const userMessage = { id: conversationStore.nextMessageId(), role: 'user', content: text }
  const answer = { id: conversationStore.nextMessageId(), role: 'assistant', content: '', streaming: true }
  messages.value.push(userMessage, answer); question.value = ''; sending.value = true; scrollBottom()
  controller = new AbortController()
  try {
    await streamGeneralChat({ question: text, context: { mode: 'general_medical_assistant' }, history: previous, max_tokens: 800 }, { signal: controller.signal, onDelta: delta => { answer.content += delta; scrollBottom() } })
  } catch (error) {
    if (error?.name !== 'AbortError') { if (!answer.content) answer.content = `生成失败：${apiErrorMessage(error)}`; ElMessage.error(apiErrorMessage(error, '大模型回答失败')) }
  } finally {
    answer.streaming = false; sending.value = false; controller = null; conversationStore.persist(userId.value); scrollBottom()
  }
}
function stop() { controller?.abort() }
onMounted(async () => { scrollBottom(); try { const result = await getLlmHealth(); health.value = result.status === 'ready' ? 'ready' : 'unavailable' } catch { health.value = 'unavailable' } })
</script>

<style scoped>
.assistant-page { max-width: 1320px; margin: 0 auto; }
.chat-card { height: calc(100vh - 145px); }
:deep(.chat-body) { height: 100%; padding: 0; display: flex; flex-direction: column; }
.chat-list { flex: 1; overflow: auto; padding: 26px; }.welcome { margin: auto; text-align: center; color: var(--ppgl-muted); padding: 80px 20px; }.welcome strong { color: var(--ppgl-text); font-size: 18px; }
.chat-row { display: grid; grid-template-columns: 56px minmax(0, 1fr); gap: 10px; margin-bottom: 18px; }.chat-row.user { grid-template-columns: minmax(0, 1fr) 56px; }.chat-row.user .speaker { grid-column: 2; }.chat-row.user .chat-bubble { grid-column: 1; grid-row: 1; justify-self: end; background: var(--ppgl-primary-soft); }
.speaker { color: var(--ppgl-muted); font-weight: 700; padding-top: 10px; }.chat-bubble { max-width: 850px; white-space: pre-wrap; line-height: 1.75; padding: 12px 16px; border: 1px solid var(--ppgl-border); background: rgba(106,169,255,.1); border-radius: 10px; }
.cursor { display: inline-block; width: 2px; height: 1em; margin-left: 3px; background: var(--ppgl-primary); vertical-align: -2px; animation: blink .8s infinite; }.privacy-note { color: var(--ppgl-warning); background: rgba(255,209,102,.08); padding: 8px 20px; border-top: 1px solid rgba(255,209,102,.15); }
.composer { padding: 16px 20px 18px; border-top: 1px solid var(--ppgl-border); }.composer-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; margin-top: 10px; }.composer-actions span { color: var(--ppgl-muted); margin-right: auto; }
@keyframes blink { 50% { opacity: 0; } }
</style>
