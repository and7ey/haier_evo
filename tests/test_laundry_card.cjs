const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

class Element {
  constructor() { this.events = {}; this.classList = { toggle: () => {} }; this.children = []; }
  setAttribute() {}
  addEventListener(name, callback) { this.events[name] = callback; }
  append(child) { this.children.push(child); }
  click() { if (!this.disabled) return this.events.click?.({ target: this }); }
}
class Shadow {
  set innerHTML(value) { this.html = value; this.nodes = {}; }
  getElementById(id) { return this.nodes[id] ||= new Element(); }
}
class HTMLElement { attachShadow() { this.shadowRoot = new Shadow(); return this.shadowRoot; } }
const classes = new Map();
classes.set('home-assistant', class {});
let confirm = false;
const context = {
  HTMLElement, document: { createElement: () => new Element(), querySelector: () => null },
  window: { confirm: () => confirm, customCards: [] },
  customElements: { define: (name, klass) => classes.set(name, klass), get: name => classes.get(name) },
  setTimeout: () => {},
};
const code = fs.readFileSync(path.join(__dirname, '../custom_components/haier_evo/haier-laundry-card.js'), 'utf8');
vm.runInNewContext(code, context);

async function fixture(status = 'idle') {
  const entries = [];
  const states = {};
  for (const [domain, suffix, value, options] of [
    ['select', 'selected_program', 'Cotton', ['None', 'Cotton']],
    ['binary_sensor', 'remote_control', 'on'],
    ['button', 'start', 'unknown'], ['button', 'pause', 'unknown'],
    ['button', 'resume', 'unknown'], ['button', 'cancel', 'unknown'],
    ['sensor', 'program_status', status], ['sensor', 'phase', 'Phase'],
  ]) {
    const entity_id = `${domain}.machine_${suffix}`;
    entries.push({ entity_id, device_id: 'machine', platform: 'haier_evo', unique_id: `machine_${suffix}` });
    states[entity_id] = { state: value, attributes: options ? { options } : {} };
  }
  const calls = [];
  const hass = { states, devices: {}, callWS: async () => entries, callService: async (...args) => calls.push(args) };
  const card = new (classes.get('haier-laundry-detail'))();
  card.setConfig({ device: 'machine', kind: 'dryer' });
  card.hass = hass;
  await new Promise(resolve => setImmediate(resolve));
  return { card, states, hass, calls };
}

test('unknown cycle state requires an explicit action choice', async () => {
  const { card, calls } = await fixture('unknown');
  assert.equal(card.shadowRoot.getElementById('start').disabled, false);
  assert.equal(card.shadowRoot.getElementById('toggle').disabled, false);
  card.shadowRoot.getElementById('start').click();
  card.shadowRoot.getElementById('toggle').click();
  assert.equal(calls.length, 0);
  await card.shadowRoot.getElementById('choose-pause').click();
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), ['button', 'press', { entity_id: 'button.machine_pause' }]);
});

test('running and paused select one appropriate action and connection loss blocks both', async () => {
  const { card, states, hass } = await fixture('running');
  assert.equal(card.shadowRoot.getElementById('toggle-label').textContent, 'Пауза');
  assert.equal(card.shadowRoot.getElementById('start').disabled, false);
  assert.equal(card.shadowRoot.getElementById('start-label').textContent, 'Отменить');
  assert.equal(card.shadowRoot.getElementById('toggle').disabled, false);
  states['sensor.machine_program_status'].state = 'paused';
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('toggle-label').textContent, 'Продолжить');
  states['binary_sensor.machine_remote_control'].state = 'unavailable';
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('toggle').disabled, true);
  assert.equal(card.shadowRoot.getElementById('cancel').disabled, true);
});

test('start needs confirmation and targets only the chosen device', async () => {
  const { card, calls } = await fixture();
  confirm = false;
  card.shadowRoot.getElementById('start').click();
  assert.equal(calls.length, 0);
  confirm = true;
  await card.shadowRoot.getElementById('start').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), ['button', 'press', { entity_id: 'button.machine_start' }]);
  confirm = false;
});

test('registration waits for the Home Assistant registry', () => {
  const registry = new Map();
  let retry;
  const sandbox = {
    HTMLElement, document: context.document, window: { customCards: [] },
    customElements: { define: (name, klass) => registry.set(name, klass), get: name => registry.get(name) },
    setTimeout: callback => { retry = callback; },
  };
  vm.runInNewContext(code, sandbox);
  assert.equal(registry.has('haier-laundry-detail'), false);
  registry.set('home-assistant', class {});
  retry();
  assert.equal(registry.has('haier-laundry-detail'), true);
});

test('program names get semantic icons with a generic fallback', async () => {
  const { card } = await fixture();
  assert.equal(card.programIcon('Постельное бельё'), 'mdi:bed');
  assert.equal(card.programIcon('Полотенца'), 'mdi:layers-outline');
  assert.equal(card.programIcon('Без названия'), 'mdi:tshirt-crew-outline');
});

test('only the machine entity is intercepted and a failed dialog falls back to Home Assistant', async () => {
  const handlers = {};
  const registry = new Map([['home-assistant', class {}]]);
  let removed = false;
  const details = { setConfig() {}, set hass(value) {} };
  const dialog = {
    dataset: {}, setAttribute() {}, addEventListener() {}, remove() { removed = true; },
    querySelector(selector) { return selector === 'haier-laundry-detail' ? details : new Element(); },
    showModal() { throw new Error('synthetic dialog failure'); },
  };
  const root = { hass: {
    callWS: async () => [{ platform: 'haier_evo', device_id: 'machine', entity_id: 'sensor.machine', unique_id: 'machine_overview' }],
    states: { 'sensor.machine': { attributes: {} } },
    connection: { subscribeEvents: async () => () => {}, addEventListener() {} },
  } };
  const sandbox = {
    HTMLElement, CustomEvent: class {}, requestAnimationFrame: () => {}, setTimeout: () => {},
    customElements: { define: (name, klass) => registry.set(name, klass), get: name => registry.get(name) },
    window: { addEventListener: (name, callback) => { handlers[name] = callback; } },
    document: { querySelector: selector => selector === 'home-assistant' ? root : null,
      createElement: () => dialog, body: { append() {} } },
  };
  vm.runInNewContext(code, sandbox);
  await new Promise(resolve => setImmediate(resolve));
  const event = id => ({ detail: { entityId: id }, stopped: false,
    preventDefault() {}, stopImmediatePropagation() { this.stopped = true; } });
  const other = event('sensor.other');
  handlers['hass-more-info'](other);
  assert.equal(other.stopped, false);
  const machine = event('sensor.machine');
  handlers['hass-more-info'](machine);
  assert.equal(removed, true);
  assert.equal(machine.stopped, false);
});
