<script setup lang="ts">
import { computed, onUnmounted, ref, shallowRef } from 'vue'
import { Download } from 'lucide-vue-next'
import { fetchTaskDelivery } from '../lib/api'
import type { DeliveryPacket } from '../lib/types'

const props = defineProps<{ taskId: string; runId: string }>()
const packet = shallowRef<DeliveryPacket | null>(null)
const selected = ref<string[]>([])
const rendered = ref<string[]>([])
const loading = ref(false)
const error = ref('')
const controller = new AbortController()
const previewMatches = computed(() => rendered.value.length === selected.value.length &&
  rendered.value.every((id) => selected.value.includes(id)))

onUnmounted(() => controller.abort())

async function preview() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  const selection = [...selected.value]
  try {
    packet.value = await fetchTaskDelivery(props.taskId, props.runId, selection, controller.signal)
    rendered.value = selection
  } catch (reason) {
    if (!controller.signal.aborted) error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    loading.value = false
  }
}

function download() {
  if (!packet.value || loading.value || !previewMatches.value || error.value) return
  const url = URL.createObjectURL(new Blob([packet.value.markdown], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = packet.value.filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <section class="delivery" aria-labelledby="delivery-heading">
    <h2 id="delivery-heading">Delivery packet</h2>
    <button v-if="!packet" type="button" :disabled="loading" @click="preview">Preview delivery packet</button>
    <p v-if="loading" role="status">Preparing delivery packet…</p>
    <p v-if="error" role="status">{{ error }}</p>
    <template v-if="packet">
      <details v-if="packet.artifacts.length">
        <summary>Include test artifacts</summary>
        <p>Select up to {{ packet.max_attachments }} items. Review raw output before sharing the packet.</p>
        <label v-for="option in packet.artifacts" :key="option.id" class="artifact">
          <input v-model="selected" type="checkbox" :value="option.id"
            :disabled="loading || (selected.length >= packet.max_attachments && !selected.includes(option.id))">
          <span>{{ option.name }}</span>
        </label>
      </details>
      <p v-if="!previewMatches" role="status">Update the preview to include the selected artifacts.</p>
      <div class="controls">
        <button type="button" :disabled="loading" @click="preview">Update preview</button>
        <button type="button" :disabled="loading || !previewMatches || !!error" @click="download">
          <Download :size="16" aria-hidden="true" /> Download Markdown
        </button>
      </div>
      <pre tabindex="0" aria-label="Delivery Markdown preview">{{ packet.markdown }}</pre>
    </template>
  </section>
</template>

<style scoped>
.delivery {
  display: grid;
  gap: 12px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  background: var(--panel);
  padding: 16px;
}
.delivery h2, .delivery p { margin: 0; }
.delivery h2 { font-size: 18px; }
.delivery button { width: fit-content; display: inline-flex; gap: 7px; align-items: center; }
.delivery details p { margin: 8px 0; color: var(--dim); }
.artifact { display: flex; align-items: start; gap: 8px; padding: 5px 0; overflow-wrap: anywhere; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; }
.delivery pre {
  max-height: 26rem;
  overflow: auto;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  font-family: var(--mono);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
