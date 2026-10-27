import http from './request'

// ──────────────── 单个注册 ────────────────
// 轻量版裁掉了注册功能；startRegister 仅保留 API 定义（无页面调用）。
export const startRegister = (payload) => http.post('/api/register', payload)

// ──────────────── 运行记录 ────────────────
export const listRuns = (limit = 50, category = '') =>
  http.get('/api/runs', { params: { limit, category } })

// ──────────────── 注册结果 / 导入账号 ────────────────
// source：'registered'（本站注册，默认）/ 'imported'（导入账号，独立表）。
// 两个数据源共用同一套端点与页面，后端按 source 路由到 registered / imported 表。
export const listRegistered = (params, source = 'registered') =>
  http.get('/api/registered', { params: { ...params, source } })

export const getRegistered = (email, source = 'registered') =>
  http.get(`/api/registered/${encodeURIComponent(email)}`, { params: { source } })

export const deleteRegistered = (email, source = 'registered') =>
  http.delete(`/api/registered/${encodeURIComponent(email)}`, { params: { source } })

// 手填凭证：不传的字段后端不动，传空串才是清空
export const updateCredentials = (payload, source = 'registered') =>
  http.post('/api/registered/update_credentials', { ...payload, source })

export const bulkDeleteRegistered = (payload, source = 'registered') =>
  http.post('/api/registered/bulk_delete', { ...payload, source })

// 轻量版不再用号池（邮箱列表）页，「下载并删除」只删导入账号表本身。
// 批量导出：格式清单由后端 export_formats.py 提供，加格式前端不用改
export const listExportFormats = () => http.get('/api/registered/export/formats')
export const exportRegistered = (payload, source = 'registered') =>
  http.post('/api/registered/export', { ...payload, source })

export const checkPlus = (emails, proxy = '', source = 'registered', proxyPool = '', preproxy = '') =>
  http.post('/api/registered/check_plus', { emails, proxy, source, proxy_pool: proxyPool, preproxy }, { timeout: 300000 })

// 补 refresh_token：对缺 RT 的号重新登录授权，拿新 RT 写回（返回每个号的 run_id）
export const backfillRt = (payload, source = 'registered') =>
  http.post('/api/registered/backfill_rt', { ...payload, source })
// 导入号专用补 RT：零邮箱接码依赖（密码+2FA 纯登录链），不碰号池。
export const importedBackfillRt = (payload) =>
  http.post('/api/imported/backfill_rt', payload)

// 刷新AT：三路径直换新 access_token（RT grant / 会话直提 / 密码+2FA 登录直提）。
// 路径3是真实登录，单号几十秒 —— 单独放宽到 5 分钟，避免 axios 60s 默认超时误报。
export const refreshAt = (payload, source = 'registered') =>
  http.post('/api/registered/refresh_at', { ...payload, source }, { timeout: 300000 })

// ──────────────── 导入账号 ────────────────
// 粘贴文本批量导入（邮箱----密码----2FA），后端逐行解析 + upsert 到 imported 独立表
export const importAccounts = (text) => http.post('/api/imported/import', { text })

export const exportToPanel = (email, targets) =>
  http.post('/api/registered/export_to_panel', { email, targets })

// ──────────────── 自动跑号 auto-loop ────────────────
export const autoStart = (payload) => http.post('/api/auto/start', payload)
export const autoPause = () => http.post('/api/auto/pause')
export const autoResume = () => http.post('/api/auto/resume')
export const autoStop = () => http.post('/api/auto/stop')
export const autoStatus = () => http.get('/api/auto/status')
