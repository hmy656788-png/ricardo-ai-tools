const CATEGORY_META = {
  banned: { label: '已封号', type: 'danger', advice: '先在官方页面确认账号状态；重复重试不能解除停用。' },
  network: { label: '网络 / 代理', type: 'warning', advice: '检查网络、代理和服务可达性；确认没有实际变更后再重试。' },
  account: { label: '账号 / 凭证', type: 'danger', advice: '核对当前密码、2FA、邮箱权限和平台额外验证要求。' },
  rate_limit: { label: '频率限制', type: 'warning', advice: '等待服务端允许的时间，避免连续追加任务。' },
  verification: { label: '验证未完成', type: 'warning', advice: '按官方页面完成邮箱、手机或 2FA 验证，再查看任务状态。' },
  unknown: { label: '未分类', type: 'info', advice: '先查看完整错误和任务日志，确认账号状态后再决定下一步。' },
}

const RULES = [
  ['banned', /deactivated|banned|账号.*(?:停用|删除|封禁)|被封禁/i],
  ['rate_limit', /429|rate.?limit|too many requests|请求过于频繁/i],
  ['verification', /验证码|2fa|otp|手机.*(?:验证|批准)|邮箱.*验证|captcha|challenge/i],
  ['account', /invalid[_ ](?:grant|credential|password)|password.*(?:wrong|incorrect)|密码.*错误|凭证.*失效|authentication failed|账号被/i],
  ['network', /timeout|timed out|network|connection|proxy|socks|dns|cloudflare|403 forbidden|连接.*(?:失败|错误|断开)/i],
]

export function normalizeCategory(category, error = '') {
  const value = String(category || '').trim().toLowerCase()
  if (CATEGORY_META[value]) return value
  const text = String(error || '')
  return RULES.find(([, pattern]) => pattern.test(text))?.[0] || 'unknown'
}

export function errorGuidance(category, error = '') {
  const key = normalizeCategory(category, error)
  const meta = CATEGORY_META[key] || CATEGORY_META.unknown
  return { key, ...meta }
}

export function categoryOptions() {
  return Object.entries(CATEGORY_META).map(([value, meta]) => ({ value, label: meta.label }))
}
