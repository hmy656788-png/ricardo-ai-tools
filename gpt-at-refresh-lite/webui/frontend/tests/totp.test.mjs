import assert from 'node:assert/strict'
import test from 'node:test'
import { parseTotpInput, generateTotp } from '../src/utils/totp.js'

// Public RFC 6238 test material, never real account credentials.
const SECRET_SHA1 = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
function encodeBase32(text) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  const bits = [...new TextEncoder().encode(text)].map((byte) => byte.toString(2).padStart(8, '0')).join('')
  return bits.match(/.{1,5}/g).map((chunk) => alphabet[parseInt(chunk.padEnd(5, '0'), 2)]).join('')
}
const RFC_SECRETS = {
  'SHA-1': SECRET_SHA1,
  'SHA-256': encodeBase32('12345678901234567890123456789012'),
  'SHA-512': encodeBase32('1234567890123456789012345678901234567890123456789012345678901234'),
}
const VECTORS = [
  [59, '94287082', '46119246', '90693936'],
  [1111111109, '07081804', '68084774', '25091201'],
  [1111111111, '14050471', '67062674', '99943326'],
  [1234567890, '89005924', '91819424', '93441116'],
  [2000000000, '69279037', '90698825', '38618901'],
  [20000000000, '65353130', '77737706', '47863826'],
]

for (const [seconds, ...expected] of VECTORS) {
  for (const [index, algorithm] of Object.keys(RFC_SECRETS).entries()) {
    test(`RFC 6238 ${algorithm} at ${seconds}`, async () => {
      const result = await generateTotp({ secret: RFC_SECRETS[algorithm], algorithm, digits: 8, period: 30 }, seconds * 1000)
      assert.equal(result.code, expected[index])
      assert.equal(result.counter, Math.floor(seconds / 30))
      assert.equal(result.validUntil, (Math.floor(seconds / 30) + 1) * 30_000)
    })
  }
}

test('bare secrets use the expected defaults and normalize whitespace/case', () => {
  assert.deepEqual(parseTotpInput(' gezdgnbv gy3tqojq\nGEZDGNBVGY3TQOJQ\t'), {
    secret: SECRET_SHA1, algorithm: 'SHA-1', digits: 6, period: 30, issuer: '', account: '',
  })
})

test('canonical padded and unpadded Base32 forms normalize identically', () => {
  for (const [padded, plain] of [['MY======', 'MY'], ['MZXQ====', 'MZXQ'], ['MZXW6===', 'MZXW6'], ['MZXW6YQ=', 'MZXW6YQ'], ['MZXW6YTB', 'MZXW6YTB']]) {
    assert.equal(parseTotpInput(padded).secret, plain)
    assert.equal(parseTotpInput(plain).secret, plain)
  }
})

test('otpauth URI parses label and supported custom parameters', () => {
  assert.deepEqual(parseTotpInput(`otpauth://totp/Example%3Aalice%40example.com?secret=${SECRET_SHA1}&issuer=Example&algorithm=SHA256&digits=8&period=60`), {
    secret: SECRET_SHA1, algorithm: 'SHA-256', digits: 8, period: 60, issuer: 'Example', account: 'alice@example.com',
  })
  assert.equal(parseTotpInput(`otpauth://totp/Example:alice?secret=${SECRET_SHA1}`).issuer, 'Example')
  assert.equal(parseTotpInput(`otpauth://totp/alice?secret=${SECRET_SHA1}`).account, 'alice')
})

test('leading zeros and six-digit output are preserved', async () => {
  assert.equal((await generateTotp(parseTotpInput(SECRET_SHA1), 1111111109_000)).code, '081804')
  assert.equal((await generateTotp(parseTotpInput(SECRET_SHA1), 59_000)).code, '287082')
})

test('code changes exactly at the period boundary', async () => {
  const config = parseTotpInput(SECRET_SHA1)
  const before = await generateTotp(config, 29_999)
  const after = await generateTotp(config, 30_000)
  assert.equal(before.code, '755224')
  assert.equal(after.code, '287082')
  assert.equal(before.counter, 0)
  assert.equal(after.counter, 1)
  assert.equal(before.validUntil, 30_000)
  assert.equal(after.validUntil, 60_000)
})

test('custom periods control the counter and expiration', async () => {
  const result = await generateTotp({ ...parseTotpInput(SECRET_SHA1), period: 60 }, 59_999)
  assert.equal(result.code, '755224')
  assert.equal(result.validUntil, 60_000)
})

test('invalid Base32 characters, length, padding, trailing bits, and oversized input are rejected', () => {
  for (const value of ['', ' \n', '0O1I', 'ABC!', 'A', 'AAA', 'AAAAAA', 'MZ', 'MZXR', 'MZXW7', 'MZXW6YR', 'MY=', 'MY=======', 'M=Y', 'MZXW6YTB=', 'A'.repeat(1025), ' '.repeat(4097), null, 123]) {
    assert.throws(() => parseTotpInput(value), Error)
  }
})

test('invalid or ambiguous URI parameters are rejected without echoing input', () => {
  const values = [
    'otpauth://hotp/alice?secret=MY&counter=1',
    'otpauth://totp/alice?secret=MY&counter=1',
    'otpauth://totp/alice',
    'otpauth://totp/alice?secret=MY&secret=MY',
    'otpauth://totp/alice?secret=MY&Secret=MY',
    'otpauth://totp/alice?secret=MY&algorithm=MD5',
    'otpauth://totp/alice?secret=MY&algorithm=',
    'otpauth://totp/alice?secret=MY&algorithm=SHA1&algorithm=SHA256',
    'otpauth://totp/alice?secret=MY&digits=7',
    'otpauth://totp/alice?secret=MY&digits=6&digits=8',
    'otpauth://totp/alice?secret=MY&period=0',
    'otpauth://totp/alice?secret=MY&period=301',
    'otpauth://totp/alice?secret=MY&period=30.5',
    'otpauth://totp/alice?secret=MY&period=3e1',
    'otpauth://totp/alice?secret=MY&period=',
    'otpauth://totp/alice?secret=MY&period=30&period=60',
    'otpauth://totp/alice?secret=MY&issuer=A&issuer=B',
    'otpauth://user:pass@totp/alice?secret=MY',
    'otpauth://totp:80/alice?secret=MY',
    'otpauth://totp/alice?secret=MY#fragment',
    'otpauth://totp/%E0%A4?secret=MY',
    'otpauth://[',
  ]
  for (const value of values) {
    assert.throws(() => parseTotpInput(value), (error) => {
      assert.ok(error instanceof Error)
      assert.ok(!error.message.includes(value))
      assert.ok(!error.message.includes('secret='))
      return true
    })
  }
})

test('invalid generation settings and times fail before cryptography', async () => {
  const config = parseTotpInput(SECRET_SHA1)
  for (const invalid of [null, [], {}, { ...config, digits: '6' }, { ...config, period: 0 }, { ...config, period: 30.5 }, { ...config, algorithm: 'MD5' }]) {
    await assert.rejects(generateTotp(invalid, 0), Error)
  }
  for (const now of [-1, NaN, Infinity, 1.5, '1000', Number.MAX_SAFE_INTEGER]) {
    await assert.rejects(generateTotp(config, now), /系统时间无效/)
  }
})

test('missing Web Crypto gives actionable guidance without leaking input', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  try {
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: undefined })
    await assert.rejects(generateTotp(parseTotpInput(SECRET_SHA1), 0), /localhost、127\.0\.0\.1 或 HTTPS/)
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor)
    else delete globalThis.crypto
  }
})
