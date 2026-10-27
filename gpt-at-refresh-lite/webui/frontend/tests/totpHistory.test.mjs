import assert from 'node:assert/strict'
import test from 'node:test'
import {
  HISTORY_KEY, HISTORY_LIMIT, clearTotpHistory, deleteTotpHistoryEntry,
  maskTotpSecret, readTotpHistory, saveTotpHistoryEntry,
} from '../src/utils/totpHistory.js'

const SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
const config = { secret: SECRET, algorithm: 'SHA-1', digits: 6, period: 30, issuer: 'Example', account: 'alice@example.com' }

function store(initial = null) {
  const data = new Map(initial === null ? [] : [[HISTORY_KEY, initial]])
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
    raw: () => data.get(HISTORY_KEY),
  }
}

test('history is opt-in and saves only canonical secret/config fields', () => {
  const storage = store()
  const [entry] = saveTotpHistoryEntry(config, '工作账号', storage, 1000)
  assert.equal(entry.secret, SECRET)
  assert.equal(entry.label, '工作账号')
  assert.equal(entry.lastUsedAt, 1000)
  assert.equal(entry.code, undefined)
  assert.equal(entry.raw, undefined)
  assert.equal(storage.raw().includes('otpauth'), false)
})

test('same config is updated and moved to front even at the same millisecond', () => {
  const storage = store()
  const first = saveTotpHistoryEntry(config, '旧', storage, 1000)[0]
  const second = saveTotpHistoryEntry({ ...config, issuer: 'New' }, '新', storage, 1000)[0]
  assert.equal(second.id, first.id)
  assert.equal(readTotpHistory(storage).length, 1)
  assert.equal(readTotpHistory(storage)[0].label, '新')
})

test('different algorithm/digits/period values are distinct', () => {
  const storage = store()
  saveTotpHistoryEntry(config, '', storage, 1)
  saveTotpHistoryEntry({ ...config, algorithm: 'SHA-256' }, '', storage, 2)
  saveTotpHistoryEntry({ ...config, digits: 8 }, '', storage, 3)
  saveTotpHistoryEntry({ ...config, period: 60 }, '', storage, 4)
  assert.equal(readTotpHistory(storage).length, 4)
})

test('history is capped, deletable, and clearable', () => {
  const storage = store()
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  for (let index = 0; index < HISTORY_LIMIT + 3; index += 1) {
    const secret = `${alphabet[Math.floor(index / alphabet.length)]}${alphabet[index % alphabet.length]}${'A'.repeat(30)}`
    saveTotpHistoryEntry({ ...config, secret }, String(index), storage, index)
  }
  assert.equal(readTotpHistory(storage).length, HISTORY_LIMIT)
  const first = readTotpHistory(storage)[0]
  assert.equal(deleteTotpHistoryEntry(first.id, storage).length, HISTORY_LIMIT - 1)
  assert.deepEqual(clearTotpHistory(storage), [])
  assert.deepEqual(readTotpHistory(storage), [])
})

test('corrupt storage fails without exposing stored content', () => {
  const secret = 'SECRET_SHOULD_NOT_APPEAR'
  assert.throws(() => readTotpHistory(store('{bad secret}')), (error) => {
    assert.match(error.message, /无法读取/)
    assert.equal(error.message.includes(secret), false)
    return true
  })
})

test('invalid config and storage failures are non-sensitive', () => {
  const storage = store()
  assert.throws(() => saveTotpHistoryEntry({ secret: 'bad-secret' }, '', storage), /密钥无效/)
  const broken = { getItem: () => { throw new Error(SECRET) } }
  assert.throws(() => readTotpHistory(broken), (error) => {
    assert.equal(error.message.includes(SECRET), false)
    return true
  })
})

test('mask never reveals short secrets in full', () => {
  assert.equal(maskTotpSecret('ABC'), '••••')
  assert.equal(maskTotpSecret('ABCDEFGH'), '••••••EFGH')
})
