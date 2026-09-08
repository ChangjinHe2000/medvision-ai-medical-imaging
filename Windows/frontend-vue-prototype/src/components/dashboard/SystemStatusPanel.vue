<template>
  <aside class="status-panel">
    <div class="section-kicker">System Status</div>
    <h3>计算节点状态</h3>

    <dl class="status-list">
      <div>
        <dt>Pipeline API</dt>
        <dd><span class="ok-dot"></span>Connected</dd>
      </div>
      <div>
        <dt>Segmentation Mode</dt>
        <dd>Abdomen / Fast</dd>
      </div>
      <div>
        <dt>LLM Service</dt>
        <dd>Qwen Local Chat</dd>
      </div>
    </dl>
  </aside>
</template>

<script setup>
</script>

<style scoped>
.status-panel {
  min-height: 100%;
  padding: 20px;
  border-left: 1px solid rgba(148, 163, 184, 0.14);
  background: rgba(9, 24, 38, 0.34);
}

.section-kicker {
  margin-bottom: 8px;
  color: #7fb4ad;
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

.status-panel h3 {
  margin: 0 0 18px;
  color: #f8fafc;
  font-size: 18px;
  font-weight: 850;
}

.status-list {
  margin: 0;
}

.status-list div {
  padding: 12px 0;
  border-top: 1px solid rgba(148, 163, 184, 0.12);
}

.status-list dt {
  color: #7f94a7;
  font-size: 12px;
  font-weight: 800;
}

.status-list dd {
  margin: 5px 0 0;
  color: #dbe7f0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.ok-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #4df1a1;
  box-shadow: 0 0 0 4px rgba(77, 241, 161, 0.1);
}

@media (max-width: 1100px) {
  .status-panel {
    border-left: 0;
    border-top: 1px solid rgba(148, 163, 184, 0.14);
  }
}
</style>
