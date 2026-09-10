<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  candidateInputDisabled,
  candidateOperationEnabled,
  type CandidateExpectedStatus,
  type CandidateReviewDraft,
  type CandidateReviewDecision,
  type FeatureAvailability,
} from '../../core/workbench/FeatureRuntimeGateway.ts'
const props = defineProps<{ availability: FeatureAvailability; result: string; busy: boolean }>()
const emit = defineEmits<{
  preview: [candidateId: string]
  review: [draft: CandidateReviewDraft]
  accept: [candidateId: string]
}>()
const candidateId = ref('')
const decision = ref<CandidateReviewDecision>('approve')
const expectedStatus = ref<CandidateExpectedStatus>('complete')
const inputDisabled = computed(() => candidateInputDisabled(props.availability, props.busy))
const canRun = computed(() => candidateOperationEnabled(props.availability, props.busy, candidateId.value))
function preview(): void { if (canRun.value) emit('preview', candidateId.value.trim()) }
function review(): void {
  if (canRun.value) emit('review', {
    candidateId: candidateId.value.trim(),
    decision: decision.value,
    expectedStatus: expectedStatus.value,
  })
}
function accept(): void { if (canRun.value) emit('accept', candidateId.value.trim()) }
</script>
<template>
  <section class="feature-panel" data-feature-surface="candidate-review" :aria-busy="busy">
    <h2>候选审阅</h2>
    <label>Candidate ID <input v-model="candidateId" :disabled="inputDisabled" type="text" autocomplete="off" placeholder="candidate_id" /></label>
    <label>Decision
      <select v-model="decision" :disabled="inputDisabled">
        <option value="approve">approve</option>
        <option value="reject">reject</option>
      </select>
    </label>
    <label>Expected status
      <select v-model="expectedStatus" :disabled="inputDisabled">
        <option value="complete">complete</option>
        <option value="partial">partial</option>
      </select>
    </label>
    <div class="feature-actions">
      <button type="button" :disabled="!canRun" @click="preview">预览</button>
      <button type="button" :disabled="!canRun" @click="review">审阅</button>
      <button type="button" :disabled="!canRun" @click="accept">接受并发布</button>
    </div>
    <p v-if="!availability.available" class="feature-reason" role="status">{{ availability.reason }}</p>
    <pre v-if="result" class="candidate-result">{{ result }}</pre>
  </section>
</template>
