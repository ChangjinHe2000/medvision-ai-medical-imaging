<template>
  <section class="stats-bar" aria-label="系统统计">
    <div
      v-for="item in stats"
      :key="item.title"
      class="stat-item"
    >
      <div class="stat-value">{{ item.value }}</div>
      <div class="stat-copy">
        <div class="stat-title">{{ item.title }}</div>
        <div class="stat-desc">{{ item.desc }}</div>
      </div>
    </div>
  </section>
</template>

<script setup>
defineProps({
  stats: {
    type: Array,
    default: () => []
  }
})
</script>

<style scoped>
.stats-bar {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-top: 1px solid rgba(148, 163, 184, 0.14);
  border-bottom: 1px solid rgba(148, 163, 184, 0.14);
  background: rgba(8, 18, 31, 0.52);
}

.stat-item {
  min-height: 74px;
  padding: 16px 20px;
  display: flex;
  align-items: center;
  gap: 14px;
  border-right: 1px solid rgba(148, 163, 184, 0.12);
}

.stat-item:last-child {
  border-right: none;
}

.stat-value {
  min-width: 42px;
  color: #f8fafc;
  font-size: 30px;
  font-weight: 800;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}

.stat-title {
  color: #dbe7f0;
  font-size: 14px;
  font-weight: 700;
}

.stat-desc {
  margin-top: 3px;
  color: #8ea3b4;
  font-size: 12px;
}

@media (max-width: 1100px) {
  .stats-bar {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .stat-item:nth-child(2n) {
    border-right: none;
  }
}
</style>
