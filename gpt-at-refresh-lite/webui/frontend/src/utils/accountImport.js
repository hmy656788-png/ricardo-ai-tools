const FORMAT_ERROR = '无法识别格式，请使用 邮箱|密码|2FA 或 邮箱----密码----2FA（支持全角｜及混用 --、---、----）'
const AMBIGUOUS_ERROR = '分隔符有歧义；若密码含连续或边界减号，请使用四减号分隔，并在分隔符两侧加空格'
const PIPE_AMBIGUOUS_ERROR = '竖线格式最多包含邮箱、密码、2FA 三段；若密码含 | 或 ｜，请使用四减号分隔字段'
const LOSSLESS_ERROR = '凭证内容无法无损转换为四减号格式，请检查是否含连续四个减号或边界字符；请勿删除或替换密码字符'

function isEmail(value) {
  return /^[^\s@|｜]+@[^\s@|｜]+$/.test(value) && !value.includes('----')
}

function result(parts, normalized) {
  const [email, password = '', secret = ''] = parts.map((part) => part.trim())
  if (!isEmail(email)) return { ok: false, error: FORMAT_ERROR }
  // Keep a trailing password hyphen from merging with the following delimiter.
  // The existing server trims each field after splitting, so padding is lossless.
  const safePassword = password.endsWith('-') ? `${password} ` : password
  const text = [email, safePassword, secret].join('----')
  const serverFields = text.split('----').map((part) => part.trim())
  if (serverFields.length !== 3 || [email, password, secret].some((field, index) => field !== serverFields[index])) {
    return { ok: false, error: LOSSLESS_ERROR }
  }
  return { ok: true, text, normalized }
}

function normalizeHyphenLine(line) {
  // Preserve the existing four-hyphen format, including empty fields, extra
  // export columns, and passwords containing two or three consecutive hyphens.
  const standard = line.split('----')
  const standardEmail = standard[0].trim()
  const domain = standardEmail.slice(standardEmail.indexOf('@') + 1)
  if (standard.length >= 3 && isEmail(standardEmail) && !/-{2,}/.test(domain)) {
    if (standard[2].trim().startsWith('-')) return { ok: false, error: AMBIGUOUS_ERROR }
    return result(standard.slice(0, 3), false)
  }

  // Runs in the mailbox name (before @) are not field boundaries.
  const at = line.indexOf('@')
  const separators = [...line.matchAll(/-{2,}/g)].filter((match) => match.index > at)
  if (at < 1 || !separators.length) return { ok: false, error: FORMAT_ERROR }
  if (separators.some((match) => match[0].length > 4)) {
    return { ok: false, error: AMBIGUOUS_ERROR }
  }

  const first = separators[0]
  const email = line.slice(0, first.index).trim()
  // A domain containing -- could itself look like a delimiter. Do not import
  // a truncated domain or guess which part of an ambiguous line is a password.
  if (!isEmail(email) || !email.slice(email.indexOf('@') + 1).includes('.')) {
    // Keep the legacy two-field form (email----password, no 2FA).
    if (standard.length === 2 && isEmail(standardEmail) && separators.length === 1) {
      return result(standard, false)
    }
    return { ok: false, error: FORMAT_ERROR }
  }
  if (separators.length > 2) return { ok: false, error: AMBIGUOUS_ERROR }

  const passwordStart = first.index + first[0].length
  const second = separators[1]
  if (!second) return result([email, line.slice(passwordStart), ''], first[0] !== '----')
  return result([
    email,
    line.slice(passwordStart, second.index),
    line.slice(second.index + second[0].length),
  ], first[0] !== '----' || second[0] !== '----')
}

/** Normalize field boundaries only. Never globally replace credential characters. */
export function normalizeAccountLine(raw) {
  const line = String(raw).trim()
  if (!line) return { ok: true, empty: true, text: '', normalized: false }

  const parts = line.split(/[|｜]/)
  const email = parts[0].trim()
  const hasPipeEmail = parts.length > 1 && isEmail(email)
  // Do not reinterpret a failed short-hyphen row's "email--password" as its
  // mailbox. Punycode labels have an unambiguous, explicitly supported prefix.
  const domainLabels = email.slice(email.indexOf('@') + 1).split('.')
  const ambiguousDomain = domainLabels.some((label) => /-{2,}/.test(label) && !/^xn--[a-z0-9]+(?:-[a-z0-9]+)*$/i.test(label))
  // An explicit pipe after a complete mailbox wins over hyphens inside that
  // mailbox (e.g. an xn-- domain label) or its password.
  if (hasPipeEmail && !ambiguousDomain) {
    if (parts.length > 3) return { ok: false, error: PIPE_AMBIGUOUS_ERROR }
    return result(parts, true)
  }

  // Otherwise preserve the existing hyphen format, including literal pipes
  // inside a password. Never use an ambiguous mailbox as a pipe fallback.
  const hyphenResult = normalizeHyphenLine(line)
  if (!hyphenResult.ok && hasPipeEmail && ambiguousDomain) return { ok: false, error: AMBIGUOUS_ERROR }
  return hyphenResult
}

/** Shared by the preview and submission; errors contain line numbers, not secrets. */
export function parseAccountImport(raw) {
  let total = 0
  let recognized = 0
  let normalized = 0
  const errors = []
  const lines = String(raw).split(/\r\n|\r|\n/).map((line, index) => {
    const parsed = normalizeAccountLine(line)
    if (parsed.empty) return ''
    total += 1
    if (!parsed.ok) {
      errors.push({ line: index + 1, message: parsed.error })
      return ''
    }
    recognized += 1
    if (parsed.normalized) normalized += 1
    return parsed.text
  })
  return { total, recognized, normalized, errors, text: lines.join('\n') }
}
