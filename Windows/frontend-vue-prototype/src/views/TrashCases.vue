<template>
  <div class="page-shell">
    <header class="page-header">
      <div><h2>病例回收站</h2><p>软删除病例可恢复；彻底删除仅限管理员且不可撤销。</p></div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </header>
    <el-card class="section-card">
      <el-table :data="cases" v-loading="loading" empty-text="回收站为空">
        <el-table-column prop="display_name" label="病例名称" min-width="180" />
        <el-table-column prop="case_id" label="内部编号" min-width="240" />
        <el-table-column label="类型" width="130"><template #default="{ row }"><el-tag>{{ row.case_type === 'brain_mri' ? '脑肿瘤 MRI' : 'PPGL CT' }}</el-tag></template></el-table-column>
        <el-table-column prop="deleted_at" label="删除时间" min-width="190"><template #default="{ row }">{{ formatTime(row.deleted_at) }}</template></el-table-column>
        <el-table-column prop="deleted_reason_code" label="删除原因" min-width="140" />
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }"><el-button size="small" @click="restore(row)">恢复</el-button><el-button size="small" type="danger" plain @click="purge(row)">彻底删除</el-button></template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiErrorMessage } from '../api/errors.js'
import { listTrashCases, purgeTrashCase, restoreTrashCase } from '../api/trashApi.js'

const cases = ref([])
const loading = ref(false)
const formatTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-'
async function load() { loading.value = true; try { cases.value = (await listTrashCases()).cases || [] } catch (error) { ElMessage.error(apiErrorMessage(error, '回收站加载失败')) } finally { loading.value = false } }
async function restore(row) { try { await restoreTrashCase(row.case_id); ElMessage.success('病例已恢复'); await load() } catch (error) { ElMessage.error(apiErrorMessage(error, '恢复失败')) } }
async function purge(row) {
  try {
    const value = await ElMessageBox.prompt(`该操作不可恢复。请输入病例内部编号：${row.case_id}`, '彻底删除病例', { confirmButtonText: '确认彻底删除', cancelButtonText: '取消', inputValidator: text => text === row.case_id || '输入的病例编号不一致', type: 'warning' })
    if (value.value !== row.case_id) return
    await purgeTrashCase(row.case_id); ElMessage.success('病例已彻底删除'); await load()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(apiErrorMessage(error, '彻底删除失败')) }
}
onMounted(load)
</script>
