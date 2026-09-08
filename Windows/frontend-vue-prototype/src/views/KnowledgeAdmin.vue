<template>
  <div class="page-shell">
    <header class="page-header"><div><h2>知识文档管理</h2><p>公开医学文档上传、解析、向量化与审核启用</p></div><el-button :loading="loading" @click="load">刷新</el-button></header>
    <el-alert title="公共知识库只能接收授权公开资料，禁止上传病例、影像、患者身份信息或包含本机路径的文档。" type="warning" show-icon :closable="false" class="notice" />
    <div class="layout">
      <el-card class="section-card" shadow="never"><template #header><strong>上传公开医学文档</strong></template>
        <el-form label-position="top" :model="form">
          <el-form-item label="文件"><input type="file" accept=".pdf,.docx,.txt,.md,.html,.htm" @change="selectFile" /></el-form-item>
          <el-form-item label="标题"><el-input v-model.trim="form.title" /></el-form-item>
          <el-form-item label="发布机构"><el-input v-model.trim="form.organization" /></el-form-item>
          <div class="form-grid"><el-form-item label="文档类型"><el-select v-model="form.document_type"><el-option label="指南" value="GUIDELINE"/><el-option label="共识" value="CONSENSUS"/><el-option label="论文" value="ARTICLE"/><el-option label="规范" value="SPECIFICATION"/></el-select></el-form-item><el-form-item label="专业"><el-select v-model="form.specialty"><el-option label="PPGL" value="PPGL"/><el-option label="脑肿瘤" value="BRAIN_TUMOUR"/><el-option label="通用影像" value="IMAGING"/></el-select></el-form-item></div>
          <div class="form-grid"><el-form-item label="版本"><el-input v-model.trim="form.version" /></el-form-item><el-form-item label="授权状态"><el-select v-model="form.license_status"><el-option label="开放获取" value="OPEN_ACCESS"/><el-option label="公共领域" value="PUBLIC_DOMAIN"/><el-option label="已获授权" value="PERMISSION_GRANTED"/><el-option label="机构授权" value="INSTITUTION_AUTHORIZED"/></el-select></el-form-item></div>
          <el-form-item label="来源地址"><el-input v-model.trim="form.source_url" /></el-form-item>
          <el-button type="primary" :loading="uploading" @click="upload">上传文档</el-button>
        </el-form>
      </el-card>
      <el-card class="section-card status-card" shadow="never"><template #header><strong>知识库状态</strong></template>
        <el-descriptions :column="1" border><el-descriptions-item label="数据库">{{ statusData.ready ? '可用' : '未就绪' }}</el-descriptions-item><el-descriptions-item label="pgvector">{{ statusData.pgvector || '-' }}</el-descriptions-item><el-descriptions-item label="已启用">{{ statusData.documents?.ACTIVE || 0 }}</el-descriptions-item><el-descriptions-item label="待审核">{{ statusData.documents?.PENDING_REVIEW || 0 }}</el-descriptions-item></el-descriptions>
      </el-card>
    </div>
    <el-card class="section-card table-card" shadow="never"><template #header><strong>文档与审核状态</strong></template>
      <el-table :data="documents" v-loading="loading"><el-table-column prop="title" label="标题" min-width="210"/><el-table-column prop="organization" label="机构" min-width="140"/><el-table-column label="提交人" min-width="130"><template #default="scope">{{ scope.row.submitter?.display_name || scope.row.submitter?.username || '未知账号' }}</template></el-table-column><el-table-column prop="version" label="版本" width="100"/><el-table-column prop="specialty" label="专业" width="120"/><el-table-column prop="status" label="状态" width="140"/><el-table-column label="操作" min-width="320"><template #default="scope"><el-button size="small" @click="operate(scope.row,'parse')">解析切片</el-button><el-button size="small" @click="operate(scope.row,'index')">生成向量</el-button><el-button size="small" type="success" @click="operate(scope.row,'enable')">审核启用</el-button><el-button size="small" @click="operate(scope.row,'disable')">停用</el-button><el-button size="small" type="danger" @click="operate(scope.row,'delete')">删除</el-button></template></el-table-column></el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiErrorMessage } from '../api/errors.js'
import { deleteKnowledgeDocument, disableKnowledgeDocument, enableKnowledgeDocument, getKnowledgeStatus, indexKnowledgeDocument, listKnowledgeDocuments, parseKnowledgeDocument, uploadKnowledgeDocument } from '../api/knowledgeAdminApi.js'
const documents=ref([]),statusData=ref({}),loading=ref(false),uploading=ref(false),file=ref(null)
const form=reactive({title:'',organization:'',document_type:'GUIDELINE',specialty:'IMAGING',version:'1.0',source_url:'',license_status:'OPEN_ACCESS'})
function selectFile(event){file.value=event.target.files?.[0]||null;if(file.value&&!form.title)form.title=file.value.name.replace(/\.[^.]+$/,'')}
async function load(){loading.value=true;try{const [status,items]=await Promise.all([getKnowledgeStatus(),listKnowledgeDocuments()]);statusData.value=status;documents.value=items.documents||[]}catch(error){ElMessage.error(apiErrorMessage(error,'知识库状态加载失败'))}finally{loading.value=false}}
async function upload(){if(!file.value||!form.title||!form.organization||!form.source_url)return ElMessage.warning('请完整填写文件、标题、机构和来源地址');uploading.value=true;try{await uploadKnowledgeDocument(file.value,form);ElMessage.success('文档已上传，下一步请解析切片');await load()}catch(error){ElMessage.error(apiErrorMessage(error,'文档上传失败'))}finally{uploading.value=false}}
const actions={parse:parseKnowledgeDocument,index:indexKnowledgeDocument,enable:enableKnowledgeDocument,disable:disableKnowledgeDocument,delete:deleteKnowledgeDocument}
async function operate(row,action){try{if(action==='delete')await ElMessageBox.confirm('删除后文档立即退出检索，是否继续？','删除知识文档',{type:'warning'});await actions[action](row.id);ElMessage.success('操作完成');await load()}catch(error){if(error!=='cancel')ElMessage.error(apiErrorMessage(error,'操作失败'))}}
onMounted(load)
</script>
<style scoped>.notice,.layout{margin-bottom:16px}.layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(280px,.5fr);gap:16px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.status-card{align-self:start}.table-card{overflow:hidden}@media(max-width:900px){.layout,.form-grid{grid-template-columns:1fr}}</style>
