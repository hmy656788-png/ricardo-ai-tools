<script setup>
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { checkPlus } from '@/api/register'
import { fmtTime } from '@/api/request'

// 独立检测弹窗：自包含全部状态与错误处理，主页面只管传入邮箱列表。
// 逐号检测（每号一次请求），进度实时可见；任何异常都停在弹窗内。
const visible = ref(false)
const running = ref(false)
const items = ref([])      // [{ email, state, status, label, error }]
const proxyText = ref('')
const source = ref('imported')

const doneCount = computed(() => items.value.filter((x) => x.state !== 'pending').length)
const progress = computed(() => items.value.length ? Math.round((doneCount.value / items.value.length) * 100) : 0)

const STATE_TYPE = { ok: 'primary', danger: 'danger', warning: 'warning', info: 'info' }
const emit = defineEmits(['done'])

async function open(emails, opts = {}) {
  if (!emails || !emails.length) { ElMessage.info('当前没有可检测的号'); return }
  
proxyText.value = opts.proxy || ''
  source.value = opts.source || 'imported'
  items.value = emails.map((e) => ({ email: e, state: 'pending', label: '', error: '' }))
  visible.value = true
  start()
}

async function start() {
  running.value = true
  try {
    for (const item of items.value) {
      if (item.state !== 'pending') continue
      item.state = 'running'
      try {
        const r = await checkPlus([item.email], proxyText.value, source.value)
        const info = (r.results || {})[item.email]
        if (info && info.status && info.status !== 'error') {
          item.state = 'ok'; item.label = info.label || info.status
        } else {
          item.state = 'warning'; item.label = (info && info.label) || '未检测成'
        }
      } catch (e) {
        item.state = 'danger'; item.label = '检测失败'; item.error = (e.message || '').slice(0, 120)
      }
    }
  } finally {
    running.value = false
    emit('done')
  }
}

defineExpose({ open })
</script>

<template>
  <el-dialog v-model="visible" title="账号检测" width="640px" :close-on-click-modal="!running">
    <div v-if="running" class="ck-progress">
      <el-progress :percentage="progress" :stroke-width="14" striped striped-flow />
      <div class="ck-progress-text">正在检测 {{ doneCount + 1 > items.length ? items.length : doneCount + 1 }} / {{ items.length }} 个…</div>
    </div>
    <div v-else class="ck-progress">
      <el-tag type="success">检测完成</el-tag>
      <span class="ck-progress-text">共 {{ items.length }} 个，正常 {{ items.filter(x=>x.state==='ok').length }} 个，失败 {{ items.filter(x=>x.state==='danger').length }} 个</span>
    </div>
    <el-table :data="items" size="small" stripe max-height="380">
      <el-table-column prop="email" label="邮箱" min-width="220" show-overflow-tooltip />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.state === 'running'" type="warning" size="small">检测中…</el-tag>
          <el-tag v-else-if="row.state === 'pending'" type="info" size="small">等待</el-tag>
          <el-tag v-else size="small" :type="STATE_TYPE[row.state] || 'info'">{{ row.label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="说明" min-width="180">
        <template #default="{ row }"><span class="ck-err">{{ row.error }}</span></template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="visible = false" :disabled="running">关闭</el-button>
      <el-button type="primary" @click="items.forEach(x => x.state = 'pending'); start()" :loading="running">重新检测</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.ck-progress { margin-bottom: 12px; display: flex; align-items: center; gap: 10px; }
.ck-progress-text { font-size: 13px; color: #909399; }
.ck-err { color: #f56c6c; font-size: 12px; }
</style>