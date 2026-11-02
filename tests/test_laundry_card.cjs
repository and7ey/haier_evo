const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

class Element {
  constructor() { this.events = {}; this.classList = { toggle: () => {} }; this.children = []; this.dataset = {}; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { this.events[name] = callback; }
  append(...children) { this.children.push(...children); }
  focus() { if (this.root) this.root.activeElement = this; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  checkValidity() { return this.valid !== false; }
  reportValidity() { this.reported = true; }
  click() { if (!this.disabled) return this.events.click?.({ target: this }); }
}
class Shadow {
  constructor() { this.nodes = {}; this.activeElement = null; }
  set innerHTML(value) { this.html = value; this.nodes = {}; this.activeElement = null; }
  getElementById(id) {
    if (!this.nodes[id]) { this.nodes[id] = new Element(); this.nodes[id].id = id; this.nodes[id].root = this; }
    return this.nodes[id];
  }
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
    ['haier_evo', 'machine', status], ['sensor', 'phase', 'Phase'],
  ]) {
    const entity_id = `${domain}.machine_${suffix}`;
    entries.push({ entity_id, device_id: 'machine', platform: 'haier_evo', unique_id: `machine_${suffix}` });
    states[entity_id] = { state: value, attributes: domain === 'haier_evo' ? { available_actions: ['start', 'pause', 'resume', 'cancel'] } : options ? { options } : {} };
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
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), ['haier_evo', 'execute_action', { entity_id: 'haier_evo.machine_machine', action: 'pause' }]);
});

test('running and paused select one appropriate action and connection loss blocks both', async () => {
  const { card, states, hass } = await fixture('running');
  assert.equal(card.shadowRoot.getElementById('toggle-label').textContent, 'Пауза');
  assert.equal(card.shadowRoot.getElementById('start').disabled, false);
  assert.equal(card.shadowRoot.getElementById('start-label').textContent, 'Отменить');
  assert.equal(card.shadowRoot.getElementById('toggle').disabled, false);
  states['haier_evo.machine_machine'].state = 'paused';
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('toggle-label').textContent, 'Продолжить');
  states['binary_sensor.machine_remote_control'].state = 'unavailable';
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('toggle').disabled, true);
  assert.equal(card.shadowRoot.getElementById('cancel').disabled, true);
});

test('active controls put pause first and keep cancellation behind confirmation', async () => {
  const { card, states, hass, calls } = await fixture('running');
  let html = card.shadowRoot.html;
  assert.ok(html.indexOf('id="toggle"') < html.indexOf('id="start"'));
  assert.match(html, /class="primary" id="toggle"/);
  assert.match(html, /class="destructive" id="start"/);
  confirm = false;
  card.shadowRoot.getElementById('start').click();
  assert.equal(calls.length, 0);
  confirm = true;
  card.shadowRoot.getElementById('start').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['haier_evo', 'execute_action', { entity_id: 'haier_evo.machine_machine', action: 'cancel' }]]);
  confirm = false;
  states['haier_evo.machine_machine'].state = 'idle';
  card.hass = hass;
  html = card.shadowRoot.html;
  assert.ok(html.indexOf('id="start"') < html.indexOf('id="toggle"'));
  assert.match(html, /class="primary" id="start"/);
});

test('active details show only device-reported values and return focus without commands', async () => {
  const { card, states, hass, calls } = await fixture('running');
  const viewChanges = [];
  card.onViewChange = value => viewChanges.push(value);
  const machine = states['haier_evo.machine_machine'];
  machine.attributes.program_settings = { program: 'Cotton', fields: [{ key: 'temp', title: 'Следующая температура', kind: 'number', value: '99' }] };
  machine.attributes.active_program_details = { program: 'Шерсть', remaining_minutes: 65, fields: [{ key: 'temp', title: 'Температура', value: '30 °C', description: 'Фактическое значение' }] };
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('current').textContent, 'Шерсть');
  assert.equal(card.shadowRoot.getElementById('remaining').textContent, 'Осталось 01:05');
  assert.match(card.shadowRoot.getElementById('completion').textContent, /^Примерно до /);
  assert.equal(card.shadowRoot.getElementById('program-fields').hidden, true);
  assert.equal(card.shadowRoot.getElementById('program-settings').hidden, true);
  card.shadowRoot.getElementById('show-details').click();
  assert.equal(viewChanges.at(-1), true);
  assert.equal(card.shadowRoot.getElementById('details-back').hidden, false);
  assert.equal(card.shadowRoot.getElementById('main-view').hidden, true);
  assert.equal(card.shadowRoot.getElementById('details-view').hidden, false);
  assert.equal(card.shadowRoot.getElementById('active-program').textContent, 'Шерсть');
  const fields = card.shadowRoot.getElementById('active-fields').children;
  assert.equal(fields.length, 1);
  assert.equal(fields[0].children[1].textContent, '30 °C');
  assert.equal(card.shadowRoot.activeElement.id, 'details-back');
  card.shadowRoot.getElementById('details-back').click();
  assert.equal(viewChanges.at(-1), false);
  assert.equal(card.shadowRoot.activeElement.id, 'show-details');
  assert.equal(calls.length, 0);
});

test('missing or invalid active details are not invented, pause has no completion estimate', async () => {
  const { card, states, hass } = await fixture('running');
  const machine = states['haier_evo.machine_machine'];
  machine.attributes.active_program_details = { program: 'Хлопок', remaining_minutes: -1, fields: [] };
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('remaining').textContent, undefined);
  assert.equal(card.shadowRoot.getElementById('completion').textContent, undefined);
  assert.equal(card.shadowRoot.getElementById('active-empty').hidden, false);
  machine.attributes.active_program_details.remaining_minutes = 10;
  machine.state = 'paused';
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('remaining').textContent, 'Осталось 00:10');
  assert.equal(card.shadowRoot.getElementById('completion').textContent, undefined);
});

test('progress requires a valid total and motion keeps its phase across updates', async () => {
  const { card, states, hass } = await fixture('running');
  const machine = states['haier_evo.machine_machine'];
  machine.attributes.active_program_details = { program: 'Cotton', remaining_minutes: 10, fields: [] };
  card.hass = hass;
  const start = card.motionSince;
  assert.equal(card.shadowRoot.getElementById('meter').attributes['stroke-dasharray'], undefined);
  machine.attributes.active_program_details.total_minutes = 40;
  card.hass = hass;
  assert.equal(card.motionSince, start);
  assert.equal(card.shadowRoot.getElementById('meter').attributes['stroke-dasharray'], '75 100');
  machine.attributes.active_program_details.total_minutes = 10;
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('meter').attributes['stroke-dasharray'], undefined);
  machine.state = 'paused';
  card.hass = hass;
  assert.equal(card.motionSince, null);
});

test('remote info is informational and reflects unknown state', async () => {
  const { card, states, hass, calls } = await fixture();
  states['binary_sensor.machine_remote_control'].state = 'unknown';
  card.hass = hass;
  const button = card.shadowRoot.getElementById('remote-info-button');
  assert.match(button.attributes['aria-label'], /неизвестно/);
  button.click();
  assert.equal(card.shadowRoot.getElementById('remote-info').hidden, false);
  assert.equal(button.attributes['aria-expanded'], 'true');
  assert.equal(calls.length, 0);
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
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), ['haier_evo', 'execute_action', { entity_id: 'haier_evo.machine_machine', action: 'start' }]);
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
    callWS: async () => [{ platform: 'haier_evo', device_id: 'machine', entity_id: 'haier_evo.machine', unique_id: 'machine_machine' }],
    states: { 'haier_evo.machine': { attributes: {} } },
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
  const machine = event('haier_evo.machine');
  handlers['hass-more-info'](machine);
  assert.equal(removed, true);
  assert.equal(machine.stopped, false);
});


test('search filters by words, case and е/ё; clearing restores the full list', async () => {
  const { card, states, hass, calls } = await fixture();
  states['select.machine_selected_program'].state = 'Не выбрана';
  states['select.machine_selected_program'].attributes.options = ['Не выбрана', 'Постельное бельё', 'Полотенца'];
  card.hass = hass;
  card.shadowRoot.getElementById('program').click();
  let search = card.shadowRoot.getElementById('program-search');
  assert.equal(card.shadowRoot.activeElement, search);
  search.value = 'БЕЛЬЕ постельное';
  search.events.input();
  let list = card.shadowRoot.getElementById('program-list').children;
  assert.deepEqual(list.map(option => option.hidden), [true, false, true]);
  assert.equal(card.shadowRoot.getElementById('program-empty').hidden, true);
  search.value = 'нет совпадений';
  search.events.input();
  assert.equal(card.shadowRoot.getElementById('program-empty').hidden, false);
  card.shadowRoot.getElementById('clear-search').click();
  assert.equal(search.value, '');
  assert.equal(card.shadowRoot.activeElement, search);
  assert.deepEqual(list.map(option => option.hidden), [false, false, false]);
  assert.equal(calls.length, 0);
});

test('search text, caret and focus survive a Home Assistant state update; Escape closes only the list', async () => {
  const { card, states, hass, calls } = await fixture();
  card.shadowRoot.getElementById('program').click();
  let search = card.shadowRoot.getElementById('program-search');
  search.value = 'Cott';
  search.events.input();
  search.setSelectionRange(1, 3);
  states['haier_evo.machine_machine'].attributes.available_actions = ['start'];
  card.hass = hass;
  search = card.shadowRoot.getElementById('program-search');
  assert.equal(search.value, 'Cott');
  assert.equal(card.shadowRoot.activeElement, search);
  assert.deepEqual([search.selectionStart, search.selectionEnd], [1, 3]);
  let stopped = false;
  let prevented = false;
  card.shadowRoot.getElementById('program-options').events.keydown({ key: 'Escape', stopPropagation() { stopped = true; }, preventDefault() { prevented = true; } });
  assert.equal(stopped, true);
  assert.equal(prevented, true);
  assert.equal(card.programMenuOpen, false);
  assert.equal(card.shadowRoot.activeElement.id, 'program');
  assert.equal(calls.length, 0);
});


test('specific cycle icons take precedence over the program category', async () => {
  const { card } = await fixture();
  for (const [name, icon] of [
    ['Авто', 'mdi:autorenew'], ['Антиаллергия', 'mdi:shield-check'],
    ['UV-обработка', 'mdi:white-balance-sunny'], ['Быстрая 15 мин', 'mdi:timer-outline'],
    ['Стирка с паром - Деликатная', 'mdi:flower-outline'], ['Освежить', 'mdi:refresh'],
    ['Отжим', 'mdi:rotate-right'], ['Очистка барабана', 'mdi:washing-machine'],
    ['Смешанная', 'mdi:tshirt-crew'], ['Хлопок+', 'mdi:flower'], ['Шерсть', 'mdi:sheep'],
    ['Пуховые вещи', 'mdi:weather-cloudy'], ['Синтетика', 'mdi:tshirt-v-outline'],
    ['Детская одежда', 'mdi:baby-face-outline'],
  ]) assert.equal(card.programIcon(name), icon, name);
});


test('matching programs from different categories remain distinct and choosing one only selects it', async () => {
  const { card, states, hass, calls } = await fixture();
  states['select.machine_selected_program'].state = 'Не выбрана';
  states['select.machine_selected_program'].attributes.options = ['Не выбрана', 'Ткани - Деликатная', 'Стирка с паром - Деликатная'];
  card.hass = hass;
  card.shadowRoot.getElementById('program').click();
  const search = card.shadowRoot.getElementById('program-search');
  search.value = 'деликатная';
  search.events.input();
  const list = card.shadowRoot.getElementById('program-list').children;
  assert.deepEqual(list.map(option => option.hidden), [true, false, false]);
  list[1].click();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['select', 'select_option', { entity_id: 'select.machine_selected_program', option: 'Ткани - Деликатная' }]]);
  assert.equal(card.programMenuOpen, false);
  assert.equal(card.programQuery, '');
});


test('catalog preserves category order, descriptions, durations, and duplicate options', async () => {
  const { card, states, hass } = await fixture();
  states['select.machine_selected_program'].attributes.options = ['None', 'Cotton', 'Delicate'];
  states['haier_evo.machine_machine'].attributes.program_catalog = [
    { title: 'Ткани', description: 'Для разных тканей', programs: [
      { option: 'Cotton', name: 'Хлопок', duration: '42 минуты', description: 'Белые вещи' },
      { option: 'Delicate', name: 'Деликатная', duration: '', description: 'Чувствительная ткань' },
    ] },
    { title: 'Любимые', description: 'Повтор программы', programs: [
      { option: 'Cotton', name: 'Хлопок', duration: '42 минуты', description: 'Второй вариант' },
    ] },
  ];
  card.hass = hass;
  card.shadowRoot.getElementById('program').click();
  assert.deepEqual(Array.from(card.programGroups, group => group.title), ['Ткани', 'Любимые']);
  assert.deepEqual(Array.from(card.programEntries, entry => entry.element.dataset.option), ['None', 'Cotton', 'Delicate', 'Cotton']);
  assert.equal(card.programGroups[0].element.children[0].textContent, 'Ткани');
  const cottonCopy = card.programGroups[0].entries[0].element.children[1];
  assert.equal(cottonCopy.children[1].textContent, '42 минуты');
  assert.equal(cottonCopy.children[2].textContent, 'Белые вещи');
  const search = card.shadowRoot.getElementById('program-search');
  search.value = 'повтор хлопок';
  search.events.input();
  assert.deepEqual(Array.from(card.programGroups, group => group.element.hidden), [true, false]);
  assert.equal(card.programGroups[1].entries[0].element.hidden, false);
  search.value = 'чувствительная';
  search.events.input();
  assert.deepEqual(Array.from(card.programGroups, group => group.element.hidden), [false, true]);
  assert.equal(card.programGroups[0].entries[1].element.hidden, false);
});

test('settings require matching program and each validated change configures once', async () => {
  const { card, states, hass, calls } = await fixture();
  const machine = states['haier_evo.machine_machine'];
  machine.attributes.program_settings = { program: 'Cotton', values: { temp: '40', dirt: '2', time: '20' }, fields: [
    { key: 'temp', title: 'Температура', kind: 'select', value: '40', options: [{ value: '40', label: '40 °C' }, { value: '60', label: '60 °C' }] },
    { key: 'dirt', title: 'Загрязнение', kind: 'select', value: '2', disabled: true, options: [{ value: '2', label: 'Сильное', description: 'Для сильно загрязнённых вещей' }] },
    { key: 'time', title: 'Время', kind: 'number', value: '20', min: 10, max: 90, step: 5, unit: 'мин' },
  ] };
  card.hass = hass;
  const fields = card.shadowRoot.getElementById('settings-grid').children;
  assert.equal(card.shadowRoot.getElementById('program-settings').hidden, false);
  assert.equal(fields.length, 3);
  assert.equal(fields[1].children[1].disabled, true);
  assert.match(fields[1].children[2].textContent, /сильно загрязнённых/);
  assert.match(fields[1].children[2].textContent, /Недоступно/);
  assert.equal(fields[1].children[1].attributes['aria-describedby'], fields[1].children[2].id);
  const invalid = fields[2].children[1];
  invalid.value = '23'; invalid.valid = false; invalid.events.change();
  assert.equal(invalid.reported, true);
  assert.equal(calls.length, 0);
  const temperature = fields[0].children[1];
  temperature.value = '60'; temperature.events.change();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['haier_evo', 'configure_program', { entity_id: 'haier_evo.machine_machine', program: 'Cotton', parameters: { temp: '60' } }]]);
  machine.attributes.program_settings = { program: 'Different', fields: [{ key: 'temp', kind: 'select' }], values: {} };
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('program-settings').hidden, true);
});


test('settings preserve an unfinished numeric edit across unrelated updates', async () => {
  const { card, states, hass, calls } = await fixture();
  const machine = states['haier_evo.machine_machine'];
  machine.attributes.program_settings = { program: 'Cotton', values: { time: '20' }, fields: [
    { key: 'time', title: 'Время', kind: 'number', min: 10, max: 90, unit: 'мин' },
  ] };
  card.hass = hass;
  card.shadowRoot.getElementById('program-settings').open = true;
  let input = card.shadowRoot.getElementById('settings-grid').children[0].children[1];
  input.root = card.shadowRoot;
  input.focus();
  input.value = '25';
  states['sensor.machine_phase'].state = 'New phase';
  card.hass = hass;
  input = card.shadowRoot.getElementById('settings-grid').children[0].children[1];
  assert.equal(input.value, '25');
  assert.equal(calls.length, 0);
  machine.attributes.program_settings.values.time = '30';
  input.root = card.shadowRoot;
  input.focus();
  states['sensor.machine_phase'].state = 'Another phase';
  card.hass = hass;
  input = card.shadowRoot.getElementById('settings-grid').children[0].children[1];
  assert.equal(input.value, '30');
});

test('partial catalog keeps ungrouped options available', async () => {
  const { card, states, hass } = await fixture();
  states['select.machine_selected_program'].attributes.options = ['None', 'Cotton', 'Delicate'];
  states['haier_evo.machine_machine'].attributes.program_catalog = [{ title: 'Ткани', description: '', programs: [{ option: 'Cotton', name: 'Хлопок' }] }];
  card.hass = hass;
  card.shadowRoot.getElementById('program').click();
  assert.deepEqual(Array.from(card.programGroups, group => group.title), ['Ткани', 'Другие программы']);
  assert.deepEqual(Array.from(card.programEntries, entry => entry.element.dataset.option), ['None', 'Cotton', 'Delicate']);
});

test('settings errors are shown only for the selected program; active cycles disable editing', async () => {
  const { card, states, hass, calls } = await fixture();
  const machine = states['haier_evo.machine_machine'];
  machine.attributes.program_settings = { program: 'Cotton', fields: [], error: 'Нет данных' };
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('program-settings').hidden, false);
  assert.match(card.shadowRoot.getElementById('settings-error').textContent, /Нет данных/);
  machine.attributes.program_settings = { program: 'Other', fields: [], error: 'Старое сообщение' };
  card.hass = hass;
  assert.equal(card.shadowRoot.getElementById('program-settings').hidden, true);
  machine.attributes.program_settings = { program: 'Cotton', values: { temp: '40' }, fields: [{ key: 'temp', title: 'Температура', kind: 'select', options: [{value:'40',label:'40 °C'},{value:'60',label:'60 °C'}] }] };
  machine.state = 'running';
  card.hass = hass;
  const control = card.shadowRoot.getElementById('settings-grid').children[0].children[1];
  assert.equal(control.disabled, true);
  control.value = '60'; control.events.change();
  assert.equal(calls.length, 0);
});
