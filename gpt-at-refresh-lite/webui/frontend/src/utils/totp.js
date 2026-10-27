// RFC 4226 / RFC 6238, using only the browser's built-in cryptography.
// Secrets stay in memory: no requests, persistence, analytics, or logging.
const BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
const MAX_INPUT_LENGTH = 4096
const MAX_SECRET_LENGTH = 1024
const ALGORITHMS = new Map([
  ['SHA1', 'SHA-1'], ['SHA-1', 'SHA-1'],
  ['SHA256', 'SHA-256'], ['SHA-256', 'SHA-256'],
  ['SHA512', 'SHA-512'], ['SHA-512', 'SHA-512'],
])
const URI_PARAMETERS = new Set(['secret', 'algorithm', 'digits', 'period', 'issuer', 'counter'])

function normalizeSecret(input) {
  if (typeof input !== 'string' || input.length > MAX_INPUT_LENGTH) {
    throw new Error('密钥格式不正确或内容过长。')
  }
  const compact = input.replace(/\s/g, '').toUpperCase()
  if (!compact) throw new Error('请输入 2FA 密钥或 otpauth 链接。')
  if (!/^[A-Z2-7]+={0,6}$/.test(compact)) {
    throw new Error('密钥必须是 Base32 格式，只能包含字母 A–Z 和数字 2–7。')
  }
  const secret = compact.replace(/=+$/, '')
  if (secret.length > MAX_SECRET_LENGTH) throw new Error('密钥内容过长。')
  const remainder = secret.length % 8
  const paddingByRemainder = { 0: 0, 2: 6, 4: 4, 5: 3, 7: 1 }
  const expectedPadding = paddingByRemainder[remainder]
  const padding = compact.length - secret.length
  if (expectedPadding === undefined || (padding !== 0 && padding !== expectedPadding)) {
    throw new Error('Base32 密钥长度或填充格式不正确。')
  }
  // RFC 4648 requires unused bits in the last character to be zero.
  const unusedBits = (secret.length * 5) % 8
  const lastValue = BASE32_ALPHABET.indexOf(secret[secret.length - 1])
  if (unusedBits && (lastValue & ((1 << unusedBits) - 1)) !== 0) {
    throw new Error('Base32 密钥末尾编码不正确，请检查是否复制完整。')
  }
  return secret
}

function decodeSecret(secret) {
  const bytes = new Uint8Array(Math.floor(secret.length * 5 / 8))
  let accumulator = 0
  let bits = 0
  let index = 0
  for (const character of secret) {
    accumulator = (accumulator << 5) | BASE32_ALPHABET.indexOf(character)
    bits += 5
    if (bits >= 8) {
      bits -= 8
      bytes[index++] = (accumulator >> bits) & 255
      accumulator &= (1 << bits) - 1
    }
  }
  return bytes
}

function normalizeAlgorithm(value) {
  const algorithm = typeof value === 'string' ? ALGORITHMS.get(value.toUpperCase()) : undefined
  if (!algorithm) throw new Error('仅支持 SHA1、SHA256 和 SHA512 算法。')
  return algorithm
}

function validateDigits(value) {
  if (value !== 6 && value !== 8) throw new Error('仅支持 6 位或 8 位验证码。')
  return value
}

function validatePeriod(value) {
  if (!Number.isInteger(value) || value < 1 || value > 300) {
    throw new Error('验证码刷新周期必须是 1–300 秒的整数。')
  }
  return value
}

function integerParameter(parameters, name, defaultValue) {
  const value = parameters.get(name)
  if (value === null) return defaultValue
  if (!/^\d+$/.test(value)) throw new Error('验证码位数或刷新周期格式不正确。')
  return Number(value)
}

/** Parse a Base32 secret or a standard otpauth://totp URI without making requests. */
export function parseTotpInput(input) {
  if (typeof input !== 'string' || input.length > MAX_INPUT_LENGTH) {
    throw new Error('输入内容不正确或过长。')
  }
  const text = input.trim()
  if (!/^otpauth:/i.test(text)) {
    return {
      secret: normalizeSecret(text), algorithm: 'SHA-1', digits: 6, period: 30,
      issuer: '', account: '',
    }
  }

  let uri
  try {
    uri = new URL(text)
  } catch {
    throw new Error('otpauth 链接格式不正确。')
  }
  if (uri.protocol !== 'otpauth:' || uri.hostname.toLowerCase() !== 'totp') {
    throw new Error('仅支持 TOTP 动态验证码，不支持 HOTP 计数器验证码。')
  }
  if (uri.username || uri.password || uri.port || uri.hash) {
    throw new Error('otpauth 链接格式不正确。')
  }
  const parameters = uri.searchParams
  const seen = new Set()
  for (const [name] of parameters) {
    const normalized = name.toLowerCase()
    if (!URI_PARAMETERS.has(normalized)) continue
    if (name !== normalized || seen.has(normalized)) {
      throw new Error('otpauth 链接包含重复或格式不正确的参数。')
    }
    seen.add(normalized)
  }
  if (parameters.has('counter')) {
    throw new Error('仅支持 TOTP 动态验证码，不支持 HOTP 计数器验证码。')
  }

  let label
  try {
    label = decodeURIComponent(uri.pathname.replace(/^\//, '')).trim()
  } catch {
    throw new Error('otpauth 链接中的账户名称编码不正确。')
  }
  const separator = label.indexOf(':')
  const labelIssuer = separator < 0 ? '' : label.slice(0, separator).trim()
  return {
    secret: normalizeSecret(parameters.get('secret') ?? ''),
    algorithm: normalizeAlgorithm(parameters.get('algorithm') ?? 'SHA1'),
    digits: validateDigits(integerParameter(parameters, 'digits', 6)),
    period: validatePeriod(integerParameter(parameters, 'period', 30)),
    issuer: (parameters.get('issuer') ?? labelIssuer).trim(),
    account: (separator < 0 ? label : label.slice(separator + 1)).trim(),
  }
}

/** Generate a code for an explicit Unix time in milliseconds (or the current time). */
export async function generateTotp(config, now = Date.now()) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error('验证码配置不正确。')
  }
  const secret = normalizeSecret(config.secret)
  const algorithm = normalizeAlgorithm(config.algorithm ?? 'SHA-1')
  const digits = validateDigits(config.digits ?? 6)
  const period = validatePeriod(config.period ?? 30)
  if (!Number.isSafeInteger(now) || now < 0) throw new Error('系统时间无效，请检查电脑时钟。')
  const counter = Math.floor(now / (period * 1000))
  const validUntil = (counter + 1) * period * 1000
  if (!Number.isSafeInteger(validUntil)) throw new Error('系统时间无效，请检查电脑时钟。')
  const subtle = globalThis.crypto?.subtle
  if (!subtle) {
    throw new Error('当前环境不支持安全加密，请使用 localhost、127.0.0.1 或 HTTPS 打开页面。')
  }

  const keyBytes = decodeSecret(secret)
  try {
    const key = await subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: algorithm }, false, ['sign'])
    const message = new Uint8Array(8)
    new DataView(message.buffer).setBigUint64(0, BigInt(counter), false)
    const digest = new Uint8Array(await subtle.sign('HMAC', key, message))
    const offset = digest[digest.length - 1] & 15
    const binary = new DataView(digest.buffer).getUint32(offset, false) & 0x7fffffff
    return { code: String(binary % (10 ** digits)).padStart(digits, '0'), counter, validUntil }
  } catch {
    throw new Error('无法生成验证码，请检查浏览器是否支持安全加密。')
  } finally {
    keyBytes.fill(0)
  }
}
