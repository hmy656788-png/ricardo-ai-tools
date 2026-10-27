<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Link, CopyDocument } from '@element-plus/icons-vue'

const CURRENT_VERSION = '2.0.0'
const STORAGE_KEY = 'ricardo-update-repositories-v1'
const DEFAULTS_MERGED_KEY = 'ricardo-update-default-repositories-v1'
const CHECKED_KEY = 'ricardo-update-last-checked-v1'
const CHECK_INTERVAL = 24 * 60 * 60 * 1000
const DEFAULT_REPOSITORIES = [
  'FuZoe/turb-gpt-free-register',
  'ludwigxs/muse-register',
  'KC-CatK/KC-PAY-GPT',
]

// 直接把用户此前提供的仓库显示在输入框，避免只显示为下方标签而看起来像未填写。
const repoInput = ref(DEFAULT_REPOSITORIES[0])
const repositories = ref([])
const results = ref([])
const loading = ref(false)
const lastChecked = ref(0)
const backups = ref([])
const backupLoading = ref(false)
const backupError = ref('')
let timer = null

const hasConfiguredRepo = computed(() => repositories.value.length > 0)
const checkedLabel = computed(() => lastChecked.value ? new Date(lastChecked.value).toLocaleString('zh-CN', { hour12: false }) : '尚未检查')
const resultSummary = computed(() => ({
  total: results.value.length,
  errors: results.value.filter((item) => item.error).length,
  available: results.value.filter((item) => item.updateAvailable).length,
}))

function loadLocalState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    const savedRepositories = Array.isArray(parsed)
      ? parsed.filter((item) => /^[\w.-]+\/[\w.-]+$/.test(item))
      : []
    const defaultsMerged = localStorage.getItem(DEFAULTS_MERGED_KEY) === '1'
    const shouldMergeDefaults = !defaultsMerged || !savedRepositories.length
    repositories.value = shouldMergeDefaults
      ? [...new Set([...DEFAULT_REPOSITORIES, ...savedRepositories])]
      : savedRepositories
    if (shouldMergeDefaults) {
      persistRepositories()
      localStorage.setItem(DEFAULTS_MERGED_KEY, '1')
    }
    lastChecked.value = Number(localStorage.getItem(CHECKED_KEY) || 0) || 0
  } catch {
    repositories.value = []
    lastChecked.value = 0
  }
}

function persistRepositories() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(repositories.value))
}

function addRepository() {
  const value = repoInput.value.trim().replace(/^https?:\/\/(?:www\.)?github\.com\//i, '').replace(/\.git$/, '').replace(/\/$/, '')
  if (!/^[\w.-]+\/[\w.-]+$/.test(value)) {
    ElMessage.warning('请输入 GitHub 仓库，例如 owner/repository')
    return
  }
  if (!repositories.value.includes(value)) repositories.value.push(value)
  persistRepositories()
  repoInput.value = ''
  void checkForUpdates(true)
}

function removeRepository(repo) {
  repositories.value = repositories.value.filter((item) => item !== repo)
  results.value = results.value.filter((item) => item.repo !== repo)
  persistRepositories()
}

function compareVersions(latest, current = CURRENT_VERSION) {
  const parse = (value) => String(value || '').replace(/^v/i, '').split('-')[0].split('.').map((part) => Number.parseInt(part, 10) || 0)
  const a = parse(latest); const b = parse(current)
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0) ? 1 : -1
  }
  return 0
}

async function fetchRelease(repo) {
  const response = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, {
    headers: { Accept: 'application/vnd.github+json' },
    signal: AbortSignal.timeout(12000),
  })
  if (response.status === 404) {
    // GitHub 对“仓库存在但没有 Release”和“仓库不存在”都返回 404，
    // 再读一次仓库元数据，避免把两种情况混成一个误导性错误。
    const repositoryResponse = await fetch(`https://api.github.com/repos/${repo}`, {
      headers: { Accept: 'application/vnd.github+json' },
      signal: AbortSignal.timeout(12000),
    })
    if (repositoryResponse.ok) {
      const repository = await repositoryResponse.json()
      return {
        repo,
        version: '',
        title: '尚未发布 Release',
        publishedAt: '',
        notes: '',
        url: repository.html_url || `https://github.com/${repo}`,
        assets: [],
        updateAvailable: false,
        noRelease: true,
        checkedAt: Date.now(),
      }
    }
    throw new Error(repositoryResponse.status === 404 ? '仓库不存在或无权访问' : `GitHub 返回 HTTP ${repositoryResponse.status}`)
  }
  if (!response.ok) throw new Error(`GitHub 返回 HTTP ${response.status}`)
  const release = await response.json()
  const version = release.tag_name || release.name || ''
  return {
    repo,
    version,
    title: release.name || release.tag_name || '未命名版本',
    publishedAt: release.published_at || release.created_at || '',
    notes: String(release.body || '').trim(),
    url: release.html_url || `https://github.com/${repo}/releases`,
    assets: Array.isArray(release.assets) ? release.assets.map((asset) => ({ name: asset.name, url: asset.browser_download_url })).filter((asset) => asset.name && asset.url) : [],
    updateAvailable: compareVersions(version) > 0,
    checkedAt: Date.now(),
  }
}

async function checkForUpdates(force = false) {
  if (loading.value || !hasConfiguredRepo.value) return
  if (!force && lastChecked.value && Date.now() - lastChecked.value < CHECK_INTERVAL && results.value.length) return
  loading.value = true
  const next = []
  for (const repo of repositories.value) {
    try { next.push(await fetchRelease(repo)) } catch (error) { next.push({ repo, error: error.message, checkedAt: Date.now() }) }
  }
  results.value = next
  lastChecked.value = Date.now()
  localStorage.setItem(CHECKED_KEY, String(lastChecked.value))
  loading.value = false
}

function formatDate(value) {
  if (!value) return '未知时间'
  try { return new Date(value).toLocaleString('zh-CN', { hour12: false }) } catch { return value }
}

async function apiError(response, fallback) {
  let detail = ''
  try {
    const payload = await response.json()
    detail = payload.detail || payload.message || ''
  } catch { /* non-JSON response */ }
  if (response.status === 404) detail = '当前主面板尚未加载备份接口，请重启 start_webui.py 后再试'
  return new Error(detail || `${fallback}（HTTP ${response.status}）`)
}

function openRelease(url) {
  window.open(url, '_blank', 'noopener,noreferrer')
}

function openRepository(repo) {
  openRelease(`https://github.com/${repo}`)
}

async function copyGptPrompt(item) {
  const prompt = `请分析这个 GitHub Release 更新，只基于下面公开信息回答：\n\n仓库：${item.repo}\n版本：${item.version || '未标记'}\n标题：${item.title}\n发布时间：${formatDate(item.publishedAt)}\n变更说明：\n${item.notes || '（无）'}\n\n请输出：1. 主要变化；2. 对现有 Ricardo AI 工具的兼容性风险；3. 是否需要手动迁移或备份；4. 建议的人工检查清单。不要执行下载、安装或修改文件。`
  try {
    await navigator.clipboard.writeText(prompt)
    ElMessage.success('GPT 分析提示已复制，请粘贴到 GPT。')
  } catch {
    ElMessage.warning('无法自动复制，请手动复制 Release 说明。')
  }
}

async function loadBackups() {
  backupError.value = ''
  try {
    const response = await fetch('/api/backups')
    if (!response.ok) throw await apiError(response, '无法读取本地备份')
    const data = await response.json()
    backups.value = Array.isArray(data.backups) ? data.backups : []
  } catch (error) {
    backupError.value = error.message
    ElMessage.warning(`无法读取本地备份：${error.message}`)
  }
}

async function createBackup() {
  if (backupLoading.value) return
  backupLoading.value = true
  backupError.value = ''
  try {
    const response = await fetch('/api/backups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'before-manual-update' }),
    })
    if (!response.ok) throw await apiError(response, '创建备份失败')
    ElMessage.success('本地备份已创建；更新仍需你手动进行。')
    await loadBackups()
  } catch (error) {
    backupError.value = error.message
    ElMessage.error(`创建备份失败：${error.message}`)
  } finally { backupLoading.value = false }
}

async function restoreBackup(item) {
  try {
    await ElMessageBox.confirm(
      `这会用 ${item.filename} 替换当前本地数据库，并自动再做一份回滚前备份。确认继续？`,
      '确认回滚',
      { type: 'warning', confirmButtonText: '继续回滚', cancelButtonText: '取消' },
    )
  } catch { return }
  backupLoading.value = true
  backupError.value = ''
  try {
    const response = await fetch(`/api/backups/${encodeURIComponent(item.id)}/restore`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: 'RESTORE' }),
    })
    if (!response.ok) throw await apiError(response, '回滚失败')
    ElMessage.success('已回滚本地数据库；建议刷新页面确认状态。')
    await loadBackups()
  } catch (error) {
    backupError.value = error.message
    ElMessage.error(`回滚失败：${error.message}`)
  } finally { backupLoading.value = false }
}

onMounted(() => {
  loadLocalState()
  void loadBackups()
  if (hasConfiguredRepo.value) void checkForUpdates()
  timer = window.setInterval(() => { void checkForUpdates() }, CHECK_INTERVAL)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <div class="page update-center-page">
    <header class="update-header">
      <div>
        <h1>更新中心</h1>
        <p>只读检查 GitHub Release，发现新版本后由你手动决定是否更新。</p>
      </div>
      <div class="update-header-actions">
        <div class="header-version">当前版本 <strong>v{{ CURRENT_VERSION }}</strong></div>
        <el-button type="primary" :loading="loading" :disabled="!hasConfiguredRepo" @click="checkForUpdates(true)">
          <el-icon><Refresh /></el-icon><span>立即检查</span>
        </el-button>
      </div>
    </header>

    <div class="update-summary" aria-label="更新状态摘要">
      <div class="summary-item"><span class="summary-label">已配置仓库</span><strong>{{ repositories.length }}</strong><small>仅保存在本机</small></div>
      <div class="summary-item"><span class="summary-label">上次检查</span><strong>{{ lastChecked ? checkedLabel : '尚未检查' }}</strong><small>每 24 小时自动检查</small></div>
      <div class="summary-item" :class="{ 'is-warning': resultSummary.available }"><span class="summary-label">可用更新</span><strong>{{ resultSummary.available }}</strong><small>{{ resultSummary.available ? '需要人工核对' : '暂无发现新版本' }}</small></div>
      <div class="summary-item" :class="{ 'is-danger': resultSummary.errors }"><span class="summary-label">检查异常</span><strong>{{ resultSummary.errors }}</strong><small>{{ resultSummary.errors ? '请检查仓库地址' : '连接状态正常' }}</small></div>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="安全规则：本页面不会自动下载、解压、覆盖或执行更新文件。"
      description="检查请求只读取 GitHub 的公开 Release 信息；下载链接仅供你手动核对和操作。"
      class="update-safety"
    />

    <el-card shadow="never" class="backup-card">
      <template #header><div class="card-title"><span>更新前备份与回滚</span><el-tag size="small" type="warning">仅本机</el-tag></div></template>
      <p class="hint">备份只保存在本机，不上传 GitHub，也不包含可供页面读取的凭证内容。建议手动安装更新前先创建备份。</p>
      <div class="backup-actions">
        <el-button type="primary" :loading="backupLoading" @click="createBackup">创建本地备份</el-button>
        <span class="hint">回滚会替换本地数据库，必须二次确认</span>
      </div>
      <el-alert v-if="backupError" type="warning" :closable="false" show-icon class="backup-error" title="备份服务暂不可用">
        <template #default>
          <div class="backup-error-content"><span>{{ backupError }}</span><el-button link type="warning" :loading="backupLoading" @click="loadBackups">重新读取</el-button></div>
        </template>
      </el-alert>
      <el-table v-if="backups.length" :data="backups" size="small" class="backup-table">
        <el-table-column prop="filename" label="备份文件" min-width="250" show-overflow-tooltip />
        <el-table-column label="创建时间" width="180"><template #default="scope">{{ formatDate(scope.row.created_at * 1000) }}</template></el-table-column>
        <el-table-column label="操作" width="110"><template #default="scope"><el-button size="small" type="warning" plain :loading="backupLoading" @click="restoreBackup(scope.row)">回滚</el-button></template></el-table-column>
      </el-table>
      <p v-else class="backup-empty">暂无本地备份</p>
    </el-card>

    <el-card shadow="never" class="repo-card">
      <template #header><div class="card-title"><div class="title-stack"><span>检查哪些仓库</span><small>支持 owner/repository 或 GitHub 链接</small></div><el-tag size="small" type="info">{{ repositories.length }} 个仓库</el-tag></div></template>
      <div class="repo-add-row">
        <el-input v-model="repoInput" placeholder="owner/repository 或 GitHub 仓库链接" clearable @keyup.enter="addRepository" />
        <el-button type="primary" @click="addRepository"><el-icon><Plus /></el-icon>添加仓库</el-button>
      </div>
      <p class="hint">已默认填写你提供的仓库；可直接点击添加仓库或替换为其他 owner/repository。仓库列表只保存在本机浏览器，不会上传账号或凭证。</p>
      <div v-if="repositories.length" class="repo-list">
        <el-tag v-for="repo in repositories" :key="repo" closable @close="removeRepository(repo)">{{ repo }}</el-tag>
      </div>
      <el-empty v-else :image-size="54" description="尚未配置 GitHub 仓库" />
    </el-card>

    <section class="update-results" aria-live="polite">
      <div class="results-meta"><span>检查结果 <strong v-if="results.length">{{ results.length }} 个仓库</strong><span v-else>尚未产生结果</span></span><span>上次检查：{{ checkedLabel }}</span><span v-if="loading" class="loading-label">正在读取公开 Release…</span></div>
      <el-card v-for="item in results" :key="item.repo" shadow="never" class="release-card" :class="{ available: item.updateAvailable }">
        <template #header>
          <div class="release-heading"><div><h2>{{ item.repo }}</h2><p v-if="item.error" class="error-text">{{ item.error }}</p><p v-else-if="item.noRelease" class="no-release-text">仓库可访问 · 暂无 Release</p><p v-else>Release：{{ item.version || '未标记版本' }} · {{ formatDate(item.publishedAt) }}</p></div><el-tag v-if="!item.error" :type="item.noRelease ? 'info' : (item.updateAvailable ? 'warning' : 'success')">{{ item.noRelease ? '暂无 Release' : (item.updateAvailable ? '发现新版本' : '已是最新') }}</el-tag></div>
        </template>
        <template v-if="item.error">
          <div class="release-error-body">
            <el-alert type="warning" :closable="false" show-icon :title="item.error" description="请确认仓库名称、访问权限以及是否已发布 GitHub Release。" />
            <div class="release-actions">
              <el-button size="small" @click="openRepository(item.repo)"><el-icon><Link /></el-icon>打开仓库</el-button>
              <el-button size="small" plain :loading="loading" @click="checkForUpdates(true)"><el-icon><Refresh /></el-icon>重新检查</el-button>
            </div>
          </div>
        </template>
        <template v-else>
          <el-alert v-if="item.noRelease" type="info" :closable="false" show-icon title="仓库已连接，但还没有发布 Release" description="发布第一个 GitHub Release 后，更新中心会在下一次检查时显示版本和变更说明。" class="no-release-alert" />
          <template v-else>
            <p class="release-title">{{ item.title }}</p>
            <pre v-if="item.notes" class="release-notes">{{ item.notes }}</pre>
            <p v-else class="hint">该 Release 没有提供变更说明。</p>
          </template>
          <div class="release-actions">
            <el-button size="small" @click="openRelease(item.url)"><el-icon><Link /></el-icon>{{ item.noRelease ? '打开仓库' : '查看 Release' }}</el-button>
            <el-button v-if="!item.noRelease" size="small" plain @click="copyGptPrompt(item)"><el-icon><CopyDocument /></el-icon>复制 GPT 分析提示</el-button>
            <el-button v-for="asset in item.assets" :key="asset.url" size="small" plain @click="openRelease(asset.url)">手动查看 {{ asset.name }}</el-button>
          </div>
          <p v-if="item.updateAvailable" class="manual-note">需要手动更新：请先备份，再核对变更与兼容性，确认后自行安装。</p>
        </template>
      </el-card>
      <el-empty v-if="!results.length && !loading" :image-size="70" description="配置仓库并点击「立即检查」，查看公开 Release" />
      <div v-if="loading && !results.length" class="results-loading" role="status"><el-icon class="is-loading"><Refresh /></el-icon><span>正在读取 GitHub Release…</span></div>
    </section>
  </div>
</template>

<style scoped>
.update-center-page { max-width: 1100px; margin: 0 auto; padding-bottom: 28px; }
.update-header { display:flex; align-items:center; justify-content:space-between; gap:20px; margin:8px 0 18px; }
.update-header h1 { margin:0 0 8px; color:var(--app-title); font-size:25px; font-weight:600; }
.update-header p { margin:0; color:var(--el-text-color-secondary); font-size:14px; }
.update-header-actions { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
.header-version { color:var(--el-text-color-secondary); font-size:12px; white-space:nowrap; }
.header-version strong { color:var(--app-title); font-weight:600; }
.update-summary { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; margin:0 0 16px; }
.summary-item { min-width:0; padding:14px 16px; border:1px solid var(--app-border); border-radius:10px; background:var(--el-bg-color); }
.summary-label, .summary-item small { display:block; color:var(--el-text-color-secondary); font-size:12px; line-height:1.5; }
.summary-item strong { display:block; margin:4px 0 2px; overflow:hidden; color:var(--app-title); font-size:18px; font-weight:600; text-overflow:ellipsis; white-space:nowrap; }
.summary-item.is-warning { border-color:color-mix(in srgb, var(--el-color-warning) 42%, var(--app-border)); background:color-mix(in srgb, var(--el-color-warning) 7%, var(--el-bg-color)); }
.summary-item.is-warning strong { color:var(--el-color-warning); }
.summary-item.is-danger { border-color:color-mix(in srgb, var(--el-color-danger) 35%, var(--app-border)); background:color-mix(in srgb, var(--el-color-danger) 5%, var(--el-bg-color)); }
.summary-item.is-danger strong { color:var(--el-color-danger); }
.update-safety { margin-bottom:16px; }
.repo-card, .release-card, .backup-card { margin-bottom:16px; border-color:var(--app-border); background:var(--el-bg-color); }
.card-title, .release-heading, .repo-add-row, .release-actions, .results-meta { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
.title-stack { display:flex; flex-direction:column; gap:2px; }
.title-stack small { color:var(--el-text-color-secondary); font-size:12px; font-weight:400; }
.repo-add-row { justify-content:flex-start; }
.repo-add-row .el-input { max-width:520px; }
.hint, .results-meta { color:var(--el-text-color-secondary); font-size:12px; line-height:1.7; }
.repo-list { display:flex; gap:8px; flex-wrap:wrap; }
.release-heading h2 { margin:0 0 4px; color:var(--app-title); font-size:16px; font-weight:600; }
.release-heading p { margin:0; color:var(--el-text-color-secondary); font-size:12px; }
.no-release-text { color:var(--el-color-info) !important; }
.release-card.available { border-color:var(--el-color-warning); }
.release-title { margin:0 0 10px; color:var(--app-title); font-weight:500; }
.release-notes { max-height:260px; overflow:auto; margin:0 0 14px; padding:12px; border:1px solid var(--app-border); border-radius:6px; background:var(--el-fill-color-lighter); color:var(--el-text-color-regular); font:12px/1.7 var(--app-font-mono); white-space:pre-wrap; overflow-wrap:anywhere; }
.release-actions { justify-content:flex-start; }
.backup-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
.backup-error { margin:0 0 14px; }
.backup-error-content { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; width:100%; }
.backup-table { width:100%; }
.backup-empty { margin:0; color:var(--el-text-color-secondary); font-size:12px; }
.manual-note { margin:14px 0 0; color:var(--el-color-warning); font-size:12px; line-height:1.7; }
.error-text { color:var(--el-color-danger) !important; }
.release-error-body { padding-top:2px; }
.release-error-body .el-alert { margin-bottom:14px; }
.no-release-alert { margin-bottom:14px; }
.loading-label { color:var(--el-color-primary); }
.results-loading { display:flex; align-items:center; justify-content:center; gap:8px; min-height:150px; color:var(--el-text-color-secondary); font-size:13px; }
.results-loading .el-icon { color:var(--el-color-primary); }
@media (max-width:900px) { .update-summary { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
@media (max-width:700px) { .update-header { align-items:flex-start; flex-direction:column; } .update-header-actions { width:100%; justify-content:space-between; } .repo-add-row .el-input { max-width:none; flex:1; } }
@media (max-width:480px) { .update-summary { grid-template-columns:1fr; } .update-header-actions { align-items:flex-start; flex-direction:column; } }
</style>
