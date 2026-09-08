<template>
  <section class="workflow-stepper" aria-label="系统工作流">
    <div class="section-kicker">Primary Workflow</div>
    <div class="stepper-line">
      <div
        v-for="(step, index) in steps"
        :key="step.title"
        class="step-item"
        :class="{ active: step.active, current: step.current }"
      >
        <div class="step-node">
          {{ String(index + 1).padStart(2, '0') }}
        </div>
        <div class="step-body">
          <div class="step-title">{{ step.title }}</div>
          <div class="step-desc">{{ step.desc }}</div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
defineProps({
  steps: {
    type: Array,
    default: () => []
  }
})
</script>

<style scoped>
.workflow-stepper {
  padding: 22px 0 8px;
}

.section-kicker {
  margin-bottom: 14px;
  color: #7fb4ad;
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

.stepper-line {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  column-gap: 18px;
  position: relative;
}

.stepper-line::before {
  content: "";
  position: absolute;
  left: 20px;
  right: 20px;
  top: 20px;
  height: 1px;
  background: rgba(148, 163, 184, 0.22);
}

.step-item {
  position: relative;
  min-width: 0;
  padding-top: 54px;
}

.step-node {
  position: absolute;
  left: 0;
  top: 0;
  width: 40px;
  height: 40px;
  border: 1px solid rgba(148, 163, 184, 0.34);
  border-radius: 999px;
  background: #081827;
  color: #8ea3b4;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 800;
  z-index: 1;
}

.step-item.active .step-node {
  border-color: rgba(32, 224, 196, 0.72);
  color: #b9f8f1;
  background: rgba(32, 224, 196, 0.12);
}

.step-item.current .step-node {
  box-shadow: 0 0 0 5px rgba(32, 224, 196, 0.08);
}

.step-title {
  color: #e7eef5;
  font-size: 14px;
  font-weight: 800;
}

.step-desc {
  margin-top: 4px;
  color: #8ea3b4;
  font-size: 12px;
  line-height: 1.5;
}

@media (max-width: 1100px) {
  .stepper-line {
    grid-template-columns: 1fr;
    row-gap: 16px;
  }

  .stepper-line::before {
    left: 20px;
    right: auto;
    top: 0;
    bottom: 0;
    width: 1px;
    height: auto;
  }

  .step-item {
    padding: 0 0 0 56px;
  }
}
</style>
