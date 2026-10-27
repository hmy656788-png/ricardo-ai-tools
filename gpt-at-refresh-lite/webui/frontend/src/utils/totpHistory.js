import { parseTotpInput } from './totp.js'

// Deliberately separate from the form/proxy stores: this is an explicit opt-in
// convenience for the 2FA page, not a general account vault.
export const HISTORY_KEY = 'ricardoai.totp.history.v1'
export const HISTORY_LIMIT = 50
const MAX_SERIALIZED_LENGTH = 256_000
const MAX_LABEL_LENGTH = 120
const MAX_TEXT_LENGTH = 512

function storageOrDefault(storage) {
  if (storage !== undefined && storage !== null) return storage
  try {
    const value = globalThis.localStorage
    if (!value) throw new Error('unavailable')
    return value
  } catch {
    throw new Error('浏览器本机存储不可用，请检查隐私设置后重试。')
  }
}

function storageFailure(action) {
  return new Error(`本机历史${action}失败，请检查浏览器存储权限。`)
}

function text(value, max = MAX_TEXT_LENGTH) {
  return typeof value === 'string' ? value.trim().slice(0, max) : ''
}

function timestamp(value) {
  const result = value === undefined ? Date.now() : value
  if (!Number.isSafeInteger(result) || result < 0) {
    throw new Error('历史记录时间无效。')
  }
  return result
}

function canonicalConfig(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error('2FA 配置无效，无法保存历史。')
  }
  // Re-parse the secret so callers cannot persist an otpauth URL or an
  // unnormalised value. parseTotpInput also enforces the Base32 size/charset.
  let parsed
  try {
    parsed = parseTotpInput(config.secret)
  } catch {
    throw new Error('2FA 密钥无效，无法保存历史。')
  }
  const algorithm = typeof config.algorithm === 'string' ? config.algorithm : parsed.algorithm
  const digits = config.digits === undefined ? parsed.digits : config.digits
  const period = config.period === undefined ? parsed.period : config.period
  if (!['SHA-1', 'SHA-256', 'SHA-512'].includes(algorithm)) {
    throw new Error('2FA 算法无效，无法保存历史。')
  }
  if (digits !== 6 && digits !== 8) throw new Error('2FA 位数无效，无法保存历史。')
  if (!Number.isInteger(period) || period < 1 || period > 300) {
    throw new Error('2FA 刷新周期无效，无法保存历史。')
  }
  return {
    secret: parsed.secret,
    algorithm,
    digits,
    period,
    issuer: text(config.issuer ?? parsed.issuer),
    account: text(config.account ?? parsed.account),
  }
}

function validEntry(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  if (typeof value.id !== 'string' || !value.id || value.id.length > 160) return false
  try {
    const config = canonicalConfig(value)
    if (!Number.isSafeInteger(value.lastUsedAt) || value.lastUsedAt < 0) return false
    return {
      id: value.id,
      ...config,
      label: text(value.label, MAX_LABEL_LENGTH),
      lastUsedAt: value.lastUsedAt,
    }
  } catch {
    return false
  }
}

function readRaw(storage) {
  let raw
  try {
    raw = storage.getItem(HISTORY_KEY)
  } catch {
    throw storageFailure('读取')
  }
  if (raw === null || raw === '') return []
  if (typeof raw !== 'string' || raw.length > MAX_SERIALIZED_LENGTH) {
    throw new Error('本机历史数据过大或已损坏，可清空后重新保存。')
  }
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw new Error('本机历史数据无法读取，可清空后重新保存。')
  }
  if (!Array.isArray(parsed)) throw new Error('本机历史数据无法读取，可清空后重新保存。')
  const entries = parsed.map(validEntry)
  if (entries.some((entry) => !entry)) {
    throw new Error('本机历史数据无法读取，可清空后重新保存。')
  }
  return entries.slice(0, HISTORY_LIMIT)
}

function writeRaw(storage, entries) {
  const value = JSON.stringify(entries.slice(0, HISTORY_LIMIT))
  if (value.length > MAX_SERIALIZED_LENGTH) throw new Error('历史记录内容过大，无法保存。')
  try {
    storage.setItem(HISTORY_KEY, value)
  } catch {
    throw storageFailure('保存')
  }
  return entries.slice(0, HISTORY_LIMIT)
}

function createId() {
  try {
    if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  } catch {
    // Fall through to a non-secret local identifier.
  }
  return `totp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

/** Read opt-in local 2FA history. Dynamic codes are never part of the data. */
export function readTotpHistory(storage) {
  return readRaw(storageOrDefault(storage))
}

/** Insert/update one history item after the user explicitly opts in. */
export function saveTotpHistoryEntry(config, label = '', storage, now) {
  const target = storageOrDefault(storage)
  const canonical = canonicalConfig(config)
  const at = timestamp(now)
  const entries = readRaw(target)
  const same = (entry) => entry.secret === canonical.secret
    && entry.algorithm === canonical.algorithm
    && entry.digits === canonical.digits
    && entry.period === canonical.period
  const existing = entries.find(same)
  const next = {
    id: existing?.id || createId(),
    ...canonical,
    label: text(label, MAX_LABEL_LENGTH),
    lastUsedAt: at,
  }
  return writeRaw(target, [next, ...entries.filter((entry) => !same(entry))])
}

export function deleteTotpHistoryEntry(id, storage) {
  const target = storageOrDefault(storage)
  if (typeof id !== 'string' || !id || id.length > 160) return readRaw(target)
  const entries = readRaw(target)
  return writeRaw(target, entries.filter((entry) => entry.id !== id))
}

export function clearTotpHistory(storage) {
  const target = storageOrDefault(storage)
  try {
    target.removeItem(HISTORY_KEY)
  } catch {
    throw storageFailure('清空')
  }
  return []
}

/** Safe display form: a short secret is never revealed in full. */
export function maskTotpSecret(secret) {
  const value = text(secret, 1024)
  if (value.length <= 4) return '••••'
  return `••••••${value.slice(-4)}`
}
