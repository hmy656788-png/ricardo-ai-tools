import assert from 'node:assert/strict'
import test from 'node:test'
import { errorGuidance, normalizeCategory } from '../src/utils/errorGuidance.js'

test('keeps server categories and gives actionable advice', () => {
  assert.equal(normalizeCategory('network', 'anything'), 'network')
  assert.equal(errorGuidance('banned').label, '已封号')
  assert.match(errorGuidance('network').advice, /代理/)
})

test('infers useful categories when older records have no category', () => {
  assert.equal(normalizeCategory('', 'HTTP 429 too many requests'), 'rate_limit')
  assert.equal(normalizeCategory('', '验证码未完成'), 'verification')
  assert.equal(normalizeCategory('', 'connection timeout'), 'network')
  assert.equal(normalizeCategory('', 'unknown local failure'), 'unknown')
})

test('does not echo supplied error text in generic guidance', () => {
  const result = errorGuidance('', 'private-password=ExamplePass42')
  assert.equal(result.key, 'unknown')
  assert.doesNotMatch(result.advice, /ExamplePass42/)
})
