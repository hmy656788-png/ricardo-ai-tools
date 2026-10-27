<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Reading } from '@element-plus/icons-vue'
import {
  listRegistered, getRegistered, deleteRegistered,
  bulkDeleteRegistered, listExportFormats, exportRegistered,
  updateCredentials, refreshAt, importAccounts, importedBackfillRt, checkPlus,
} from '@/api/register'
import { copyText, fmtTime } from '@/api/request'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import CheckDialog from '@/components/CheckDialog.vue'
import AccountRunHistory from '@/components/AccountRunHistory.vue'
import AccountProxyPool from '@/components/AccountProxyPool.vue'
import AccountUsageGuide from '@/components/AccountUsageGuide.vue'
import { parseAccountImport } from '@/utils/accountImport'

const route = useRoute()
const router = useRouter()
const guideVisible = ref(false)
const activeTab = computed({
  get: () => ['runs', 'proxy'].includes(route.query.tab) ? route.query.tab : 'accounts',
  set: (value) => {
    const query = { ...route.query }
    if (value === 'runs' || value === 'proxy') query.tab = value
    else delete query.tab
    router.replace({ name: 'registered', query })
  },
})

function openGuideTab(tab) {
  guideVisible.value = false
  activeTab.value = tab
}

const { form } = storeToRefs(useFormStore())
// 刷新AT 的单代理可以直接从代理池里挑：和代理池页共用同一份 localStorage 池。
const proxyStore = useProxyStore()
const { list: proxyList } = storeToRefs(proxyStore)
const runtime = useRuntimeStore()
// dataVersion 要走 storeToRefs 才保持响应（watch 用）；bumpData 是 action，直接从
// store 实例上取 —— storeToRefs 只转 state/getter，把 action 解构出来会丢 this。
const { dataVersion } = storeToRefs(runtime)

// 轻量版固定使用 imported（导入账号）数据源：
// 邮箱----密码----2FA 导入 → 勾选 → 刷新AT，全流程只需要这一张表。
const SOURCE = 'imported'

// 默认维持 20 条，日常浏览不拥挤；需要批量勾选导出时再切到更大页数。
const PAGE_SIZES = [20, 50, 100, 200, 500]
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(PAGE_SIZES[0])
const filter = ref('all')
const search = ref('')
const selected = ref([])
const loading = ref(false)
// 操作结果横幅（导入 / 刷新AT 共用）：
// 结构化 { kind, title, details } —— kind 决定横幅配色（绿=全成功/橙黄=有失败/红=失败），
// details 是逐条明细，一行一条。
const checkResult = ref(null)
const checkDlg = ref(null)
// 检测逻辑已迁移到独立弹窗组件 CheckDialog.vue：自包含进度/结果/错误处理。

function plusOf(row) { return row.plus_check || null }
// 打开检测弹窗：收集邮箱并传入当前选定的代理（脚本里取 form.value 才正确；模板中 ref 已自动解包，不能写 form.value）
function openCheck(mode) {
  const emails = mode === 'selected'
    ? selected.value.map((r) => r.email)
    : rows.value.filter((r) => !plusOf(r)).map((r) => r.email)
  checkDlg.value.open(emails, { proxy: proxyText(form.value), source: SOURCE })
}

// 批量并发数：常显，最高 20。勾选 >2 个号才把这个值送出；
// 单代理时后端会自动降为串行（防同 IP 风控关联）。
const batchConcurrency = ref(2)

// ──────────── 导入账号弹窗 ────────────
// 别处注册的号（邮箱----密码----2FA）导进来，存 imported 独立表。
const importDlg = ref(false)
const importText = ref('')
const importing = ref(false)
const importPreview = computed(() => parseAccountImport(importText.value))
const importStat = computed(() => {
  const preview = importPreview.value
  if (!preview.total) return '尚未粘贴任何内容'
  let message = `共 ${preview.total} 行，${preview.recognized} 行格式可识别`
  if (preview.normalized) message += `，${preview.normalized} 行分隔符将在导入时自动统一为 ----`
  if (preview.errors.length) {
    message += `；第 ${preview.errors.map((error) => error.line).join('、')} 行需要检查。${preview.errors[0].message}`
  }
  return message
})

function openImport() {
  importDlg.value = true
}

async function doImport() {
  const preview = importPreview.value
  if (!preview.total) { ElMessage.info('请先粘贴账号文本'); return }
  if (preview.errors.length) {
    ElMessage.warning(`第 ${preview.errors[0].line} 行：${preview.errors[0].message}`)
    return
  }
  importing.value = true
  try {
    const r = await importAccounts(preview.text)
    const errs = r.errors || []
    importText.value = ''
    importDlg.value = false
    await load(true)
    checkResult.value = {
      kind: errs.length ? 'warn' : 'success',
      title: `导入完成：新增 ${r.inserted} 个，更新 ${r.updated} 个`,
      details: errs,
    }
    ElMessage.success(`导入完成：新增 ${r.inserted}，更新 ${r.updated}${errs.length ? `（${errs.length} 行有问题）` : ''}`)
  } catch (e) { ElMessage.error('导入失败: ' + e.message) }
  finally { importing.value = false }
}

async function batchPayload(emails) {
  // 下拉留空 = 直连，不带池。
  // 下拉选中一条，且勾选 >2、池里 ≥2 条：才把整池发出去轮询。
  // 勾选 ≤2，或池里只有 1 条：只走选中的那一条，后端会串行。
  const selectedProxy = proxyText(form.value)
  const usePool = emails.length > 2 && !!selectedProxy && proxyList.value.length >= 2
  return {
    emails,
    proxy: selectedProxy || '',
    concurrency: emails.length > 2 ? batchConcurrency.value : 1,
    proxy_pool: usePool ? (proxyStore.text || '') : '',
  }
}

// 刷新AT：三路径级联直换新 access_token —— 有 RT 走 OAuth grant；无 RT/RT 失效
// 用 session_token 走会话直提；连 RT/ST 都没有的号（如新导入）用 密码+2FA 直接
// 登录拿会话（不开 Codex RT 交换、不绑手机号、不接码）。轮换出的新 RT/ST 自动写回。
const refreshing = ref(false)

async function refreshAT() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) { ElMessage.info('请先勾选要刷新 AT 的号'); return }
  if (!(await confirm(
    `刷新这 ${emails.length} 个号的 access_token（不碰邮箱验证码、不接码）。\n\n` +
    `· 有 refresh_token 的号：走 OAuth 直换（RT 轮换自动写回，秒级）\n` +
    `· 无 RT / RT 失效：用 session_token 走登录会话直提（秒级）\n` +
    `· 无 RT/ST 的号（如新导入）：用 密码+2FA 直接登录拿会话（较慢，单个约几十秒）\n` +
    `· 会话滚动下发的新 session_token 会自动写回\n` +
    `· 勾选 ≤2 个号一律串行；>2 个号才按所选并发一起跑\n` +
    `· 代理留空 = 本机 IP 直连并发，不轮询代理池\n` +
    `· 下拉选中 1 个代理，且代理池 ≥2 个：按整池轮询并发；池里只有 1 个则串行\n\n确定开始？`,
  ))) return
  refreshing.value = true
  try {
    const r = await refreshAt(await batchPayload(emails), SOURCE)
    const results = r.results || []
    const ok = results.filter((x) => x.ok)
    const bad = results.filter((x) => !x.ok)
    const rotated = ok.filter((x) => x.st_rotated || x.rt_rotated).length
    const viaSt = ok.filter((x) => x.via === 'session_token').length
    const viaLogin = ok.filter((x) => x.via === 'login').length
    const details = []
    if (viaLogin) details.push(`${viaLogin} 个走 密码+2FA 登录直提（免 RT/ST）`)
    if (viaSt) details.push(`${viaSt} 个走 session_token 直提（免 RT）`)
    if (rotated) details.push(`${rotated} 个会话/RT 已轮换并写回`)
    const banned = bad.filter((x) => x.banned)
    checkResult.value = {
      kind: bad.length ? 'warn' : 'success',
      title: `刷新AT 完成 ${ok.length}/${results.length} 个`,
      details: [
        ...details,
        ...bad.map((x) => x.banned
          ? `🚫 ${x.email}：已封号（OpenAI 返回账号已删除或停用）`
          : `❌ ${x.email}：${(x.error || '').slice(0, 80)}`),
      ],
    }
    if (banned.length) ElMessage.warning(`${banned.length} 个已封号，表格已标红`)
    else if (bad.length) ElMessage.warning(`刷新完成，${bad.length} 个失败`)
    else ElMessage.success(`刷新完成 ${ok.length}/${results.length} 个`)
    await load()
  } catch (e) {
    checkResult.value = { kind: 'error', title: '刷新AT 失败', details: [e.message] }
  }
  finally { refreshing.value = false }
}

// 刷新refresh（补 RT）：对缺 refresh_token 的号重新登录授权拿新 RT 写回。
// 使用导入凭证重新登录；不依赖邮箱接码，但可能要求额外的手机号验证。
const backfilling = ref(false)

async function backfillRT() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) { ElMessage.info('请先勾选要刷新 refresh 的号'); return }
  if (!(await confirm(
    `重新登录授权这 ${emails.length} 个号，补 refresh_token 并刷新凭证。\n\n` +
    `· 使用导入的密码与2FA重新登录，此路径不依赖邮箱接码\n` +
    `· 可能遇到额外的手机号验证，请以任务实际结果为准\n` +
    `· 单个账号处理较慢；拿到的新 RT 自动写回\n` +
    `· 可在本页「运行记录」标签查看任务状态\n\n确定开始？`,
  ))) return
  backfilling.value = true
  try {
    const r = await importedBackfillRt(await batchPayload(emails))
    const runs = r.runs || []
    const ok = runs.filter((x) => x.run_id)
    const bad = runs.filter((x) => !x.run_id)
    checkResult.value = {
      kind: bad.length ? 'warn' : 'success',
      title: `刷新refresh 已启动 ${ok.length}/${runs.length} 个`,
      details: [
        '任务在后台执行，状态可在本页「运行记录」标签查看',
        ...bad.map((x) => `❌ ${x.email}：${(x.error || '').slice(0, 80)}`),
      ],
    }
    if (bad.length) ElMessage.warning(`启动完成，${bad.length} 个失败`)
    else ElMessage.success(`已启动 ${ok.length}/${runs.length} 个补 refresh 任务`)
    trackBackfill(runs)
  } catch (e) {
    checkResult.value = { kind: 'error', title: '刷新refresh 启动失败', details: [e.message] }
  }
  finally { backfilling.value = false }
}

let backfillTimer = null

async function trackBackfill(runs) {
  const ids = runs.map((x) => x.run_id).filter(Boolean)
  if (!ids.length) return
  const emailOf = Object.fromEntries(runs.map((x) => [x.run_id, x.email]))
  if (backfillTimer) {
    clearInterval(backfillTimer)
    backfillTimer = null
  }
  const tick = async () => {
    try {
      const r = await fetch(`/api/runs/status?ids=${ids.join(',')}`).then((x) => x.json())
      const byId = {}
      for (const i of r.items || []) byId[i.run_id] = i
      const done = ids.filter((id) => !byId[id] || byId[id].status !== 'running')
      const failed = ids.filter((id) => byId[id] && byId[id].status === 'failed')
      if (done.length < ids.length) {
        checkResult.value = { kind: 'info', title: `刷新refresh 进行中 ${done.length}/${ids.length} 个完成`, details: [] }
        return false
      }
      if (backfillTimer) {
        clearInterval(backfillTimer)
        backfillTimer = null
      }
      checkResult.value = {
        kind: failed.length ? 'warn' : 'success',
        title: `刷新refresh 完成 成功 ${ids.length - failed.length}/${ids.length} 个`,
        details: failed.map((id) =>
          `❌ ${emailOf[id] || byId[id].email || id}：${((byId[id].error || byId[id].error_category || '失败') + '').slice(0, 80)}`),
      }
      await load()
      return true
    } catch { return false }
  }
  const finished = await tick()
  if (!finished) backfillTimer = setInterval(() => { tick() }, 3000)
}

async function load(resetPage) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const { items, total: t } = await listRegistered({
      limit: pageSize.value, offset: (page.value - 1) * pageSize.value,
      filter: filter.value, search: search.value,
    }, SOURCE)
    rows.value = items
    total.value = t
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

// customClass 里的 pre-line 让消息里的 \n 真的换行。
// 不用 dangerouslyUseHTMLString：消息里会拼邮箱、文件名这些数据，走 HTML 等于开 XSS 口子。
async function confirm(msg) {
  try {
    await ElMessageBox.confirm(msg, '确认', {
      type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消',
      customClass: 'confirm-multiline',
    })
    return true
  }
  catch (_) { return false }
}
async function deleteOne(email) {
  if (!(await confirm(`删除 ${email} 的凭证？`))) return
  try { await deleteRegistered(email, SOURCE); ElMessage.success('已删除'); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`确定删除选中的 ${emails.length} 条凭证？(不可恢复)`))) return
  try { const r = await bulkDeleteRegistered({ emails }, SOURCE); ElMessage.success(`已删除 ${r.deleted} 条`); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteAll() {
  if (!(await confirm('这会清空导入账号表里的所有凭证，确定？'))) return
  if (!(await confirm('再次确认：真的要删除全部凭证吗？此操作不可恢复！'))) return
  try { const r = await bulkDeleteRegistered({ all: true }, SOURCE); ElMessage.success(`已清空 ${r.deleted} 条`); load() }
  catch (e) { ElMessage.error(e.message) }
}

// ──────────── 批量导出 ────────────
// 格式清单来自后端 export_formats.py，下拉菜单是 v-for 出来的：
// 以后加格式只改后端那一个文件，这里一行都不用动。
const exportFormats = ref([])
const exporting = ref(false)
const exportVisible = ref(false)
const exportText = ref('')
const exportCount = ref(0)
const exportFilename = ref('')
const exportLabel = ref('')
// CPA JSON 是每号一个文件的 ZIP，内容较多时不在页面解包/预览，
// 但仍要先弹出同一套「下载保留 / 下载删除」选择。
const exportBytes = ref(null)
const exportMime = ref('text/plain;charset=utf-8')
const isBinaryExport = computed(() => exportBytes.value instanceof Uint8Array)
// 这一批导出的到底是哪些号 —— 「下载并删除」照着它删，来自后端 r.emails。
const exportedEmails = ref([])
const deletingExported = ref(false)

const exportBtnText = computed(() =>
  selected.value.length ? `导出选中 (${selected.value.length})` : '导出全部',
)

async function loadExportFormats() {
  if (exportFormats.value.length) return
  try {
    const { formats } = await listExportFormats()
    exportFormats.value = formats || []
  } catch (e) { ElMessage.error('加载导出格式失败: ' + e.message) }
}

async function doExport(fmt) {
  const emails = selected.value.map((r) => r.email)
  // 没勾选 = 导出全部（跨页，不只当前页）
  const payload = emails.length ? { format: fmt.id, emails } : { format: fmt.id, all: true }
  exporting.value = true
  try {
    const r = await exportRegistered(payload, SOURCE)
    exportedEmails.value = (r.emails || []).filter(Boolean)
    exportCount.value = r.count || 0
    exportFilename.value = r.filename || 'export.txt'
    exportLabel.value = r.label || fmt.label
    // CPA ZIP 等二进制文件不解包预览，避免大量账号时卡住页面；
    // 改为打开导出弹窗，让用户明确选择保留或下载后删除。
    if (r.mode === 'download') {
      exportText.value = ''
      exportBytes.value = b64ToBytes(r.b64)
      exportMime.value = r.mime || 'application/octet-stream'
      exportVisible.value = true
      return
    }
    exportText.value = r.text || ''
    exportBytes.value = null
    exportMime.value = 'text/plain;charset=utf-8'
    exportVisible.value = true
  } catch (e) { ElMessage.error('导出失败: ' + e.message) }
  finally { exporting.value = false }
}

function b64ToBytes(b64) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

function saveBlob(data, filename, mime) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function downloadExport(notifyPreserved = true) {
  saveBlob(
    isBinaryExport.value ? exportBytes.value : exportText.value,
    exportFilename.value,
    isBinaryExport.value ? exportMime.value : 'text/plain;charset=utf-8',
  )
  if (isBinaryExport.value && notifyPreserved) {
    ElMessage.success(`已下载 ${exportFilename.value}，原数据已保留`)
  }
}

// ──────────── 下载并删除 ────────────
// 导出的 txt / CPA ZIP 里账号数据都齐了，表里就没有留存价值了，一起清掉。
//
// ⚠️ 顺序**必须**是「先下载、再确认、最后删」：
//    删库是不可恢复的，而浏览器下载可能被拦（弹窗拦截 / 用户点了取消 / 磁盘满）。
//    先把文件落盘再问，用户是在**手里已经有导出文件**的前提下点的确认。
async function downloadAndDelete() {
  downloadExport(false)

  const emails = exportedEmails.value
  if (!emails.length) {
    ElMessage.warning('这批导出没有拿到 email 列表，只下载不删除')
    return
  }

  const ok = await confirm(
    `已下载 ${exportFilename.value}。\n\n` +
    `现在删除这 ${emails.length} 个号：\n` +
    `  · 导入账号表（凭证、2FA secret）\n\n` +
    `删掉后只剩刚下载的导出文件，不可恢复。确定？`,
  )
  if (!ok) return

  deletingExported.value = true
  try {
    const r1 = await bulkDeleteRegistered({ emails }, SOURCE)
    ElMessage.success(`已删除：导入账号 ${r1.deleted} 条`)
    exportVisible.value = false
    exportedEmails.value = []
    selected.value = []
    load(true)          // 回第一页：这一批没了，停在旧页码多半是空页
    runtime.bumpData()
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  } finally {
    deletingExported.value = false
  }
}

// 凭证弹窗
const credVisible = ref(false)
const credEmail = ref('')
const credData = ref(null)
// totp_secret 放最前：它是唯一「服务端取不回」的字段，弹窗一打开就要能看到
const CRED_KEYS = ['totp_secret', 'totp_factor_id', 'access_token', 'session_token', 'refresh_token', 'id_token', 'device_id', 'csrf_token', 'cookie_header', 'password']
const credRows = computed(() => {
  if (!credData.value) return []
  return CRED_KEYS.filter((k) => credData.value[k]).map((k) => ({ key: k, val: credData.value[k] }))
})
async function viewCred(email) {
  try {
    const { data } = await getRegistered(email, SOURCE)
    credData.value = data
    credEmail.value = email
    credVisible.value = true
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
async function copyCell(email, field) {
  try {
    const { data } = await getRegistered(email, SOURCE)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} 为空`); return }
    await copyText(val)
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
function copyAllJson() {
  if (credData.value) copyText(JSON.stringify(credData.value, null, 2))
}

// ── 手动编辑凭证 ──
// 只改本地库，不同步 OpenAI。改完的值会被登录流程直接用上
// （registrar 的 account_callback 走 db.get_registered，不区分数据来源）。
const editVisible = ref(false)
const editSaving = ref(false)
const editEmail = ref('')
const editPassword = ref('')
const editSecret = ref('')
// 打开弹窗时的原值，用来判断哪些字段真被改过（没改的不传，后端就不碰）
const editOrigPassword = ref('')
const editOrigSecret = ref('')

function openEdit(row) {
  editEmail.value = row.email
  editPassword.value = row.password || ''
  editSecret.value = row.totp_secret || ''
  editOrigPassword.value = row.password || ''
  editOrigSecret.value = row.totp_secret || ''
  editVisible.value = true
}

async function saveEdit() {
  const pw = editPassword.value
  const sec = editSecret.value.trim()
  const payload = { email: editEmail.value }
  // 只把真正改动过的字段传给后端 —— 没动的字段不传，后端就不会碰它
  if (pw !== editOrigPassword.value) payload.password = pw
  if (sec !== editOrigSecret.value) payload.totp_secret = sec
  if (payload.password === undefined && payload.totp_secret === undefined) {
    ElMessage.info('没有改动')
    editVisible.value = false
    return
  }
  // secret 是唯一「服务端取不回」的凭证：覆盖掉原值 = 该号 2FA 永久锁死。
  // 只在「原本就有 secret」且「确实要改」时拦一道，新填不打扰。
  if (payload.totp_secret !== undefined && editOrigSecret.value) {
    try {
      await ElMessageBox.confirm(
        `该账号已有 2FA secret：\n${editOrigSecret.value}\n\n` +
        '覆盖后原 secret 将永久丢失，服务端取不回。\n' +
        '若原 secret 仍是账号上生效的那个，覆盖会导致该号 2FA 永远登不上。',
        '确认覆盖 2FA secret？',
        { type: 'warning', confirmButtonText: '确认覆盖', cancelButtonText: '取消' },
      )
    } catch { return }
  }
  editSaving.value = true
  try {
    const r = await updateCredentials(payload, SOURCE)
    ElMessage.success(`已保存：${(r.changed || []).join(' + ') || '无改动'}`)
    editVisible.value = false
    await load()
  } catch (e) {
    // 后端 400 会带具体原因（如「TOTP secret 含非法字符」），原样透出
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally { editSaving.value = false }
}

let searchTimer
watch(page, () => load())
watch(search, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => load(true), 250)
})
watch(pageSize, () => {
  // 改页容量后从第一页开始；若当前已在第一页，page 不会触发 watch，需手动加载。
  if (page.value === 1) load()
  else page.value = 1
})
watch(dataVersion, () => load())
onActivated(() => load())
</script>
<template>
  <div class="page is-imported">
    <el-card shadow="never" class="main-card">
      <template #header>
        <div class="head-row">
          <span class="page-title">帐号资格验证</span>
          <div class="head-actions">
            <el-button class="guide-button" @click="guideVisible = true">
              <el-icon><Reading /></el-icon><span>使用教程</span>
            </el-button>
            <button v-if="activeTab === 'accounts'" class="add-btn" type="button" @click="openImport">
              ＋ 添加账号
            </button>
          </div>
        </div>
      </template>

      <el-tabs v-model="activeTab" class="account-tabs">
        <el-tab-pane label="账号列表" name="accounts">
      <el-space class="account-toolbar" wrap style="margin-bottom: 12px">
        <el-button @click="load(false)"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-select v-model="filter" style="width: 130px" @change="load(true)">
          <el-option label="全部" value="all" />
          <el-option label="有 RT" value="has_rt" />
          <el-option label="无 RT" value="no_rt" />
          <el-option label="缺AT" value="no_at" />
          <el-option label="未检测" value="unchecked" />
          <el-option label="Free" value="free" />
          <el-option label="可领Plus" value="plus" />
          <el-option label="Plus半价折扣" value="plus_half" />
          <el-option label="已封号" value="banned" />
          <el-option label="凭证失效" value="token_invalid" />
        </el-select>
        <el-input
          v-model="search" clearable :prefix-icon="Search"
          placeholder="搜索邮箱账号，支持部分匹配" style="width: 240px"
        />
        <el-select
          v-model="form.proxy" filterable clearable allow-create default-first-option
          :reserve-keyword="false" placeholder="代理（留空直连，但ip批量登录易风控）" title="留空直连，但ip批量登录易风控"
          style="width: 260px"
        >
          <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
        </el-select>
        <el-button @click="openCheck('unchecked')">检查未检测</el-button>
        <el-button :disabled="!selected.length" @click="openCheck('selected')">
          检测选中 ({{ selected.length }})
        </el-button>
        <el-button type="primary" plain :loading="refreshing" :disabled="!selected.length" @click="refreshAT">
          <el-icon><Refresh /></el-icon>刷新AT ({{ selected.length }})
        </el-button>
        <el-button type="warning" plain :loading="backfilling" :disabled="!selected.length" @click="backfillRT" title="重新登录授权以补充 refresh_token，可能需要额外的手机号验证">
          <el-icon><RefreshRight /></el-icon>刷新refresh ({{ selected.length }})
        </el-button>
        <span class="conc-label">并发</span>
        <el-input-number
          v-model="batchConcurrency"
          class="conc-input"
          :min="1"
          :max="20"
          :step="1"
          title="并发数：最多同时跑几个号（1-20）。勾选超过 2 个号才启用；留空直连走本机 IP；下拉选中代理且池里 ≥2 个时按整池轮询；池里只有 1 个则串行"
        />
        <el-divider direction="vertical" />
        <el-dropdown trigger="click" @command="doExport" @visible-change="(v) => v && loadExportFormats()">
          <el-button :loading="exporting">
            <el-icon><Download /></el-icon>{{ exportBtnText }}
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="f in exportFormats" :key="f.id" :command="f" :divided="f.mode === 'download' && f.id === 'cpa'">
                {{ f.label }}
                <span v-if="f.note" class="hint" style="margin-left: 6px">{{ f.note }}</span>
              </el-dropdown-item>
              <el-dropdown-item v-if="!exportFormats.length" disabled>加载中...</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-divider direction="vertical" />
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          删除选中 ({{ selected.length }})
        </el-button>
        <el-button type="danger" plain @click="deleteAll">清空全部</el-button>
        </el-space>

      <!-- 独立检测弹窗：自包含进度与结果，主页面零侵入 -->
      <CheckDialog ref="checkDlg" @done="load(false)" />
      <!-- 操作结果横幅：导入 / 刷新AT 的结果摘要（三色醒目） -->
      <div v-if="checkResult" class="op-banner" :class="`is-${checkResult.kind}`">
        <div class="op-banner-title">{{ checkResult.title }}</div>
        <div v-if="checkResult.details && checkResult.details.length" class="op-banner-line">{{ checkResult.details.join('\n') }}</div>
        <i class="op-banner-x" title="关闭" @click="checkResult = null">✕</i>
      </div>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column label="邮箱" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button size="small" text type="primary" class="cell-copy" @click="copyText(row.email)">
              {{ row.email }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
          </template>
        </el-table-column>
        <!-- 密码直接明文列出：登录账号的必需品，一点就能复制。 -->
        <el-table-column label="密码" min-width="150">
          <template #default="{ row }">
            <el-button
              v-if="row.password" size="small" text type="primary"
              class="cell-copy mono" @click="copyText(row.password)"
            >
              {{ row.password }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <!-- 2FA secret 明文列出：唯一「服务端取不回」的凭证，
             丢了这个号就永久锁死，必须一眼看见、一点就能复制。 -->
        <el-table-column label="2FA" min-width="260">
          <template #default="{ row }">
            <el-button
              v-if="row.totp_secret" size="small" text type="warning"
              class="cell-copy mono" @click="copyText(row.totp_secret)"
            >
              {{ row.totp_secret }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="Plus / 时间" width="160">
          <template #default="{ row }">
            <div v-if="plusOf(row)" class="plus-status">
              <el-tag
                size="small"
                :class="{
                  'plus-half': plusOf(row).status === 'plus_half',
                  'plus-banned': plusOf(row).status === 'banned'
                }"
                :type="{ plus_eligible: 'success', plus_active: 'success', plus_half: 'warning', banned: 'danger', free: 'info', token_invalid: 'warning', error: 'warning' }[plusOf(row).status] || 'info'"
              >{{ plusOf(row).label }}</el-tag>
              <span v-if="plusOf(row).checked_at" class="hint">{{ fmtTime(plusOf(row).checked_at) }}</span>
            </div>
            <span v-else class="hint">未检测</span>
          </template>
        </el-table-column>
   
     <el-table-column label="access" width="70" align="center">
          <template #default="{ row }">
            <el-button v-if="row.at_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'access_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.at_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="session" width="70" align="center">
          <template #default="{ row }">
            <el-button v-if="row.st_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'session_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.st_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="refresh" width="70" align="center">
          <template #default="{ row }">
            <el-button v-if="row.rt_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'refresh_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.rt_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="140">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text @click="viewCred(row.email)">查看凭证</el-button>
            <el-button size="small" text type="warning" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row.email)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            description="暂无导入账号，点右上角「＋ 添加账号」粘贴导入"
            :image-size="70"
          />
        </template>
      </el-table>
      <div style="display: flex; justify-content: center; margin-top: 14px">
        <el-pagination
          v-model:current-page="page" v-model:page-size="pageSize"
          :page-sizes="PAGE_SIZES" :total="total"
          layout="prev, pager, next, total, sizes" background
        />
      </div>

      <el-dialog v-model="exportVisible" width="720px" top="8vh">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <span style="font-weight: 600">导出 · {{ exportLabel }}</span>
            <el-tag size="small" type="info">共 {{ exportCount }} {{ isBinaryExport ? '个号' : '行' }}</el-tag>
          </div>
        </template>
        <el-alert
          v-if="isBinaryExport"
          type="info" :closable="false" show-icon
          title="CPA JSON 将按每个账号一个 .cpa.json 文件，统一打包为 ZIP。"
          description="为避免大量账号时解包、渲染 JSON 导致页面卡顿，此处不显示文件内容。请选择下载后保留原数据，或确认下载后删除本次导出的数据。"
        />
        <el-input
          v-else
          :model-value="exportText" type="textarea" :rows="14" readonly
          class="mono export-area"
        />
        <template #footer>
          <template v-if="!isBinaryExport">
            <el-button @click="copyText(exportText)">
              <el-icon><CopyDocument /></el-icon>复制全部
            </el-button>
            <el-button type="primary" @click="downloadExport">
              <el-icon><Download /></el-icon>下载 {{ exportFilename }}
            </el-button>
          </template>
          <el-button v-else type="primary" @click="downloadExport">
            <el-icon><Download /></el-icon>下载并保留原数据
          </el-button>
          <!-- 危险动作放最右、danger 色，和左边的纯下载拉开距离，避免手滑。
                先下载文件、再弹二次确认，确认框里会报清楚要删多少条。 -->
          <el-button
            type="danger" plain
            :loading="deletingExported"
            :disabled="!exportedEmails.length"
            @click="downloadAndDelete"
          >
            <el-icon><Delete /></el-icon>下载并删除这 {{ exportedEmails.length }} 个号
          </el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="credVisible" :title="credEmail" width="760px" top="6vh">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <span class="mono" style="font-weight: 600">{{ credEmail }}</span>
            <el-button size="small" @click="copyAllJson">复制全部 JSON</el-button>
          </div>
        </template>
        <div v-for="r in credRows" :key="r.key" style="margin-bottom: 12px">
          <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 4px">
            <span class="mono" style="font-weight: 600; color: var(--dango-pink-dark)">{{ r.key }}</span>
            <el-tag size="small" type="info">len={{ r.val.length }}</el-tag>
            <el-button size="small" @click="copyText(r.val)">复制</el-button>
          </div>
          <el-input :model-value="r.val" type="textarea" :rows="2" readonly class="mono" />
        </div>
        <el-empty v-if="!credRows.length" description="无凭证字段" />
      </el-dialog>

      <!-- 手动编辑凭证：把外部已知的密码/2FA 补进来，或修正记录错误 -->
      <el-dialog v-model="editVisible" title="编辑凭证" width="560px" top="10vh">
        <el-alert
          type="warning" :closable="false" show-icon style="margin-bottom: 16px"
          title="仅修改本地记录，不会同步到 OpenAI"
          description="这里改密码不等于改了账号密码。填入的值会被登录流程直接使用。"
        />
        <el-form label-position="top">
          <el-form-item label="邮箱">
            <el-input :model-value="editEmail" class="mono" disabled />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="editPassword" class="mono" placeholder="留空表示该号无密码" />
          </el-form-item>
          <el-form-item label="2FA Secret">
            <el-input
              v-model="editSecret" class="mono"
              placeholder="base32，支持带空格/小写/otpauth:// 链接，会自动规范化"
            />
            <div class="hint" style="margin-top: 6px; line-height: 1.6">
              服务端取不回此值，覆盖后原 secret 永久丢失。清空则该号按无 2FA 处理。
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="editVisible = false">取消</el-button>
          <el-button type="primary" :loading="editSaving" @click="saveEdit">保存</el-button>
        </template>
      </el-dialog>

      <!-- 导入账号：粘贴 邮箱----密码----2FA，逐行解析 upsert 到独立 imported 表 -->
      <el-dialog v-model="importDlg" title="导入账号" width="680px" top="8vh">
        <el-alert
          type="info" :closable="false" show-icon style="margin-bottom: 14px"
          title="每行一条：邮箱 / 密码 / 2FA密钥，支持 |、｜ 或 --、---、---- 分隔"
          description="如 邮箱|密码|2FA、邮箱｜密码｜2FA，或 邮箱--密码---2FA，导入时自动统一为四个减号。密码含竖线时请用 ---- 分隔；竖线格式最多三段，四减号格式多余字段仍忽略。重复邮箱仅更新密码/2FA，原凭证与导入时间保留。"
        />
        <el-input
          v-model="importText" type="textarea" :rows="10" class="mono"
          placeholder="user1@example.com----example-password----EXAMPLE2FASECRET&#10;user2@example.com----example-password----EXAMPLE2FASECRET"
        />
        <div class="hint" style="margin-top: 8px">{{ importStat }}</div>
        <template #footer>
          <el-button @click="importDlg = false">取消</el-button>
          <el-button type="warning" :loading="importing" :disabled="!importPreview.total || importPreview.errors.length > 0" @click="doImport">导入</el-button>
        </template>
      </el-dialog>

        </el-tab-pane>
        <el-tab-pane label="运行记录" name="runs" lazy>
          <AccountRunHistory :active="route.name === 'registered' && activeTab === 'runs'" />
        </el-tab-pane>
        <el-tab-pane label="代理池" name="proxy" lazy>
          <AccountProxyPool />
        </el-tab-pane>
      </el-tabs>
      <AccountUsageGuide v-model="guideVisible" @navigate="openGuideTab" />
    </el-card>
  </div>
</template>

<style scoped>
/* 表格里「点一下就复制」的明文单元格（密码 / 2FA secret）。
   :deep 是必需的：.el-button 由 Element Plus 渲染，scoped 的属性选择器打不到它。

   为什么要重置 padding —— Element Plus 有两个长得很像的类：
     .el-button--text  （旧版 type="text"）  padding 左右为 0
     .el-button.is-text（新版 text 属性）    继承 --small 的 5px 11px
   我们用的是后者，于是 11px padding + 12px 图标 + 4px 间隙 = 值被整体右推 27px，
   同列的表头和空值「—」都贴着 cell 左沿，一眼就看出错位。 */
:deep(.el-button.cell-copy.el-button--small) {
  padding: 0 6px 0 0;
  height: 22px;
  font-size: 13px;
  font-weight: 400;
}
/* 图标默认透明但**保留占位**：用 opacity 而不是 display:none，
   否则 hover 时图标撑开宽度会把文字挤得左右抖。 */
:deep(.cell-copy .ico) {
  margin-left: 5px;
  opacity: 0;
  transition: opacity 0.12s;
}
:deep(.cell-copy:hover .ico) { opacity: 0.65; }

.plus-status { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
:deep(.plus-status .el-tag.plus-half) {
  color: #ad4e00;
  font-weight: 600;
  background: #fff7e6;
  border-color: #ffc069;
}
:deep(.plus-status .el-tag.plus-banned) {
  color: #cf1322 !important;
  font-weight: 600 !important;
  background: #fff1f0 !important;
  border-color: #ffa39e !important;
  border-width: 1px !important;
}

/* 操作结果横幅（导入 / 刷新AT）：醒目三色 */
.op-banner {
  width: fit-content;
  max-width: 100%;
  margin: 0 0 14px;
  padding: 10px 14px;
  border-radius: 8px;
  border: 1.5px solid transparent;
  font-size: 14px;
  line-height: 1.6;
}
.op-banner-title { font-weight: 600; font-size: 15px; }
.op-banner-line { font-size: 13px; opacity: 0.92; white-space: pre-line; }
.op-banner.is-info { color: #1e5fa8; border-color: #79bbff; background: #ecf5ff; }
html.dark .op-banner.is-info { color: #79bbff; background: rgba(64, 158, 255, 0.12); border-color: rgba(64, 158, 255, 0.45); }
.op-banner-x { position: absolute; top: 8px; right: 10px; font-style: normal; font-size: 15px; line-height: 1; opacity: 0.55; cursor: pointer; }
.op-banner-x:hover { opacity: 1; }
.op-banner { position: relative; }
.op-banner.is-success {
  color: #1f7a1f;
  border-color: #95d475;
  background: #f0f9eb;
}
.op-banner.is-warn {
  color: #b8860b;
  border-color: #f3d19e;
  background: #fdf6ec;
}
.op-banner.is-error {
  color: #c45656;
  border-color: #fab6b6;
  background: #fef0f0;
}
html.dark .op-banner.is-success { color: #95d475; background: rgba(103, 194, 58, 0.12); border-color: rgba(103, 194, 58, 0.45); }
html.dark .op-banner.is-warn { color: #eebe77; background: rgba(230, 162, 60, 0.14); border-color: rgba(230, 162, 60, 0.5); }
html.dark .op-banner.is-error { color: #f89898; background: rgba(245, 108, 108, 0.12); border-color: rgba(245, 108, 108, 0.45); }

/* 只调整账号页的配色，不覆盖其它模块或状态标签的语义颜色。 */
.page.is-imported {
  --account-accent: #a84f35;
  --account-accent-hover: #8f432f;
  --account-accent-text: #fffdfa;
  --account-danger: #a6423a;
  --el-fill-color-blank: var(--el-bg-color);
}
html.dark .page.is-imported {
  --account-accent: #e08a6a;
  --account-accent-hover: #f0b19a;
  --account-accent-text: #211f1d;
  --account-danger: #e6a49b;
}

/* ── 页头：炭灰标题 ＋ 陶土色主操作 ── */
.head-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-height: 34px; }
.head-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-left: auto; min-width: 0; }
.head-actions .add-btn { margin-left: 0; }
.guide-button { padding-right: 12px; padding-left: 12px; }
.guide-button span { margin-left: 5px; }
/* 工具条里的并发框：字样 + 默认尺寸数字框，宽度收紧 */
.conc-label { font-size: 14px; color: var(--el-text-color-regular); }
.conc-input { width: 110px; }
.conc-input :deep(.el-input__inner) { font-size: 14px; font-weight: 500; font-variant-numeric: tabular-nums; }
.page-title {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 18px; font-weight: 600; line-height: 1.4;
  color: var(--el-text-color-primary);
}
.add-btn {
  position: relative;
  display: inline-flex; align-items: center; gap: 4px;
  padding: 7px 16px; margin-left: 6px;
  border: none; border-radius: 18px;
  background: var(--account-accent);
  color: var(--account-accent-text); font-size: 14px; font-weight: 500; line-height: 20px; cursor: pointer;
  box-shadow: 0 1px 2px rgb(0 0 0 / 6%);
  transition: background-color 0.15s, box-shadow 0.15s;
}
.add-btn:hover { background: var(--account-accent-hover); }
.add-btn:focus-visible {
  outline: 2px solid var(--account-accent);
  outline-offset: 3px;
}

/* 奶油白内容面、暖灰分隔线：移除金边和大面积渐变。 */
.main-card { border-color: var(--app-border); background: var(--el-bg-color); }
.main-card > :deep(.el-card__header) {
  background: var(--el-bg-color);
  border-bottom-color: var(--app-border);
}
.main-card > :deep(.el-card__body) { background: var(--el-bg-color); }
.account-tabs > :deep(.el-tabs__header) { margin-bottom: 16px; }
.account-tabs :deep(.el-tabs__nav-wrap::after) { height: 1px; background: var(--app-border); }
.account-tabs :deep(.el-tabs__item) { font-weight: 500; }
.account-tabs :deep(.el-tabs__item.is-active),
.account-tabs :deep(.el-tabs__item:hover) { color: var(--account-accent); }
.account-tabs :deep(.el-tabs__active-bar) { background: var(--account-accent); }
.main-card :deep(.el-table) {
  --el-table-bg-color: var(--el-bg-color);
  --el-table-tr-bg-color: var(--el-bg-color);
  --el-table-header-bg-color: var(--el-fill-color-lighter);
  --el-table-row-hover-bg-color: var(--el-fill-color);
  --el-table-border-color: var(--el-border-color-lighter);
}

/* 刷新类操作采用统一的次级色，只有主操作和危险操作带强调色。 */
.account-toolbar :deep(.el-button:not(.el-button--danger)) {
  --el-button-text-color: var(--el-text-color-regular);
  --el-button-bg-color: var(--el-bg-color);
  --el-button-border-color: var(--el-border-color);
  --el-button-hover-text-color: var(--account-accent);
  --el-button-hover-bg-color: var(--el-fill-color-lighter);
  --el-button-hover-border-color: var(--account-accent);
  --el-button-active-text-color: var(--account-accent-hover);
  --el-button-active-bg-color: var(--el-fill-color);
  --el-button-active-border-color: var(--account-accent-hover);
}
.account-toolbar :deep(.el-button--danger) {
  --el-button-text-color: var(--account-danger);
  --el-button-bg-color: color-mix(in srgb, var(--account-danger) 5%, var(--el-bg-color));
  --el-button-border-color: color-mix(in srgb, var(--account-danger) 24%, var(--el-bg-color));
  --el-button-hover-text-color: var(--account-danger);
  --el-button-hover-bg-color: color-mix(in srgb, var(--account-danger) 10%, var(--el-bg-color));
  --el-button-hover-border-color: var(--account-danger);
}
.account-toolbar :deep(.el-button.is-disabled) {
  color: var(--el-text-color-placeholder);
  background: var(--el-fill-color-lighter);
  border-color: var(--el-border-color-light);
}
@media (prefers-reduced-motion: reduce) {
  .add-btn { transition: none; }
}
</style>

<!-- 非 scoped：ElMessageBox 是挂到 body 上的，不在本组件的 scope 属性范围内，
     scoped 样式打不到它。只作用在自家 customClass 上，不会污染别处的确认框。 -->
<style>
.confirm-multiline .el-message-box__message { white-space: pre-line; }
</style>
