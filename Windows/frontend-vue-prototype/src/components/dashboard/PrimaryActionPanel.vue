<template>
  <section class="primary-panel">
    <div class="panel-copy">
      <div class="section-kicker">Core Operation</div>
      <h3>从 CT 上传开始一次完整分析</h3>
      <p>上传影像后进入病例队列，系统将完成分割、量化指标、三维重建和辅助报告链路。</p>
      <el-button type="primary" size="large" :icon="UploadFilled" @click="$emit('primary-action')">
        上传 CT
      </el-button>
    </div>

    <div class="command-list">
      <button
        v-for="action in actions"
        :key="action.title"
        type="button"
        class="command-item"
        @click="$emit('action', action.path)"
      >
        <el-icon>
          <component :is="action.icon" />
        </el-icon>
        <span>{{ action.title }}</span>
        <small>{{ action.desc }}</small>
      </button>
    </div>
  </section>
</template>

<script setup>
import { UploadFilled } from '@element-plus/icons-vue'

defineProps({
  actions: {
    type: Array,
    default: () => []
  }
})

defineEmits(['primary-action', 'action'])
</script>

<style scoped>
.primary-panel {
  display: grid;
  grid-template-columns: minmax(280px, 0.9fr) minmax(0, 1.4fr);
  gap: 24px;
  padding: 24px 0;
  border-bottom: 1px solid rgba(148, 163, 184, 0.14);
}

.section-kicker {
  margin-bottom: 10px;
  color: #7fb4ad;
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

.panel-copy h3 {
  margin: 0;
  color: #f8fafc;
  font-size: 22px;
  font-weight: 850;
}

.panel-copy p {
  max-width: 480px;
  margin: 10px 0 18px;
  color: #8ea3b4;
  line-height: 1.7;
}

.command-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  align-content: start;
}

.command-item {
  min-height: 62px;
  padding: 12px 14px;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-radius: 8px;
  background: rgba(9, 24, 38, 0.62);
  color: #dbe7f0;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  grid-template-rows: auto auto;
  column-gap: 10px;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.18s ease, background 0.18s ease, transform 0.18s ease;
}

.command-item:hover {
  transform: translateY(-1px);
  border-color: rgba(32, 224, 196, 0.38);
  background: rgba(32, 224, 196, 0.08);
}

.command-item .el-icon {
  grid-row: 1 / span 2;
  align-self: center;
  color: #20e0c4;
  font-size: 20px;
}

.command-item span {
  font-weight: 800;
}

.command-item small {
  margin-top: 2px;
  color: #8ea3b4;
}

@media (max-width: 1100px) {
  .primary-panel {
    grid-template-columns: 1fr;
  }
}
</style>
