<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { listRuns } from '@/api/register'
import { fmtTime } from '@/api/request'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'
import { categoryOptions, errorGuidance } from '@/utils/errorGuidance'

const props = defineProps({ active: { type: Boolean, default: false } })
const { dataVersion } = storeToRefs(useRuntimeStore())
const rows = ref([])
const loading = ref(false)
const category = ref('all')
let requestId = 0

const STATUS_TYPE = { done: 'primary', failed: 'danger', running: 'warning' }
const categories = categoryOptions()

function guidance(row) {
  return errorGuidance(row.error_category, row.error)
}

async function load() {
  if (!props.active) return
  const id = ++requestId
  loading.value = true
  try {
    const { items } = await listRuns(50, category.value)
    if (id === requestId) rows.value = items
  } catch (e) {
    if (id === requestId) ElMessage.error(e.message)
  } finally {
    if (id === requestId) loading.value = false
  }
}

// The parent combines the selected tab and current route into active. Lazy
// mounting and returning to the tab both load once; hidden tabs stay idle.
watch([() => props.active, dataVersion], ([active]) => {
  if (active) load()
  else {
    requestId += 1
    loading.value = false
  }
}, { immediate: true })
onBeforeUnmount(() => { requestId += 1 })
</script>

<template>
  <section class="account-run-history" aria-label="运行记录">
      <div class="history-toolbar">
          <p class="hint history-caption">最近 50 条已记录任务</p>
          <div class="history-actions">
            <el-select v-model="category" class="history-category" aria-label="按错误分类筛选" @change="load">
              <el-option label="全部分类" value="all" />
              <el-option v-for="item in categories" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
            <el-button :loading="loading" @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
          </div>
      </div>
      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table v-else v-loading="loading" :data="rows" size="small" stripe>
        <el-table-column prop="run_id" label="run_id" width="180">
          <template #default="{ row }"><span class="mono">{{ row.run_id }}</span></template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusDot :type="STATUS_TYPE[row.status] || 'info'" :text="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="错误分类" width="140">
          <template #default="{ row }">
            <div v-if="row.error || row.error_category" class="error-summary">
              <StatusDot :type="guidance(row).type" :text="guidance(row).label" />
              <span class="error-advice">{{ guidance(row).advice }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="失败原因" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.error" class="error-reason">{{ row.error }}</span>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无运行记录" :image-size="70" />
        </template>
      </el-table>
  </section>
</template>

<style scoped>
.account-run-history { min-width: 0; }
.history-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 14px;
}
.history-caption { margin: 0; }
.history-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.history-category { width: 132px; }
.error-summary { display: grid; gap: 4px; min-width: 0; }
.error-advice { color: var(--el-text-color-secondary); font-size: 11px; line-height: 1.45; white-space: normal; }
.error-reason { display: block; overflow-wrap: anywhere; white-space: normal; line-height: 1.5; }
</style>
