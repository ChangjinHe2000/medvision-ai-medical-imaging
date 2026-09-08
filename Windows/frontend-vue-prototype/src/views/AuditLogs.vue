<template>
  <div class="page-shell">
    <header class="page-header">
      <div><h2>操作审计</h2><p>查看用户、病例、推理和下载操作，并验证审计链完整性。</p></div>
      <div class="page-header-actions"><el-button @click="verify">验证完整性</el-button><el-button type="primary" @click="downloadCsv">导出记录</el-button></div>
    </header>
    <el-card class="section-card">
      <div class="filters">
        <el-input v-model.trim="filters.action" clearable placeholder="操作类型，例如 case.create" @keyup.enter="load" />
        <el-input v-model.trim="filters.subject_id" clearable placeholder="对象编号" @keyup.enter="load" />
        <el-select v-model="filters.limit"><el-option :value="50" label="最近 50 条" /><el-option :value="100" label="最近 100 条" /><el-option :value="500" label="最近 500 条" /></el-select>
        <el-button :loading="loading" @click="load">查询</el-button>
      </div>
      <el-alert v-if="integrity" :type="integrity.valid ? 'success' : 'error'" :closable="false" :title="integrity.valid ? `审计链完整，已校验 ${integrity.checked} 条记录` : '审计链校验未通过'" class="integrity" />
      <el-table :data="records" v-loading="loading">
        <el-table-column prop="sequence" label="序号" width="80" />
        <el-table-column prop="created_at" label="时间" min-width="180"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
        <el-table-column prop="user_id" label="操作者编号" min-width="190" />
        <el-table-column prop="action" label="操作" min-width="180" />
        <el-table-column label="结果" width="100"><template #default="{ row }"><el-tag :type="row.outcome === 'success' ? 'success' : 'danger'">{{ row.outcome === 'success' ? '成功' : '失败' }}</el-tag></template></el-table-column>
        <el-table-column prop="subject_type" label="对象类型" width="120" />
        <el-table-column prop="subject_id" label="对象编号" min-width="190" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { exportAuditLogs, listAuditLogs, verifyAuditIntegrity } from '../api/auditApi.js'
import { apiErrorMessage } from '../api/errors.js'

const filters = reactive({ action: '', subject_id: '', limit: 100 })
const records = ref([])
const integrity = ref(null)
const loading = ref(false)
const formatTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-'
async function load() { loading.value = true; try { records.value = (await listAuditLogs(filters)).records || [] } catch (error) { ElMessage.error(apiErrorMessage(error, '审计记录加载失败')) } finally { loading.value = false } }
async function verify() { try { integrity.value = await verifyAuditIntegrity() } catch (error) { ElMessage.error(apiErrorMessage(error, '审计链校验失败')) } }
async function downloadCsv() {
  try {
    const blob = await exportAuditLogs(filters)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a'); link.href = url; link.download = `操作审计_${new Date().toISOString().slice(0, 10)}.csv`; link.click(); URL.revokeObjectURL(url)
  } catch (error) { ElMessage.error(apiErrorMessage(error, '审计记录导出失败')) }
}
onMounted(load)
</script>

<style scoped>
.filters { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) 160px 90px; gap: 12px; margin-bottom: 16px; }
.integrity { margin-bottom: 16px; }
@media (max-width: 900px) { .filters { grid-template-columns: 1fr; } }
</style>
