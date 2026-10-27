import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../webui/static/embedded.js', import.meta.url), 'utf8')
const ORIGIN = 'http://127.0.0.1:8768'
const query = (origin = ORIGIN, theme = 'light') => `?${new URLSearchParams({ embedded: '1', parentOrigin: origin, theme })}`

function setup({ search = query(), name = '', topLevel = false, height = 800 } = {}) {
  const classes = new Set()
  const sent = []
  const frames = []
  const observed = []
  const windowListeners = new Map()
  const documentListeners = new Map()
  let currentHeight = height
  let forbiddenAccess = 0
  const forbidden = () => { forbiddenAccess += 1; throw new Error('Forbidden network, persistence, or reload') }
  const listen = (map) => (type, callback) => {
    if (!map.has(type)) map.set(type, [])
    map.get(type).push(callback)
  }
  const parent = { postMessage: (message, origin) => sent.push({ message: JSON.parse(JSON.stringify(message)), origin }) }
  const body = { getBoundingClientRect: () => ({ height: currentHeight }) }
  const document = {
    title: 'Standalone title',
    documentElement: { classList: { add: (value) => classes.add(value) }, dataset: {} },
    body,
    addEventListener: listen(documentListeners),
  }
  // A viewport-based measurement would conceal shrinking content and create a feedback loop.
  Object.defineProperty(document.documentElement, 'scrollHeight', { get: forbidden })
  Object.defineProperty(body, 'scrollHeight', { get: forbidden })
  const window = {
    name,
    location: { search, reload: forbidden },
    parent,
    addEventListener: listen(windowListeners),
    requestAnimationFrame: (callback) => { frames.push(callback) },
    fetch: forbidden,
  }
  if (topLevel) window.parent = window
  for (const target of [window, document]) {
    for (const property of ['localStorage', 'sessionStorage', 'indexedDB', 'cookie']) {
      Object.defineProperty(target, property, { get: forbidden, set: forbidden })
    }
  }
  class ResizeObserver {
    constructor(callback) { this.callback = callback }
    observe(target) { observed.push({ target, callback: this.callback }) }
  }
  const context = vm.createContext({ window, document, URL, URLSearchParams, ResizeObserver, fetch: forbidden })
  for (const property of ['localStorage', 'sessionStorage', 'indexedDB']) {
    Object.defineProperty(context, property, { get: forbidden, set: forbidden })
  }
  vm.runInContext(source, context, { filename: 'embedded.js' })
  const flush = () => {
    let turns = 0
    while (frames.length) {
      assert.ok(++turns < 20, 'rAF must not create a measurement loop')
      frames.shift()()
    }
  }
  const dispatch = (map, type, event) => (map.get(type) || []).forEach((callback) => callback(event))
  return {
    classes, sent, document, window, observed, windowListeners, documentListeners,
    flush,
    ready: () => { dispatch(documentListeners, 'DOMContentLoaded'); flush() },
    resize: (value) => {
      currentHeight = value
      observed.forEach(({ callback }) => callback())
      flush()
    },
    dispatchResize: () => dispatch(windowListeners, 'resize'),
    message: ({ origin = ORIGIN, source: messageSource = parent, data = { type: 'ricardo-ui-theme', theme: 'dark' } } = {}) => {
      dispatch(windowListeners, 'message', { origin, source: messageSource, data })
      flush()
    },
    assertNoForbiddenAccess: () => assert.equal(forbiddenAccess, 0),
  }
}

test('non-embedded standalone pages remain untouched, including a top-level frame-name match', () => {
  for (const options of [{ search: '' }, { search: '?embedded=0' }, { search: '', name: 'ricardo-gpt-register', topLevel: true }]) {
    const env = setup(options)
    assert.equal(env.classes.size, 0)
    assert.deepEqual(env.document.documentElement.dataset, {})
    assert.equal(env.document.title, 'Standalone title')
    assert.equal(env.windowListeners.size, 0)
    assert.equal(env.documentListeners.size, 0)
    assert.deepEqual(env.sent, [])
    env.assertNoForbiddenAccess()
  }
})

test('embedded initialization sets the integration title and only accepts explicit dark', () => {
  for (const theme of ['dark', 'light', 'unexpected']) {
    const env = setup({ search: query(ORIGIN, theme) })
    assert.ok(env.classes.has('embedded'))
    assert.equal(env.document.documentElement.dataset.theme, theme === 'dark' ? 'dark' : 'light')
    assert.equal(env.document.title, 'GPT 注册机 · Ricardo AI 工具')
    env.assertNoForbiddenAccess()
  }
})

test('invalid or non-loopback explicit origins cannot install a communication bridge', () => {
  for (const origin of ['https://example.com', 'http://127.0.0.1.example.com', 'file:///', 'not a URL', 'http://user:pass@localhost:8768', `${ORIGIN}/path`, `${ORIGIN}/?x=1`, `${ORIGIN}/#x`]) {
    const env = setup({ search: query(origin) })
    assert.equal(env.windowListeners.size, 0, origin)
    assert.equal(env.documentListeners.size, 0, origin)
    assert.deepEqual(env.sent, [])
    env.assertNoForbiddenAccess()
  }
})

test('layout messages contain only type and content height and use the exact target origin', () => {
  for (const origin of [ORIGIN, 'http://localhost:9000', 'https://[::1]:8768']) {
    const env = setup({ search: query(origin), height: 800.25 })
    assert.deepEqual(env.sent, [])
    env.ready()
    assert.equal(env.observed[0].target, env.document.body)
    assert.deepEqual(env.sent, [{ message: { type: 'turb-register-layout', height: 801 }, origin }])
    env.assertNoForbiddenAccess()
  }
})

test('named login/logout frame waits for a trusted parent handshake when query parameters are gone', () => {
  const env = setup({ search: '', name: 'ricardo-gpt-register' })
  env.ready()
  assert.ok(env.classes.has('embedded'))
  assert.deepEqual(env.sent, [])
  env.message({ origin: 'https://untrusted.example' })
  env.message({ origin: 'http://localhost:9000', source: {} })
  assert.deepEqual(env.sent, [])
  env.message({ origin: 'http://localhost:9000' })
  assert.equal(env.document.documentElement.dataset.theme, 'dark')
  assert.deepEqual(env.sent, [{ message: { type: 'turb-register-layout', height: 800 }, origin: 'http://localhost:9000' }])
  env.message({ origin: ORIGIN, data: { type: 'ricardo-ui-theme', theme: 'light' } })
  assert.equal(env.document.documentElement.dataset.theme, 'dark')
  assert.equal(env.sent.length, 1)
  env.assertNoForbiddenAccess()
})

test('explicit origin rejects spoofed source, different loopback origin, type, and theme', () => {
  const env = setup()
  env.ready()
  const attempts = [
    { source: {} },
    { origin: 'http://localhost:8768' },
    { origin: 'http://127.0.0.1:9000' },
    { origin: 'null' },
    { data: null },
    { data: { type: 'task-command', theme: 'dark' } },
    { data: { type: 'ricardo-ui-theme', theme: 'other' } },
    { data: { type: 'ricardo-ui-theme' } },
  ]
  attempts.forEach((attempt) => env.message(attempt))
  assert.equal(env.document.documentElement.dataset.theme, 'light')
  assert.equal(env.sent.length, 1)
  env.assertNoForbiddenAccess()
})

test('valid theme updates reannounce layout without reload, network requests, or persistence', () => {
  const env = setup()
  env.ready()
  env.message()
  assert.equal(env.document.documentElement.dataset.theme, 'dark')
  assert.equal(env.sent.length, 2)
  env.message({ data: { type: 'ricardo-ui-theme', theme: 'light', secret: 'NOT_FOR_FORWARDING' } })
  assert.equal(env.document.documentElement.dataset.theme, 'light')
  assert.deepEqual(env.sent[2], { message: { type: 'turb-register-layout', height: 800 }, origin: ORIGIN })
  assert.ok(!JSON.stringify(env.sent).includes('NOT_FOR_FORWARDING'))
  env.assertNoForbiddenAccess()
})

test('body measurement grows, shrinks, deduplicates and clamps malformed or excessive heights', () => {
  const env = setup()
  env.ready()
  env.resize(1200)
  env.resize(500)
  env.resize(500)
  env.dispatchResize()
  env.dispatchResize()
  env.flush()
  assert.deepEqual(env.sent.map(({ message }) => message.height), [800, 1200, 500])
  for (const height of [0, 199, -1, NaN, Infinity, -Infinity, 20000.1]) env.resize(height)
  assert.equal(env.sent.length, 3)
  env.resize(200)
  env.resize(20000)
  assert.deepEqual(env.sent.map(({ message }) => message.height), [800, 1200, 500, 200, 20000])
  env.assertNoForbiddenAccess()
})

test('a valid early theme message is retained until DOMContentLoaded', () => {
  const env = setup()
  env.message()
  assert.equal(env.document.documentElement.dataset.theme, 'dark')
  assert.deepEqual(env.sent, [])
  env.ready()
  assert.equal(env.sent.length, 1)
  env.assertNoForbiddenAccess()
})
