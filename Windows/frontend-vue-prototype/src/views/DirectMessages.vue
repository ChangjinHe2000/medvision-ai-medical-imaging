<template>
  <div class="page-shell messages-page">
    <header class="page-header"><div><h2>站内私聊</h2><p>账号之间发送内部工作消息，不得发送患者身份信息。</p></div></header>
    <el-card class="section-card messages-card" body-class="messages-body">
      <aside class="contacts">
        <div class="contacts-title">联系人</div>
        <button v-for="contact in contacts" :key="contact.id" type="button" class="contact" :class="{ active: selected?.id === contact.id }" @click="selectContact(contact)">
          <span class="avatar">{{ (contact.display_name || contact.username).slice(0, 1) }}</span>
          <span class="contact-text"><strong>{{ contact.display_name || contact.username }}</strong><small>{{ roleText(contact.role) }}</small></span>
          <el-badge :value="contact.unread_count" :hidden="!contact.unread_count" :max="99" />
        </button>
        <el-empty v-if="!contacts.length" description="暂无可联系账号" :image-size="60" />
      </aside>
      <section class="conversation">
        <template v-if="selected">
          <div class="conversation-header"><strong>{{ selected.display_name || selected.username }}</strong><span>{{ roleText(selected.role) }}</span></div>
          <div ref="messageBox" class="message-list">
            <div v-for="item in messages" :key="item.id" class="message-row" :class="{ mine: item.sender_id === authStore.state.user?.id }">
              <div class="bubble"><div>{{ item.content }}</div><time>{{ formatTime(item.created_at) }}</time></div>
            </div>
            <el-empty v-if="!messages.length" description="暂无消息，开始一次工作沟通" :image-size="70" />
          </div>
          <div class="composer"><el-input v-model="content" type="textarea" :rows="3" maxlength="2000" show-word-limit placeholder="输入工作消息，请勿发送患者姓名、手机号、证件号等信息" @keydown.ctrl.enter.prevent="send" /><el-button type="primary" :loading="sending" @click="send">发送</el-button></div>
        </template>
        <el-empty v-else description="请选择联系人" />
      </section>
    </el-card>
  </div>
</template>

<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getDirectMessages, getMessageContacts, markDirectMessagesRead, sendDirectMessage } from '../api/messagingApi.js'
import { apiErrorMessage } from '../api/errors.js'
import { authStore } from '../store/authStore.js'

const contacts = ref([]), selected = ref(null), messages = ref([]), content = ref(''), sending = ref(false), messageBox = ref(null)
let timer = null
const roleText = role => ({ admin: '管理员', doctor: '医生', viewer: '只读用户' }[role] || role)
const formatTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : ''
async function loadContacts() { const data = await getMessageContacts(); contacts.value = data.contacts || []; notifyUnread() }
function notifyUnread() { globalThis.dispatchEvent?.(new CustomEvent('medvision:messages-updated', { detail: { unreadTotal: contacts.value.reduce((sum, item) => sum + Number(item.unread_count || 0), 0) } })) }
async function loadConversation() { if (!selected.value) return; const data = await getDirectMessages(selected.value.id); messages.value = data.messages || []; await markDirectMessagesRead(selected.value.id); selected.value.unread_count = 0; notifyUnread(); await nextTick(); if (messageBox.value) messageBox.value.scrollTop = messageBox.value.scrollHeight }
async function selectContact(contact) { selected.value = contact; try { await loadConversation() } catch (error) { ElMessage.error(apiErrorMessage(error, '消息加载失败')) } }
async function send() { if (!selected.value || !content.value.trim()) return; sending.value = true; try { await sendDirectMessage(selected.value.id, content.value.trim()); content.value = ''; await loadConversation() } catch (error) { ElMessage.error(apiErrorMessage(error, '消息发送失败')) } finally { sending.value = false } }
async function refresh() { try { await loadContacts(); if (selected.value) { selected.value = contacts.value.find(item => item.id === selected.value.id) || null; await loadConversation() } } catch { /* 登录失效由统一拦截器处理 */ } }
onMounted(async () => { try { await loadContacts() } catch (error) { ElMessage.error(apiErrorMessage(error, '联系人加载失败')) }; timer = setInterval(refresh, 5000) })
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.messages-card { height: calc(100vh - 145px); }
:deep(.messages-body) { height: 100%; padding: 0; display: grid; grid-template-columns: 280px 1fr; }
.contacts { border-right: 1px solid var(--ppgl-border); padding: 16px 10px; overflow: auto; }
.contacts-title { padding: 0 10px 12px; font-weight: 800; }
.contact { width: 100%; border: 0; background: transparent; color: var(--ppgl-text); padding: 10px; border-radius: 8px; display: flex; align-items: center; gap: 10px; cursor: pointer; text-align: left; }
.contact:hover,.contact.active { background: var(--ppgl-primary-soft); }
.avatar { width: 36px; height: 36px; border-radius: 50%; background: #0f766e; display: grid; place-items: center; font-weight: 800; }
.contact-text { min-width: 0; flex: 1; display: flex; flex-direction: column; }.contact-text small,.conversation-header span { color: var(--ppgl-muted); }
.conversation { min-width: 0; display: flex; flex-direction: column; }.conversation-header { height: 58px; padding: 12px 18px; border-bottom: 1px solid var(--ppgl-border); display: flex; flex-direction: column; }
.message-list { flex: 1; padding: 20px; overflow: auto; }.message-row { display: flex; margin-bottom: 12px; }.message-row.mine { justify-content: flex-end; }
.bubble { max-width: 70%; padding: 10px 13px; border-radius: 10px; background: rgba(106,169,255,.13); }.mine .bubble { background: var(--ppgl-primary-soft); }.bubble time { display: block; color: var(--ppgl-muted); font-size: 11px; margin-top: 5px; }
.composer { border-top: 1px solid var(--ppgl-border); padding: 14px; display: grid; grid-template-columns: 1fr 88px; gap: 12px; align-items: end; }
@media (max-width: 800px) { :deep(.messages-body) { grid-template-columns: 96px 1fr; }.contact-text { display:none; }.contacts { padding: 10px 5px; }.contact { justify-content:center; } }
</style>
