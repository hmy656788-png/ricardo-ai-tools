<script setup>
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, isValidProxy, proxyScheme } from '@/stores/proxy'
import { useFormStore, preproxyText } from '@/stores/form'
import { testProxies } from '@/api/proxy'
import { copyText } from '@/api/request'

const proxyStore = useProxyStore()
const { form } = storeToRefs(useFormStore())
const { list, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({}) // proxy -> { status:'testing'|'ok'|'fail', latency_ms, ip, error }
const testingAll = ref(false)

const rows = computed(() =>
  list.value.map((p, i) => ({
    index: i + 1, proxy: p, valid: isValidProxy(p), result: testResults.value[p] || null,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)
// 测过且失败的才算。未测的不在里面，避免误删还没测的号。
const failCount = computed(() => rows.value.filter((r) => r.result && r.result.status === 'fail').length)

async function runTest(targets) {
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets, 8, preproxyText(form.value))
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('测试失败: ' + e.message)
  }
}
async function testOne(proxy) {
  await runTest([proxy])
}
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try { await runTest([...list.value]) }
  finally { testingAll.value = false }
}

function save() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.setFromText(draft.value)
  draft.value = ''
  ElMessage.success(`已保存 ${r.kept} 个代理${r.duplicated ? `（去重 ${r.duplicated} 个）` : ''}`)
}
function append() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.append(draft.value)
  draft.value = ''
  ElMessage.success(`已追加 ${r.added} 个新代理`)
}
async function clearFailed() {
  const failed = list.value.filter((p) => testResults.value[p]?.status === 'fail')
  if (!failed.length) {
    ElMessage.info('当前没有连通失败的代理')
    return
  }
  const drop = new Set(failed)
  proxyStore.setFromText(list.value.filter((p) => !drop.has(p)).join('\n'))
  for (const p of failed) delete testResults.value[p]
  ElMessage.success(`已清掉 ${failed.length} 个连通失败的代理`)
}
async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(`确定清空全部 ${count.value} 个代理？`, '确认', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
    proxyStore.clear()
    ElMessage.success('已清空')
  } catch (_) { /* cancel */ }
}
function editInDraft() {
  draft.value = proxyStore.text
  ElMessage.info('已把当前代理池载入编辑框，改完点「覆盖保存」')
}
</script>

<template>
  <section class="account-proxy-pool" aria-label="代理池">
    <el-row :gutter="16">
      <el-col :lg="10" class="proxy-column">
        <el-card shadow="never" class="proxy-panel">
          <template #header><span class="section-title" style="margin: 0">批量导入</span></template>
          <p class="hint">
            每行一个：<span class="mono">[协议://][user:pass@]host:port</span><br />
            不写协议默认按 <b>HTTP 代理</b>；SOCKS5 必须写 <span class="mono">socks5://</span>。<br />
            若某代理裸写能连、加了 <span class="mono">socks5://</span> 反而连不上，说明它其实是 HTTP 代理。
          </p>
          <el-input
            v-model="draft" type="textarea" :rows="12" class="mono"
            placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
          />
          <div class="proxy-actions draft-actions">
            <el-button type="primary" @click="save">覆盖保存</el-button>
            <el-button @click="append">追加合并</el-button>
            <el-button @click="editInDraft">载入当前池</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :lg="14" class="proxy-column">
        <el-card shadow="never" class="proxy-panel">
          <template #header>
            <div class="pool-header">
              <span class="section-title" style="margin: 0">
                当前代理池（{{ count }} 个<template v-if="invalidCount">，<span style="color: var(--el-color-danger)">{{ invalidCount }} 个格式异常</span></template>）
              </span>
              <div class="proxy-actions">
                <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">测试全部</el-button>
                <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">复制全部</el-button>
                <el-button size="small" type="danger" plain :disabled="!failCount || testingAll" @click="clearFailed">清空失败 ({{ failCount }})</el-button>
                <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">清空</el-button>
              </div>
            </div>
          </template>

          <el-table :data="rows" size="small" stripe max-height="440">
            <el-table-column prop="index" label="#" width="48" />
            <el-table-column prop="proxy" label="代理地址" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
            </el-table-column>
            <el-table-column label="格式" width="70">
              <template #default="{ row }">
                <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="light">
                  {{ row.valid ? '正常' : '异常' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="生效协议" width="110">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ proxyScheme(row.proxy) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="连通性" min-width="150">
              <template #default="{ row }">
                <template v-if="!row.result">
                  <span class="hint">未测</span>
                </template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">测试中…</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">正常 {{ row.result.latency_ms }}ms</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 6px">{{ row.result.ip }}</span>
                </template>
                <el-tooltip v-else :content="row.result.error || '连接失败'" placement="top">
                  <el-tag type="danger" size="small">失败</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" text type="primary"
                  :loading="row.result && row.result.status === 'testing'"
                  @click="testOne(row.proxy)"
                >测试</el-button>
                <el-button size="small" text type="danger" @click="proxyStore.remove(row.proxy)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>暂无代理，请通过「批量导入」添加</template>
          </el-table>

          <el-alert
            type="info" :closable="false" show-icon style="margin-top: 12px"
            title="账号列表中：代理下拉留空 = 本机 IP 直连。下拉选中一条、且这里 ≥2 个、勾选超过 2 个号时，按这里整池轮询并发；这里只有 1 个，或勾选不超过 2 个号时，串行。"
          />
        </el-card>
      </el-col>
    </el-row>
  </section>
</template>

<style scoped>
.account-proxy-pool { min-width: 0; }
.proxy-column { min-width: 0; margin-bottom: 16px; }
.account-proxy-pool .proxy-panel {
  margin-bottom: 0;
  border-color: var(--el-border-color-lighter);
  background: var(--el-bg-color);
  box-shadow: none;
}
.proxy-panel :deep(.el-card__header) {
  padding: 14px 16px;
  background: var(--el-fill-color-lighter);
  border-bottom-color: var(--el-border-color-lighter);
}
.proxy-panel :deep(.el-card__body) { padding: 16px; }
.proxy-panel .section-title { font-size: 15px; }
.proxy-panel .hint { overflow-wrap: anywhere; }
.pool-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
.proxy-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.proxy-actions :deep(.el-button + .el-button) { margin-left: 0; }
.draft-actions { margin-top: 12px; }
</style>
