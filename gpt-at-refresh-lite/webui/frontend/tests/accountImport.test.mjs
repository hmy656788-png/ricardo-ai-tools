import test from 'node:test'
import assert from 'node:assert/strict'
import { normalizeAccountLine, parseAccountImport } from '../src/utils/accountImport.js'

const EMAIL = 'demo@example.com'
const PASSWORD = 'Demo-pass.42!'
const SECRET = 'JBSWY3DPEHPK3PXP'
const expected = [EMAIL, PASSWORD, SECRET].join('----')

for (const first of ['--', '---', '----']) {
  for (const second of ['--', '---', '----']) {
    test(`recognizes mixed delimiters of lengths ${first.length} and ${second.length}`, () => {
      const parsed = normalizeAccountLine(`${EMAIL}${first}${PASSWORD}${second}${SECRET}`)
      assert.equal(parsed.ok, true)
      assert.equal(parsed.text, expected)
      assert.equal(parsed.normalized, first !== '----' || second !== '----')
      // Contract with the unchanged API: it splits on exactly four hyphens.
      assert.deepEqual(parsed.text.split('----').slice(0, 3), [EMAIL, PASSWORD, SECRET])
    })
  }
}

test('standard four-hyphen input preserves shorter runs inside passwords', () => {
  for (const password of ['a-b', 'a--b', 'a---b', 'a--b---c', '-leading']) {
    const parsed = normalizeAccountLine(`${EMAIL}----${password}----${SECRET}`)
    assert.equal(parsed.ok, true)
    assert.equal(parsed.text.split('----')[1], password)
  }
})

test('boundary hyphens with explicit padding survive the server split and trim', () => {
  const parsed = normalizeAccountLine(`${EMAIL} ---- trailing- ---- ${SECRET}`)
  assert.equal(parsed.ok, true)
  assert.deepEqual(parsed.text.split('----').map((field) => field.trim()), [EMAIL, 'trailing-', SECRET])
  assert.equal(normalizeAccountLine(`${EMAIL}----trailing-----${SECRET}`).ok, false)
})

test('standard input retains empty fields and ignores extra export columns', () => {
  for (const [password, secret] of [['', SECRET], [PASSWORD, ''], ['', '']]) {
    const parsed = normalizeAccountLine(`${EMAIL}----${password}----${secret}----ignored-token----ignored-time`)
    assert.equal(parsed.ok, true)
    assert.deepEqual(parsed.text.split('----'), [EMAIL, password, secret])
  }
})

test('two-field imports remain supported with optional 2FA', () => {
  for (const delimiter of ['--', '---', '----']) {
    const parsed = normalizeAccountLine(`${EMAIL}${delimiter}${PASSWORD}`)
    assert.deepEqual(parsed.text.split('----'), [EMAIL, PASSWORD, ''])
  }
  assert.equal(normalizeAccountLine('demo@localhost----demo-pass').ok, true)
})

test('whitespace around fields and different line endings are normalized', () => {
  const input = `  ${EMAIL} -- ${PASSWORD} --- ${SECRET}  \r\n\r\n${EMAIL}---${PASSWORD}--${SECRET}\r`
  const parsed = parseAccountImport(input)
  assert.equal(parsed.total, 2)
  assert.equal(parsed.recognized, 2)
  assert.equal(parsed.normalized, 2)
  assert.deepEqual(parsed.errors, [])
  assert.deepEqual(parsed.text.split('\n'), [expected, '', expected, ''])
})

test('short hyphen runs inside the mailbox name are preserved', () => {
  for (const email of ['demo--name@example.com', 'demo---name@example.com']) {
    const parsed = normalizeAccountLine(`${email}--${PASSWORD}---${SECRET}`)
    assert.equal(parsed.ok, true)
    assert.equal(parsed.text.split('----')[0], email)
  }
})

test('ambiguous short delimiters do not silently truncate a password', () => {
  const parsed = normalizeAccountLine(`${EMAIL}--private--password---${SECRET}`)
  assert.equal(parsed.ok, false)
  assert.match(parsed.error, /歧义/)
  assert.equal(parsed.text, undefined)
})

test('invalid or unrepresentable input is rejected without echoing credentials', () => {
  for (const input of [
    'not-an-email--private-password---PRIVATESECRET',
    `${EMAIL}-private-password-${SECRET}`,
    `${EMAIL}--private-password-----${SECRET}`,
    `demo----name@example.com--private-password---${SECRET}`,
    'demo@xn--domain.example--private-password---PRIVATESECRET',
  ]) {
    const parsed = normalizeAccountLine(input)
    assert.equal(parsed.ok, false)
    assert.doesNotMatch(parsed.error, /private-password|PRIVATESECRET|demo@/)
  }
})

test('preview reports original line numbers and never includes invalid raw input', () => {
  const input = `\n${expected}\n\ninvalid-private-input\n${EMAIL}--p---${SECRET}`
  const parsed = parseAccountImport(input)
  assert.equal(parsed.total, 3)
  assert.equal(parsed.recognized, 2)
  assert.equal(parsed.normalized, 1)
  assert.deepEqual(parsed.errors.map((error) => error.line), [4])
  assert.doesNotMatch(JSON.stringify(parsed), /invalid-private-input/)
})

test('empty input is not counted as an import row', () => {
  const parsed = parseAccountImport(' \r\n\t\n')
  assert.equal(parsed.total, 0)
  assert.equal(parsed.recognized, 0)
  assert.deepEqual(parsed.errors, [])
})

const PIPE_EMAIL = 'pipe-demo@example.test'
const PIPE_PASSWORD = 'Example-pass.42!'
const PIPE_SECRET = 'JBSWY3DPEHPK3PXP'

function assertServerFields(parsed, fields) {
  assert.equal(parsed.ok, true)
  // Emulate the unchanged API, including trimming but without dropping columns.
  assert.deepEqual(parsed.text.split('----').map((field) => field.trim()), fields)
}

for (const first of ['|', '｜']) {
  for (const second of ['|', '｜']) {
    test(`recognizes pipe delimiters ${JSON.stringify(first)} and ${JSON.stringify(second)}`, () => {
      const parsed = normalizeAccountLine(`${PIPE_EMAIL}${first}${PIPE_PASSWORD}${second}${PIPE_SECRET}`)
      assertServerFields(parsed, [PIPE_EMAIL, PIPE_PASSWORD, PIPE_SECRET])
      assert.equal(parsed.normalized, true)
    })
  }
}

test('pipe formats retain optional 2FA and empty fields', () => {
  for (const delimiter of ['|', '｜']) {
    for (const [password, secret] of [[PIPE_PASSWORD, PIPE_SECRET], ['', PIPE_SECRET], [PIPE_PASSWORD, ''], ['', '']]) {
      assertServerFields(normalizeAccountLine(`${PIPE_EMAIL}${delimiter}${password}${delimiter}${secret}`), [PIPE_EMAIL, password, secret])
    }
    assertServerFields(normalizeAccountLine(`${PIPE_EMAIL}${delimiter}${PIPE_PASSWORD}`), [PIPE_EMAIL, PIPE_PASSWORD, ''])
    assertServerFields(normalizeAccountLine(`${PIPE_EMAIL}${delimiter}`), [PIPE_EMAIL, '', ''])
  }
})

test('pipe-delimited passwords retain short hyphen runs and boundary hyphens', () => {
  for (const delimiter of ['|', '｜']) {
    for (const password of ['a--b', 'a---b', 'a--b---c', '-leading', 'trailing-', '--both---', '---']) {
      const parsed = normalizeAccountLine(`${PIPE_EMAIL}${delimiter}${password}${delimiter}${PIPE_SECRET}`)
      assertServerFields(parsed, [PIPE_EMAIL, password, PIPE_SECRET])
    }
  }
})

test('hyphen-delimited credentials keep literal halfwidth and fullwidth pipes', () => {
  for (const password of ['left|right', 'left｜right', '|both｜', 'left|middle｜right']) {
    for (const first of ['--', '---', '----']) {
      for (const second of ['--', '---', '----']) {
        assertServerFields(normalizeAccountLine(`${PIPE_EMAIL}${first}${password}${second}${PIPE_SECRET}`), [PIPE_EMAIL, password, PIPE_SECRET])
      }
    }
  }
  assertServerFields(normalizeAccountLine(`${PIPE_EMAIL} ---- |trailing- ---- ${PIPE_SECRET}`), [PIPE_EMAIL, '|trailing-', PIPE_SECRET])
})

test('pipe whitespace and Unicode password characters survive the server contract', () => {
  const password = '演示Pass_｜not-a-boundary'
  // A literal fullwidth pipe in a password requires the explicit hyphen format.
  assertServerFields(normalizeAccountLine(` ${PIPE_EMAIL} ---- ${password} ---- ${PIPE_SECRET} `), [PIPE_EMAIL, password, PIPE_SECRET])
  assertServerFields(normalizeAccountLine(` ${PIPE_EMAIL} ｜ 示例Pass_42! | ${PIPE_SECRET} `), [PIPE_EMAIL, '示例Pass_42!', PIPE_SECRET])
})

test('extra pipe fields and literal password pipes are rejected instead of guessed', () => {
  for (const input of [
    `${PIPE_EMAIL}|private|password|${PIPE_SECRET}`,
    `${PIPE_EMAIL}｜private｜password｜${PIPE_SECRET}`,
    `${PIPE_EMAIL}|private｜password|${PIPE_SECRET}`,
    `${PIPE_EMAIL}|${PIPE_PASSWORD}|${PIPE_SECRET}|private-extra-column`,
    `${PIPE_EMAIL}|||`,
  ]) {
    const parsed = normalizeAccountLine(input)
    assert.equal(parsed.ok, false)
    assert.equal(parsed.text, undefined)
    assert.doesNotMatch(JSON.stringify(parsed), /private|pipe-demo@|JBSWY3DPEHPK3PXP/)
  }
})

test('pipe credentials containing four consecutive hyphens cannot silently change fields', () => {
  for (const delimiter of ['|', '｜']) {
    for (const [password, secret] of [
      ['private----password', PIPE_SECRET],
      ['----leading', PIPE_SECRET],
      ['trailing----', PIPE_SECRET],
      [PIPE_PASSWORD, 'PRIVATE----SECRET'],
    ]) {
      const parsed = normalizeAccountLine(`${PIPE_EMAIL}${delimiter}${password}${delimiter}${secret}`)
      assert.equal(parsed.ok, false)
      assert.equal(parsed.text, undefined)
      assert.doesNotMatch(JSON.stringify(parsed), /private|PRIVATE|pipe-demo@|Example-pass/)
    }
  }
})

test('mailbox boundary hyphens cannot be swallowed when normalized for the API', () => {
  for (const delimiter of ['|', '｜']) {
    const parsed = normalizeAccountLine(`demo@example.test-${delimiter}${PIPE_PASSWORD}${delimiter}${PIPE_SECRET}`)
    assert.equal(parsed.ok, false)
    assert.equal(parsed.text, undefined)
  }
})

test('pipe formats support mailbox hyphens and unambiguous punycode domains', () => {
  for (const email of ['demo--name@example.test', 'demo---name@example.test', 'demo@xn--bcher-kva.test', 'demo@books.xn--p1ai.example.test', 'demo@books.xn--bcher-kva.example.test']) {
    for (const delimiter of ['|', '｜']) {
      assertServerFields(normalizeAccountLine(`${email}${delimiter}${PIPE_PASSWORD}${delimiter}${PIPE_SECRET}`), [email, PIPE_PASSWORD, PIPE_SECRET])
    }
  }
})

test('failed hyphen rows cannot be reinterpreted as a larger mailbox through pipe fallback', () => {
  for (const input of [
    `${PIPE_EMAIL}--private--password|suffix---${PIPE_SECRET}`,
    `${PIPE_EMAIL}-----private|${PIPE_SECRET}`,
    `${PIPE_EMAIL}--private|password-----${PIPE_SECRET}`,
    `demo@example.xn--domain--pass|piece---private--${PIPE_SECRET}`,
  ]) {
    const parsed = normalizeAccountLine(input)
    assert.equal(parsed.ok, false)
    assert.equal(parsed.text, undefined)
    assert.doesNotMatch(JSON.stringify(parsed), /private|pipe-demo@|piece|JBSWY3DPEHPK3PXP/)
  }
})

test('mixed row formats normalize CRLF and preserve error line numbers without leaking invalid input', () => {
  const good = [PIPE_EMAIL, PIPE_PASSWORD, PIPE_SECRET].join('----')
  const input = [
    '',
    ` ${PIPE_EMAIL} | ${PIPE_PASSWORD} ｜ ${PIPE_SECRET} `,
    '',
    `${PIPE_EMAIL}--${PIPE_PASSWORD}---${PIPE_SECRET}`,
    'invalid@example.test|private-a|private-b|PRIVATESECRET',
    good,
    '',
  ].join('\r\n')
  const parsed = parseAccountImport(input)
  assert.equal(parsed.total, 4)
  assert.equal(parsed.recognized, 3)
  assert.equal(parsed.normalized, 2)
  assert.deepEqual(parsed.errors.map((error) => error.line), [5])
  assert.deepEqual(parsed.text.split('\n'), ['', good, '', good, '', good, ''])
  assert.doesNotMatch(JSON.stringify(parsed), /invalid@example|private-a|private-b|PRIVATESECRET/)
})

test('invalid pipe input reports only generic errors, not supplied credentials', () => {
  for (const input of [
    'not-an-email|private-password|PRIVATESECRET',
    '|private-password|PRIVATESECRET',
    'name@with space.test｜private-password｜PRIVATESECRET',
    'name@domain@other.test|private-password|PRIVATESECRET',
    'private|name@example.test----private-password----PRIVATESECRET',
  ]) {
    const parsed = normalizeAccountLine(input)
    assert.equal(parsed.ok, false)
    assert.equal(parsed.text, undefined)
    assert.doesNotMatch(JSON.stringify(parsed), /private-password|PRIVATESECRET|name@/)
  }
})
