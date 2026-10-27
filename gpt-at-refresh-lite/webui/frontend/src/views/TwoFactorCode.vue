<script setup>
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, shallowRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { generateTotp, parseTotpInput } from '@/utils/totp'
import { HISTORY_KEY, HISTORY_LIMIT, readTotpHistory, saveTotpHistoryEntry, deleteTotpHistoryEntry, clearTotpHistory, maskTotpSecret } from '@/utils/totpHistory'

// 不读取账号库、不请求服务器；只有用户明确勾选后才在当前浏览器保存历史密钥。
// AdminLayout 使用 keep-alive，离开页面仍清理当前输入、历史的内存副本和计时器。
const draft = ref('')
const config = shallowRef(null)
const code = ref('')
const validUntil = ref(0)
const now = ref(Date.now())
const error = ref('')
const generating = ref(false)
const copying = ref(false)
const remember = ref(false)
const label = ref('')
const history = shallowRef([])
const historyError = ref('')
const activeHistoryId = ref(null)
let active = false
let revision = 0
let timer = null

const supported = Boolean(globalThis.crypto?.subtle)
const remaining = computed(() => Math.max(0, Math.ceil((validUntil.value - now.value) / 1000)))
const progress = computed(() => config.value ? Math.min(100, remaining.value / config.value.period * 100) : 0)
const formattedCode = computed(() => {
  if (!code.value) return '— — —'
  const middle = code.value.length / 2
  return `${code.value.slice(0, middle)} ${code.value.slice(middle)}`
})

function invalidate() {
  revision += 1
  activeHistoryId.value = null
  config.value = null
  code.value = ''
  validUntil.value = 0
  error.value = ''
  generating.value = false
  copying.value = false
}

function clearAll() {
  invalidate()
  draft.value = ''
  label.value = ''
  remember.value = false
}

async function refreshCode() {
  if (!config.value || generating.value || document.hidden) return
  const currentRevision = revision
  const currentConfig = config.value
  generating.value = true
  try {
    const result = await generateTotp(currentConfig)
    if (currentRevision !== revision || document.hidden) return
    now.value = Date.now()
    // 签名期间恰好跨过周期，丢弃旧码，下一次 tick 会重新生成。
    if (result.counter !== Math.floor(now.value / (currentConfig.period * 1000))) return
    code.value = result.code
    validUntil.value = result.validUntil
    return true
  } catch {
    if (currentRevision !== revision) return
    config.value = null
    code.value = ''
    validUntil.value = 0
    error.value = '无法生成验证码，请检查密钥，并使用支持 Web Crypto 的浏览器。'
  } finally {
    if (currentRevision === revision) generating.value = false
  }
}

function tick() {
  now.value = Date.now()
  if (!config.value) return
  const start = validUntil.value - config.value.period * 1000
  if (now.value >= validUntil.value || now.value < start) {
    code.value = ''
    validUntil.value = 0
    void refreshCode()
  }
}

async function generate() {
  invalidate()
  if (!supported) return
  const currentRevision = revision
  const shouldRemember = remember.value
  const historyLabel = label.value
  try {
    config.value = parseTotpInput(draft.value)
  } catch (cause) {
    error.value = cause.message
    return
  }
  const currentConfig = config.value
  const success = await refreshCode()
  if (success && currentRevision === revision && shouldRemember) {
    saveHistory(currentConfig, historyLabel)
  }
}

function loadHistory() {
  try {
    history.value = readTotpHistory()
    historyError.value = ''
    if (activeHistoryId.value && !history.value.some(item => item.id === activeHistoryId.value)) clearAll()
  } catch {
    history.value = []
    historyError.value = '无法读取本机历史。浏览器可能禁用了存储，或记录已损坏；可以重试或清空历史，临时生成不受影响。'
  }
}

function saveHistory(currentConfig, historyLabel) {
  try {
    history.value = saveTotpHistoryEntry(currentConfig, historyLabel)
    activeHistoryId.value = history.value[0]?.id || null
    historyError.value = ''
    ElMessage.success('已保存到本机历史')
  } catch {
    historyError.value = '验证码已生成，但历史保存失败。请检查浏览器存储权限或空间；已有历史未被主动清空。'
    ElMessage.warning('历史未保存，当前验证码仍可使用')
  }
}

async function reuseHistory(id) {
  // 使用前重新读取，避免复用已在另一标签页删除的记录。
  loadHistory()
  const item = history.value.find(entry => entry.id === id)
  if (!item) return
  clearAll()
  const currentRevision = revision
  const params = new URLSearchParams({ secret: item.secret, algorithm: item.algorithm, digits: String(item.digits), period: String(item.period), issuer: item.issuer })
  // 仅构造输入值，绝不导航或发起请求；完整保留非默认算法、位数和周期。
  draft.value = `otpauth://totp/${encodeURIComponent(item.account)}?${params}`
  label.value = item.label
  config.value = parseTotpInput(draft.value)
  activeHistoryId.value = item.id
  const success = await refreshCode()
  if (success && currentRevision === revision) saveHistory(config.value, item.label)
}

async function removeHistory(id) {
  try {
    await ElMessageBox.confirm('删除这条本机历史及保存的密钥？不会关闭账号的两步验证。', '删除历史', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  if (!active) return
  try {
    history.value = deleteTotpHistoryEntry(id)
    historyError.value = ''
    if (activeHistoryId.value === id) clearAll()
    ElMessage.success('历史记录已删除')
  } catch { ElMessage.error('删除失败，请检查浏览器存储权限后重试') }
}

async function removeAllHistory() {
  try {
    await ElMessageBox.confirm('清空当前浏览器中全部 2FA 历史及保存的密钥？此操作无法撤销，不影响账号库或账号的两步验证。', '清空历史', {
      confirmButtonText: '清空历史', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  if (!active) return
  try {
    history.value = clearTotpHistory()
    historyError.value = ''
    clearAll()
    ElMessage.success('本机历史已清空')
  } catch { ElMessage.error('清空失败，请检查浏览器存储权限后重试') }
}

function handleHistoryStorage(event) {
  if (event.key === HISTORY_KEY || event.key === null) loadHistory()
}

function historyName(item) {
  return item.label || [item.issuer, item.account].filter(Boolean).join(' · ') || '未命名密钥'
}

function formatHistoryTime(timestamp) {
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
}

async function copyCode() {
  if (!config.value || copying.value) return
  if (!navigator.clipboard?.writeText) {
    ElMessage.warning('浏览器不支持自动复制，请选中验证码手动复制。')
    return
  }
  const currentRevision = revision
  const currentConfig = config.value
  copying.value = true
  try {
    // 点击时重新计算，避免复制因后台挂起或周期交接而过期的显示值。
    const result = await generateTotp(currentConfig)
    if (currentRevision !== revision || document.hidden) return
    const timestamp = Date.now()
    if (result.counter !== Math.floor(timestamp / (currentConfig.period * 1000))) {
      tick()
      ElMessage.info('验证码刚刚更新，请重新点击复制。')
      return
    }
    if (currentConfig.period > 1 && result.validUntil - timestamp < 1000) {
      ElMessage.info('验证码即将更新，请稍后再复制新码。')
      return
    }
    code.value = result.code
    validUntil.value = result.validUntil
    now.value = timestamp
    await navigator.clipboard.writeText(result.code)
    if (currentRevision === revision) {
      if (Date.now() >= result.validUntil) {
        tick()
        ElMessage.warning('复制时验证码已过期，请重新复制新码。')
      } else {
        ElMessage.success('验证码已复制')
      }
    }
  } catch {
    if (currentRevision === revision) ElMessage.warning('复制未完成，请选中验证码手动复制。')
  } finally {
    if (currentRevision === revision) copying.value = false
  }
}

function stopTimer() {
  if (timer !== null) window.clearInterval(timer)
  timer = null
}

function handleVisibility() {
  stopTimer()
  if (document.hidden) {
    code.value = ''
    validUntil.value = 0
    return
  }
  tick()
  timer = window.setInterval(tick, 250)
}

function activate() {
  if (active) return
  active = true
  loadHistory()
  document.addEventListener('visibilitychange', handleVisibility)
  window.addEventListener('pagehide', clearAll)
  window.addEventListener('storage', handleHistoryStorage)
  handleVisibility()
}

function deactivate() {
  active = false
  stopTimer()
  document.removeEventListener('visibilitychange', handleVisibility)
  window.removeEventListener('pagehide', clearAll)
  window.removeEventListener('storage', handleHistoryStorage)
  clearAll()
  history.value = []
}

onMounted(activate)
onActivated(activate)
onDeactivated(deactivate)
onBeforeUnmount(deactivate)
</script>

<template>
  <div class="page totp-page">
    <header class="totp-heading">
      <div>
        <h1>2FA验证码获取</h1>
        <p>把你的 2FA 密钥转换为动态验证码，无需打开第三方网站。</p>
      </div>
      <span class="local-badge"><el-icon><Lock /></el-icon> 浏览器本地计算</span>
    </header>

    <el-alert
      v-if="!supported"
      title="当前环境不支持本地加密运算"
      description="请使用最新版浏览器，并通过 localhost、127.0.0.1 或 HTTPS 打开本工具。"
      type="error"
      :closable="false"
      show-icon
      class="support-alert"
    />

    <div class="totp-grid">
      <el-card shadow="never" class="input-card">
        <template #header><h2>输入 2FA 密钥</h2></template>
        <form @submit.prevent="generate" autocomplete="off">
          <label for="totp-secret" class="input-label">密钥或 otpauth 链接</label>
          <el-input
            id="totp-secret"
            v-model="draft"
            type="password"
            show-password
            autocomplete="off"
            :spellcheck="false"
            autocapitalize="off"
            :maxlength="4096"
            placeholder="粘贴 Base32 密钥或 otpauth://totp/…"
            :aria-invalid="Boolean(error)"
            aria-describedby="totp-input-help totp-error"
            @input="invalidate"
          />
          <p id="totp-input-help" class="hint input-help">普通密钥默认使用 SHA-1、6 位码、30 秒周期。otpauth 链接会自动读取参数。</p>
          <div class="remember-options">
            <el-checkbox v-model="remember" aria-describedby="totp-history-warning">保存到本机历史，方便下次使用</el-checkbox>
            <p id="totp-history-warning" class="hint">仅在勾选后生成成功时保存。密钥会以明文保存在当前浏览器中，其他能使用此浏览器的人也可能读取；请勿在公共设备上开启。</p>
            <template v-if="remember">
              <label for="totp-history-label" class="input-label">备注（可选）</label>
              <el-input id="totp-history-label" v-model="label" :maxlength="80" autocomplete="off" placeholder="例如：工作账号，请勿填写密码或密钥" />
            </template>
          </div>
          <p v-if="error" id="totp-error" class="input-error" role="alert">{{ error }}</p>
          <div class="input-actions">
            <el-button type="primary" native-type="submit" :disabled="!draft.trim() || !supported" :loading="generating">生成验证码</el-button>
            <el-button native-type="button" :disabled="!draft" @click="clearAll">清空</el-button>
          </div>
        </form>
        <div class="privacy-note">
          <el-icon><Lock /></el-icon>
          <p>密钥和验证码不上传，也不读取账号库。切换功能或刷新会清空当前输入；主动保存的历史会保留在当前浏览器，可在下方删除。</p>
        </div>
      </el-card>

      <el-card shadow="never" class="code-card">
        <div class="code-status"><span :class="['status-light', { active: code }]" />{{ code ? '正在自动更新' : generating ? '正在计算' : '等待输入密钥' }}</div>
        <div class="code-value mono" aria-label="当前验证码" :class="{ empty: !code }">{{ formattedCode }}</div>
        <template v-if="config && code">
          <p class="countdown">{{ remaining <= 5 ? '即将更新' : '距离下次更新' }} <strong>{{ remaining }}</strong> 秒</p>
          <el-progress :percentage="progress" :show-text="false" :stroke-width="4" :color="remaining <= 5 ? 'var(--el-color-warning)' : 'var(--brand)'" />
          <div class="code-options">{{ config.algorithm }} <span>·</span> {{ config.digits }} 位 <span>·</span> {{ config.period }} 秒</div>
        </template>
        <p v-else class="code-placeholder">生成后会自动刷新，无需反复点击。</p>
        <el-button class="copy-button" type="primary" :disabled="!code" :loading="copying" @click="copyCode"><el-icon><CopyDocument /></el-icon><span>复制验证码</span></el-button>
      </el-card>
    </div>

    <el-card shadow="never" class="history-card" aria-labelledby="totp-history-title">
      <template #header>
        <div class="history-heading">
          <div>
            <h2 id="totp-history-title">本机历史 <span class="history-count">{{ history.length }} / {{ HISTORY_LIMIT }}</span></h2>
            <p class="hint">最近使用在前，同一密钥与参数自动合并；超过 {{ HISTORY_LIMIT }} 条会移除最旧记录。不保存过期验证码。</p>
          </div>
          <el-button type="danger" plain :disabled="!history.length && !historyError" @click="removeAllHistory">清空历史</el-button>
        </div>
      </template>
      <div v-if="historyError" class="history-error" role="alert">
        <p>{{ historyError }}</p>
        <el-button size="small" @click="loadHistory">重新读取</el-button>
      </div>
      <ul v-if="history.length" class="history-list" aria-label="2FA 本机历史记录">
        <li v-for="item in history" :key="item.id" class="history-row">
          <div class="history-details">
            <h3>{{ historyName(item) }}</h3>
            <p class="history-meta"><span class="mono">{{ maskTotpSecret(item.secret) }}</span><span>{{ item.algorithm }} · {{ item.digits }} 位 · {{ item.period }} 秒</span></p>
            <p class="history-time">最近使用：{{ formatHistoryTime(item.lastUsedAt) }}</p>
          </div>
          <div class="history-actions">
            <el-button type="primary" plain :disabled="!supported || generating" @click="reuseHistory(item.id)">再次生成</el-button>
            <el-button type="danger" text @click="removeHistory(item.id)">删除</el-button>
          </div>
        </li>
      </ul>
      <div v-else-if="!historyError" class="history-empty">
        <p>暂无历史记录</p>
        <p class="hint">勾选“保存到本机历史”后生成验证码，即可在这里再次使用。</p>
      </div>
      <p class="history-footnote hint">历史只属于当前浏览器和访问地址，不会跨浏览器同步；清除网站数据也会清空历史。列表中的密钥仅作遮挡显示，并非加密存储。</p>
    </el-card>

    <section class="usage-note" aria-label="使用说明">
      <h2>使用前了解</h2>
      <p>这是验证器动态码工具，不是短信或邮箱接码。你需要已有的 2FA 密钥；它不能从邮箱、密码或旧验证码恢复密钥。</p>
      <p>验证码不通过时，请先确认密钥正确、电脑时间已自动同步。复制会把验证码放入系统剪贴板；不要分享密钥。</p>
    </section>
  </div>
</template>

<style scoped>
.totp-page { max-width: 1100px; margin: 0 auto; }
.totp-heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin: 8px 0 26px; }
.totp-heading h1 { color: var(--app-title); font-size: 25px; font-weight: 600; margin: 0 0 10px; }
.totp-heading p { color: var(--el-text-color-secondary); font-size: 14px; line-height: 1.6; margin: 0; }
.local-badge { display: inline-flex; gap: 7px; align-items: center; flex-shrink: 0; border: 1px solid var(--app-border); border-radius: 20px; padding: 8px 12px; color: var(--el-text-color-regular); font-size: 12px; }
.totp-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 20px; }
.totp-grid .el-card { margin: 0; border-radius: 12px; }
h2 { font-size: 15px; font-weight: 600; margin: 0; }
.input-card :deep(.el-card__body) { padding: 24px; }
.input-label { display: block; font-size: 13px; margin-bottom: 10px; }
.input-help { margin-top: 12px; }
.input-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
.input-actions .el-button { margin-left: 0; }
.input-error { font-size: 13px; line-height: 1.5; color: var(--el-color-danger); overflow-wrap: anywhere; }
.remember-options { margin-top: 18px; }
.remember-options .el-checkbox { height: auto; align-items: flex-start; }
.remember-options :deep(.el-checkbox__input) { margin-top: 3px; }
.remember-options :deep(.el-checkbox__label) { white-space: normal; line-height: 1.5; }
.remember-options .hint { margin: 8px 0 14px; font-size: 12px; line-height: 1.7; color: var(--el-text-color-secondary); }
.privacy-note { display: flex; gap: 10px; padding-top: 22px; margin-top: 26px; border-top: 1px solid var(--app-border); color: var(--el-text-color-secondary); }
.privacy-note .el-icon { margin-top: 3px; flex-shrink: 0; }
.privacy-note p { margin: 0; font-size: 12px; line-height: 1.8; }
.code-card :deep(.el-card__body) { padding: 30px; text-align: center; }
.code-status { display: flex; align-items: center; justify-content: center; gap: 8px; color: var(--el-text-color-secondary); font-size: 12px; }
.status-light { height: 6px; width: 6px; border-radius: 50%; background: var(--el-text-color-placeholder); }
.status-light.active { background: var(--brand); }
.code-value { font-size: clamp(30px, 4vw, 46px); font-weight: 600; letter-spacing: 2px; font-variant-numeric: tabular-nums; margin: 24px 0 16px; color: var(--app-title); white-space: nowrap; user-select: all; }
.code-value.empty { color: var(--el-text-color-placeholder); }
.countdown { font-size: 12px; color: var(--el-text-color-secondary); margin: 0 0 14px; }
.countdown strong { font-weight: 600; color: var(--app-title); font-variant-numeric: tabular-nums; }
.code-options { font-size: 11px; color: var(--el-text-color-secondary); margin: 14px 0 20px; }
.code-options span { margin: 0 8px; }
.code-placeholder { font-size: 12px; color: var(--el-text-color-secondary); margin: 24px 0 32px; }
.copy-button { width: 100%; }
.copy-button span { margin-left: 6px; }
.history-card { margin-top: 24px; border-radius: 12px; }
.history-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.history-count { font-size: 12px; font-weight: 400; color: var(--el-text-color-secondary); margin-left: 8px; }
.history-heading p { margin: 8px 0 0; font-size: 12px; line-height: 1.7; }
.history-heading > .el-button { flex-shrink: 0; }
.history-list { list-style: none; margin: 0; padding: 0; }
.history-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 16px 0; border-bottom: 1px solid var(--app-border); }
.history-row:first-child { padding-top: 0; }
.history-row:last-child { border-bottom: 0; }
.history-details { min-width: 0; }
.history-details h3 { margin: 0 0 8px; font-size: 14px; font-weight: 500; overflow-wrap: anywhere; }
.history-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; font-size: 12px; margin: 0; color: var(--el-text-color-regular); }
.history-time { margin: 8px 0 0; font-size: 12px; color: var(--el-text-color-secondary); }
.history-actions { display: flex; gap: 8px; flex-shrink: 0; }
.history-actions .el-button { margin-left: 0; }
.history-footnote { border-top: 1px solid var(--app-border); padding-top: 14px; margin: 12px 0 0; line-height: 1.7; font-size: 12px; }
.history-empty { text-align: center; padding: 12px 0 22px; }
.history-empty p { margin: 8px 0; }
.history-empty .hint { font-size: 12px; line-height: 1.7; }
.history-error { padding-bottom: 16px; color: var(--el-color-danger); font-size: 13px; line-height: 1.7; }
.usage-note { margin-top: 28px; padding: 0 4px; }
.usage-note h2 { color: var(--el-text-color-regular); font-size: 13px; }
.usage-note p { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.8; margin: 8px 0; }
.support-alert { margin-bottom: 16px; }
@media (max-width: 980px) {
  .totp-grid { grid-template-columns: minmax(0, 1fr); }
  .totp-heading { align-items: flex-start; flex-direction: column; gap: 12px; }
  .code-value { font-size: 38px; }
}
@media (max-width: 480px) {
  .input-card :deep(.el-card__body), .code-card :deep(.el-card__body) { padding: 16px; }
  .code-value { font-size: 28px; letter-spacing: 0; }
}
@media (max-width: 640px) {
  .history-heading, .history-row { align-items: flex-start; flex-direction: column; gap: 12px; }
  .history-actions { flex-wrap: wrap; }
}
</style>
