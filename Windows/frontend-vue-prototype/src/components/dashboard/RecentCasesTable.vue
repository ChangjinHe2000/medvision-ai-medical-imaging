<template>
  <section class="recent-cases">
    <div class="section-heading">
      <div>
        <div class="section-kicker">Active Queue</div>
        <h3>最近病例 / 当前任务</h3>
      </div>
      <el-button link type="primary" @click="$emit('open-case', { path: '/cases' })">
        查看全部
      </el-button>
    </div>

    <div class="cases-table">
      <div class="table-row table-head">
        <span>Case ID</span>
        <span>数据来源</span>
        <span>当前状态</span>
        <span>分割完成度</span>
        <span>报告状态</span>
        <span>更新时间</span>
      </div>

      <button
        v-for="item in cases"
        :key="item.caseId"
        type="button"
        class="table-row case-row"
        @click="$emit('open-case', item)"
      >
        <strong>{{ item.caseId }}</strong>
        <span>{{ item.source }}</span>
        <span>
          <em class="status-dot" :class="item.statusType"></em>
          {{ item.status }}
        </span>
        <span class="progress-cell">
          <i>
            <b :style="{ width: item.progress + '%' }"></b>
          </i>
          {{ item.progress }}%
        </span>
        <span>{{ item.reportStatus }}</span>
        <span>{{ item.updatedAt }}</span>
      </button>
    </div>
  </section>
</template>

<script setup>
defineProps({
  cases: {
    type: Array,
    default: () => []
  }
})

defineEmits(['open-case'])
</script>

<style scoped>
.recent-cases {
  padding: 24px 0 8px;
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 14px;
}

.section-kicker {
  margin-bottom: 6px;
  color: #7fb4ad;
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

.section-heading h3 {
  margin: 0;
  color: #f8fafc;
  font-size: 20px;
  font-weight: 850;
}

.cases-table {
  overflow-x: auto;
  border-top: 1px solid rgba(148, 163, 184, 0.14);
}

.table-row {
  min-width: 980px;
  width: 100%;
  display: grid;
  grid-template-columns: 1.05fr 0.8fr 0.9fr 1.1fr 0.9fr 0.9fr;
  gap: 18px;
  align-items: center;
  border: 0;
  border-bottom: 1px solid rgba(148, 163, 184, 0.11);
  background: transparent;
  color: #dbe7f0;
  text-align: left;
}

.table-head {
  padding: 12px 0;
  color: #7f94a7;
  font-size: 12px;
  font-weight: 800;
}

.case-row {
  padding: 14px 0;
  cursor: pointer;
  transition: background 0.18s ease;
}

.case-row:hover {
  background: rgba(32, 224, 196, 0.055);
}

.case-row strong {
  color: #f8fafc;
}

.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 7px;
  border-radius: 999px;
  background: #8ea3b4;
}

.status-dot.success {
  background: #4df1a1;
}

.status-dot.warning {
  background: #ffd166;
}

.status-dot.info {
  background: #6aa9ff;
}

.progress-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}

.progress-cell i {
  width: 96px;
  height: 5px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.16);
  overflow: hidden;
}

.progress-cell b {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: #20e0c4;
}
</style>
