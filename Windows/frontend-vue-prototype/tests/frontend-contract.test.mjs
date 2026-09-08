import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('Vite 默认代理到 FastAPI 8000', () => {
  const config = read('vite.config.js')
  assert.match(config, /127\.0\.0\.1:8000/)
  assert.doesNotMatch(config, /localhost:8080|127\.0\.0\.1:8080/)
})

test('请求层启用 Cookie、CSRF 与统一 401 事件', () => {
  const request = read('src/api/request.js')
  assert.match(request, /withCredentials:\s*true/)
  assert.match(request, /X-CSRF-Token/)
  assert.match(request, /medvision:unauthorized/)
})

test('正式前端 API 不再调用旧 Spring Boot 业务前缀', () => {
  const files = ['src/api/authApi.js', 'src/api/caseApi.js', 'src/api/gliomaApi.js', 'src/api/reportApi.js', 'src/api/ragApi.js']
  for (const file of files) {
    const content = read(file)
    assert.doesNotMatch(content, /\/api\/ai|\/api\/brain/)
  }
})

test('路由包含服务端会话、管理员和只读角色保护', () => {
  const router = read('src/router/index.js')
  assert.match(router, /ensureSession/)
  assert.match(router, /requiresAdmin/)
  assert.match(router, /requiresEditor/)
})

test('对话仅由显式退出清除', () => {
  const auth = read('src/store/authStore.js')
  assert.match(auth, /logout[\s\S]*clearSession\(\{ clearConversations: true \}\)/)
  assert.match(auth, /medvision:unauthorized[\s\S]*clearSession\(\)/)
})

test('普通助手和病例助手直接消费后端流，不使用定时器伪造打字', () => {
  const assistant = read('src/views/AiAssistant.vue')
  const report = read('src/views/ReportView.vue')
  assert.match(assistant, /onDelta:\s*delta\s*=>\s*\{\s*answer\.content \+= delta/)
  assert.match(report, /onDelta:\s*delta\s*=>\s*\{\s*answer\.content \+= delta/)
  assert.doesNotMatch(assistant, /characterQueue|pumpCharacters|setTimeout\(tick/)
})

test('第七阶段包含统一报告与管理员知识文档入口', () => {
  const reportApi = read('src/api/reportApi.js')
  const router = read('src/router/index.js')
  assert.match(reportApi, /report\/generate/)
  assert.match(reportApi, /assistant\/stream/)
  assert.match(router, /admin\/knowledge/)
})
