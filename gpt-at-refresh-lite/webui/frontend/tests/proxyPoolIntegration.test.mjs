import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, disposePinia, setActivePinia, storeToRefs } from 'pinia'
import { nextTick } from 'vue'
import { useProxyStore } from '../src/stores/proxy.js'
import { useFormStore, proxyText } from '../src/stores/form.js'

test('embedded account and proxy panels share the existing pool and persistence keys', async (t) => {
  // In-memory storage only: never read or modify the user's browser settings.
  const firstProxy = 'http://192.0.2.10:8080'
  const secondProxy = 'socks5://192.0.2.11:1080'
  const memory = new Map([
    ['dango_proxy_pool_v1', JSON.stringify([firstProxy])],
    ['gpt_outlook_register_form_v2', JSON.stringify({ proxy: firstProxy })],
  ])
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const stores = []
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: (key) => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value) },
  })
  t.after(() => {
    for (const store of stores) disposePinia(store)
    if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
    else delete globalThis.localStorage
  })

  const pinia = createPinia()
  stores.push(pinia)
  setActivePinia(pinia)
  const accountsPool = useProxyStore()
  const proxyPanelPool = useProxyStore()
  const { list: accountOptions } = storeToRefs(accountsPool)
  const form = useFormStore()
  assert.equal(accountsPool, proxyPanelPool)
  assert.deepEqual(accountOptions.value, [firstProxy])
  assert.equal(proxyText(form.form), firstProxy)

  assert.deepEqual(proxyPanelPool.append(`${firstProxy}\n${secondProxy}`), { added: 1 })
  await nextTick()
  assert.deepEqual(accountOptions.value, [firstProxy, secondProxy])
  assert.equal(proxyText(form.form), firstProxy)
  assert.deepEqual(JSON.parse(memory.get('dango_proxy_pool_v1')), [firstProxy, secondProxy])

  const reloadedPinia = createPinia()
  stores.push(reloadedPinia)
  setActivePinia(reloadedPinia)
  assert.deepEqual(useProxyStore().list, [firstProxy, secondProxy])
  assert.equal(proxyText(useFormStore().form), firstProxy)
})
