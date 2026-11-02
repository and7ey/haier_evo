class HaierLaundryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.pending = false;
    this.error = '';
  }

  setConfig(config) {
    if (!config.device) throw new Error('Выберите устройство Haier');
    this.config = config;
    this.entities = null;
    this.loadFailed = false;
    this.signature = null;
    this.loadEntities();
    this.render();
  }

  async loadEntities() {
    if (!this.config || !this._hass) return;
    try {
      const device = this.config.device;
      const entries = await this._hass.callWS({ type: 'config/entity_registry/list' });
      if (device !== this.config.device) return;
      const bySuffix = (domain, suffix) => entries.find(entry =>
        entry.device_id === device && entry.platform === 'haier_evo' &&
        entry.entity_id.startsWith(`${domain}.`) && entry.unique_id?.endsWith(`_${suffix}`)
      )?.entity_id;
      this.entities = {
        program: bySuffix('select', 'selected_program'), remote: bySuffix('binary_sensor', 'remote_control'),
        start: bySuffix('button', 'start'), pause: bySuffix('button', 'pause'),
        resume: bySuffix('button', 'resume'), cancel: bySuffix('button', 'cancel'),
        status: bySuffix('sensor', 'program_status'), phase: bySuffix('sensor', 'phase'),
        current_program: bySuffix('sensor', 'program_name'), duration: bySuffix('sensor', 'remaining_time'),
      };
      this.loadFailed = false;
      this.error = '';
    } catch (_) {
      this.loadFailed = true;
      this.error = 'Не удалось загрузить сущности устройства.';
    }
    this.signature = null;
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.config) return;
    if (!this.entities) {
      if (!this.loading && !this.loadFailed) {
        this.loading = true;
        this.loadEntities().finally(() => { this.loading = false; });
      }
      return;
    }
    const ids = ['program', 'remote', 'start', 'pause', 'resume', 'cancel', 'status', 'phase', 'current_program', 'duration'];
    const signature = JSON.stringify(ids.map(key => {
      const state = hass.states[this.entities[key]];
      return [state?.state, state?.attributes?.options];
    }));
    if (signature !== this.signature) {
      this.signature = signature;
      this.render();
    }
  }

  getCardSize() { return 6; }

  state(key) { return this._hass?.states[this.entities?.[key]]; }
  value(key) {
    const value = this.state(key)?.state;
    return value && !['unknown', 'unavailable', 'None'].includes(value) ? value : null;
  }
  usable(key) {
    const state = this.state(key);
    return !!state && state.state !== 'unavailable';
  }

  async send(domain, service, data) {
    if (this.pending) return;
    this.pending = true;
    this.error = '';
    this.render();
    try {
      await this._hass.callService(domain, service, data);
    } catch (_) {
      this.error = 'Не удалось подтвердить выполнение команды. Проверьте машину.';
    } finally {
      this.pending = false;
      this.render();
    }
  }

  render() {
    if (!this.config || !this._hass) return;
    if (!this.entities) {
      this.shadowRoot.innerHTML = '<ha-card style="padding:20px"><span id="message" role="status"></span><button id="retry" hidden>Повторить</button></ha-card>';
      this.shadowRoot.getElementById('message').textContent = this.error || 'Загрузка устройства...';
      const retry = this.shadowRoot.getElementById('retry');
      retry.hidden = !this.loadFailed;
      retry.addEventListener('click', () => { this.loadFailed = false; this.error = ''; this.loadEntities(); });
      return;
    }
    const c = { ...this.entities, ...this.config };
    const status = this.value('status');
    const statuses = { idle: 'Ожидание', running: 'Выполняется', paused: 'Пауза', finished: 'Программа завершена', cancelled: 'Программа отменена' };
    const phaseValue = this.value('phase');
    const statusText = statuses[status] || (phaseValue && !/^(Код|Code)\s/.test(phaseValue) ? phaseValue : 'Нет данных о цикле');
    const options = this.state('program')?.attributes?.options || [];
    const selected = this.value('program');
    const unselected = options[0];
    const selectedProgram = selected && selected !== unselected && options.includes(selected);
    const remote = this.value('remote');
    const connected = remote === 'on';
    const active = status === 'running' || status === 'paused';
    const uncertain = !status;
    const canStart = connected && selectedProgram && this.usable('start') && (status === 'idle' || uncertain) && !this.pending;
    const action = status === 'running' ? 'pause' : status === 'paused' ? 'resume' : null;
    const canToggle = connected && action && this.usable(action) && !this.pending;
    const canCancel = connected && (active || uncertain) && this.usable('cancel') && !this.pending;
    const canChooseToggle = connected && uncertain && !this.pending && (this.usable('pause') || this.usable('resume'));
    const isDryer = c.kind === 'dryer';
    const device = this._hass.devices?.[c.device];
    if (!connected || active || this.pending) this.programMenuOpen = false;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, inherit); }
        ha-card { padding: 22px; overflow: hidden; }
        h2, p { margin: 0; }
        .heading { display: flex; gap: 12px; align-items: center; padding-right: 44px; }
        .heading ha-icon { color: var(--primary-color); --mdc-icon-size: 25px; }
        h2 { font-size: 19px; font-weight: 600; line-height: 1.3; }
        .connection { margin-top: 3px; color: var(--secondary-text-color); font-size: 13px; }
        .visual { display: flex; min-height: 145px; align-items: center; justify-content: center; gap: 25px; margin: 18px 0; }
        .drum { position: relative; width: 112px; height: 112px; border-radius: 50%; background: var(--secondary-background-color); display: grid; place-items: center; flex-shrink: 0; }
        .drum ha-icon { --mdc-icon-size: 68px; color: var(--primary-text-color); opacity: .78; }
        .drum.running::after { content: ''; position: absolute; inset: 4px; border: 2px solid transparent; border-top-color: var(--primary-color); border-radius: 50%; animation: cycle 4s linear infinite; }
        @keyframes cycle { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) { .drum.running::after { animation: none; } }
        .summary { min-width: 0; max-width: 250px; }
        .status { font-size: 21px; font-weight: 600; line-height: 1.25; overflow-wrap: anywhere; }
        .phase, .current { margin-top: 7px; font-size: 14px; color: var(--secondary-text-color); overflow-wrap: anywhere; }
        .fields { display: grid; gap: 8px; }
        label { color: var(--secondary-text-color); font-size: 13px; }
        .program-trigger { box-sizing: border-box; width: 100%; min-height: 48px; padding: 0 12px; border: 1px solid var(--divider-color); border-radius: 9px; background: var(--secondary-background-color); color: var(--primary-text-color); font: inherit; justify-content: flex-start; text-align: left; }
        .program-options { max-height: 230px; overflow-y: auto; border-radius: 9px; background: var(--secondary-background-color); }
        .program-option { width: 100%; justify-content: flex-start; text-align: left; border-radius: 0; }
        .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
        .actions[hidden] { display: none; }
        button { min-height: 44px; border: 0; border-radius: 9px; padding: 0 14px; background: var(--secondary-background-color); color: var(--primary-text-color); font: inherit; cursor: pointer; display: inline-flex; gap: 7px; align-items: center; justify-content: center; }
        button.primary { background: var(--primary-color); color: var(--text-primary-color, white); }
        button ha-icon { --mdc-icon-size: 19px; }
        button:disabled { opacity: .48; cursor: not-allowed; }
        button:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
        .note { margin-top: 13px; font-size: 13px; line-height: 1.4; color: var(--secondary-text-color); }
        .error { margin-top: 10px; color: var(--error-color); font-size: 13px; }
        @media (max-width: 380px) { ha-card { padding: 17px; } .visual { gap: 16px; } .drum { width: 88px; height: 88px; } .drum ha-icon { --mdc-icon-size: 55px; } .actions button { flex: 1 1 auto; } }
      </style>
      <ha-card>
        <div class="heading"><ha-icon id="header-icon"></ha-icon><div><h2 id="name"></h2><p class="connection" id="connection"></p></div></div>
        <div class="visual"><div class="drum" id="drum" aria-hidden="true"><ha-icon id="machine-icon"></ha-icon></div><div class="summary"><p class="status" id="status"></p><p class="phase" id="phase"></p><p class="current" id="current"></p></div></div>
        <div class="fields"><label>Программа для запуска</label><button type="button" class="program-trigger" id="program" aria-haspopup="true" aria-expanded="false"><ha-icon id="selected-icon"></ha-icon><span id="selected-name"></span></button><div class="program-options" id="program-options" hidden></div></div>
        <div class="actions">
          <button class="primary" id="start" aria-controls="start-choices" aria-expanded="false"><ha-icon id="start-icon"></ha-icon><span id="start-label"></span></button>
          <button id="toggle" aria-controls="toggle-choices" aria-expanded="false"><ha-icon id="toggle-icon"></ha-icon><span id="toggle-label"></span></button>
        </div>
        <div class="actions" id="start-choices" hidden><button id="choose-start">Запустить программу</button><button id="cancel">Отменить программу</button></div>
        <div class="actions" id="toggle-choices" hidden><button id="choose-pause">Пауза</button><button id="choose-resume">Продолжить</button>
        </div>
        <p class="note" id="note"></p><p class="error" id="error" role="alert"></p>
        <button type="button" id="native"><ha-icon icon="mdi:history"></ha-icon>История и настройки</button>
      </ha-card>`;
    const $ = id => this.shadowRoot.getElementById(id);
    $('name').textContent = c.name || device?.name_by_user || device?.name || (isDryer ? 'Сушильная машина' : 'Стиральная машина');
    $('drum').classList.toggle('running', status === 'running');
    $('header-icon').setAttribute('icon', isDryer ? 'mdi:tumble-dryer' : 'mdi:washing-machine');
    $('machine-icon').setAttribute('icon', isDryer ? 'mdi:tumble-dryer' : 'mdi:washing-machine');
    $('connection').textContent = remote === 'on' ? 'Дистанционное управление включено' : remote === 'off' ? 'Дистанционное управление выключено' : 'Нет связи с устройством';
    $('status').textContent = remote === null ? 'Нет данных об устройстве' : statusText;
    const phase = this.value('phase');
    $('phase').textContent = remote !== null && phase && !/^(Код|Code)\s/.test(phase) && phase !== statusText && !(active && phase === 'Режим ожидания') ? phase : '';
    const current = this.value('current_program');
    const duration = this.value('duration');
    $('current').textContent = remote === null ? '' : (active && current === 'Нет активной программы' ? '' : current) || (duration ? `Время программы: ${duration} мин` : '');
    const select = $('program');
    $('selected-icon').setAttribute('icon', this.programIcon(selected));
    $('selected-name').textContent = selected || 'Нет доступных программ';
    select.setAttribute('aria-label', `Программа для запуска: ${selected || 'нет доступных программ'}`);
    select.setAttribute('aria-expanded', String(!!this.programMenuOpen));
    $('program-options').hidden = !this.programMenuOpen;
    for (const option of options) {
      const element = document.createElement('button');
      const icon = document.createElement('ha-icon');
      const label = document.createElement('span');
      element.type = 'button';
      element.className = 'program-option';
      icon.setAttribute('icon', this.programIcon(option));
      label.textContent = option;
      element.append(icon, label);
      element.addEventListener('click', () => {
        this.programMenuOpen = false;
        this.send('select', 'select_option', { entity_id: c.program, option });
      });
      $('program-options').append(element);
    }
    select.disabled = !connected || !this.usable('program') || active || this.pending;
    select.addEventListener('click', () => { this.programMenuOpen = !this.programMenuOpen; this.render(); this.shadowRoot.getElementById('program').focus(); });
    $('start').disabled = !(uncertain ? canStart || canCancel : active ? canCancel : canStart);
    $('toggle').disabled = !(canToggle || canChooseToggle);
    $('choose-start').disabled = !canStart;
    $('cancel').disabled = !canCancel;
    $('choose-pause').disabled = !canChooseToggle || !this.usable('pause');
    $('choose-resume').disabled = !canChooseToggle || !this.usable('resume');
    $('start-icon').setAttribute('icon', active ? 'mdi:stop' : 'mdi:play');
    $('start-label').textContent = uncertain ? 'Запуск / отмена' : active ? 'Отменить' : 'Запустить';
    $('toggle-icon').setAttribute('icon', action === 'resume' ? 'mdi:play' : 'mdi:pause');
    $('toggle-label').textContent = action === 'resume' ? 'Продолжить' : action === 'pause' ? 'Пауза' : 'Пауза / продолжить';
    $('note').textContent = !connected ? 'Управление недоступно, пока нет связи или выключен удалённый доступ.' : !status ? 'Состояние цикла не получено. Выберите действие вручную.' : ['finished', 'cancelled'].includes(status) ? 'Для запуска дождитесь подтверждённого режима ожидания.' : 'Запуск и отмена требуют подтверждения.';
    $('error').textContent = this.error;
    const start = () => {
      if (canStart && window.confirm(`Запустить программу «${selected}»?`)) this.send('button', 'press', { entity_id: c.start });
    };
    const cancel = () => {
      if (canCancel && window.confirm('Отменить текущую программу?')) this.send('button', 'press', { entity_id: c.cancel });
    };
    $('start').addEventListener('click', () => {
      if (uncertain) {
        $('start-choices').hidden = !$('start-choices').hidden;
        $('start').setAttribute('aria-expanded', String(!$('start-choices').hidden));
      }
      else if (active) cancel();
      else start();
    });
    $('toggle').addEventListener('click', () => {
      if (canToggle) this.send('button', 'press', { entity_id: c[action] });
      else if (canChooseToggle) {
        $('toggle-choices').hidden = !$('toggle-choices').hidden;
        $('toggle').setAttribute('aria-expanded', String(!$('toggle-choices').hidden));
      }
    });
    $('choose-start').addEventListener('click', start);
    $('cancel').addEventListener('click', cancel);
    for (const choice of ['pause', 'resume']) {
      $('choose-' + choice).addEventListener('click', () => {
        if (canChooseToggle && this.usable(choice)) this.send('button', 'press', { entity_id: c[choice] });
      });
    }
    $('native').addEventListener('click', () => this.dispatchEvent(new CustomEvent('haier-show-native', { bubbles: true, composed: true })));
  }

  programIcon(name = '') {
    const text = String(name).toLocaleLowerCase();
    const icons = [
      [/постель|бель[её] пост|bed/, 'mdi:bed'], [/детск|baby/, 'mdi:baby-face-outline'],
      [/питом|pet/, 'mdi:paw'], [/полотен|towel/, 'mdi:layers-outline'], [/джинс|jeans/, 'mdi:hanger'],
      [/рубаш|shirt/, 'mdi:tshirt-crew'], [/спорт|sport/, 'mdi:run'], [/штор|curtain/, 'mdi:curtains'],
      [/шерст|wool/, 'mdi:sheep'], [/быстр|quick|времени|time/, 'mdi:timer-outline'],
      [/ночн|night/, 'mdi:weather-night'], [/паром|steam/, 'mdi:weather-fog'],
      [/полоскан|rinse/, 'mdi:water'], [/пух|объ[её]мн|пухов/, 'mdi:weather-cloudy'],
      [/школьн|school/, 'mdi:school-outline'], [/обув|shoe/, 'mdi:shoe-sneaker'],
      [/хлопок|cotton/, 'mdi:flower'], [/деликат|кашемир|delicate/, 'mdi:flower-outline'],
      [/мембран|waterproof/, 'mdi:water-outline'], [/подогрев|warm/, 'mdi:heat-wave'],
      [/синтет|synthetic/, 'mdi:tshirt-v-outline'],
      [/не выбран|not selected/, 'mdi:format-list-bulleted'],
    ];
    return icons.find(([pattern]) => pattern.test(text))?.[1] || 'mdi:tshirt-crew-outline';
  }
}


function registerHaierDetails(attempt = 0) {
  // shortcut: wait for the frontend registry to avoid HA frontend#53890; remove when fixed upstream.
  if (!customElements.get('home-assistant')) {
    if (attempt < 1200) setTimeout(() => registerHaierDetails(attempt + 1), 50);
    return;
  }
  if (!customElements.get('haier-laundry-detail')) customElements.define('haier-laundry-detail', HaierLaundryCard);
  initializeHaierDetails();
}

async function initializeHaierDetails(attempt = 0) {
  const root = document.querySelector('home-assistant');
  const hass = root?.hass;
  if (!hass?.callWS || !hass?.connection) {
    if (attempt < 1200) setTimeout(() => initializeHaierDetails(attempt + 1), 50);
    return;
  }
  if (window.__haierLaundryDetailsInstalled) return;
  window.__haierLaundryDetailsInstalled = true;
  let entities = new Map();
  const refresh = async () => {
    try {
      const registry = await root.hass.callWS({ type: 'config/entity_registry/list' });
      entities = new Map(registry.filter(entry => entry.platform === 'haier_evo' && entry.device_id &&
        entry.entity_id.startsWith('sensor.') && entry.unique_id?.endsWith('_overview'))
        .map(entry => [entry.entity_id, entry.device_id]));
      return true;
    } catch (_) {
      return false;
    }
  };
  if (!await refresh()) {
    window.__haierLaundryDetailsInstalled = false;
    if (attempt < 1200) setTimeout(() => initializeHaierDetails(attempt + 1), 1000);
    return;
  }
  hass.connection.subscribeEvents(refresh, 'entity_registry_updated').catch(() => {});
  hass.connection.addEventListener?.('ready', refresh);
  window.addEventListener('hass-more-info', event => {
    const entityId = event.detail?.entityId;
    const deviceId = entities.get(entityId);
    if (!deviceId || !root.hass || event.detail?.haierNative) return;
    let dialog;
    try {
      const previous = document.querySelector('dialog[data-haier-laundry]');
      if (previous) previous.close();
      dialog = document.createElement('dialog');
      dialog.dataset.haierLaundry = '';
      dialog.setAttribute('aria-label', 'Управление машиной');
      dialog.innerHTML = `<style>
        dialog[data-haier-laundry] { box-sizing: border-box; width: min(560px, calc(100vw - 24px)); max-width: none; max-height: calc(100dvh - 24px); padding: 0; border: 0; border-radius: 18px; background: var(--card-background-color, var(--primary-background-color)); color: var(--primary-text-color); box-shadow: 0 16px 50px #0006; }
        dialog[data-haier-laundry]::backdrop { background: #0009; }
        dialog[data-haier-laundry] .close { position: sticky; top: 8px; z-index: 1; float: right; width: 44px; height: 44px; margin: 8px 8px -52px 0; border: 0; border-radius: 50%; background: var(--secondary-background-color); color: var(--primary-text-color); cursor: pointer; }
        dialog[data-haier-laundry] .close:focus-visible { outline: 3px solid var(--primary-color); outline-offset: -3px; }
        dialog[data-haier-laundry] haier-laundry-detail { display: block; }
      </style><button class="close" type="button" aria-label="Закрыть"><ha-icon icon="mdi:close"></ha-icon></button><haier-laundry-detail></haier-laundry-detail>`;
      const details = dialog.querySelector('haier-laundry-detail');
      const icon = root.hass.states[entityId]?.attributes?.icon || '';
      details.setConfig({ device: deviceId, kind: icon.includes('tumble') ? 'dryer' : 'washer' });
      details.hass = root.hass;
      dialog.querySelector('.close').addEventListener('click', () => dialog.close());
      dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
      dialog.addEventListener('haier-show-native', () => {
        dialog.close();
        root.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId, haierNative: true }, bubbles: true, composed: true }));
      });
      let unsubscribe;
      hass.connection.subscribeEvents(update => {
        if (update.data?.entity_id !== entityId && !Object.values(details.entities || {}).includes(update.data?.entity_id)) return;
        requestAnimationFrame(() => { if (dialog.open && root.hass) details.hass = root.hass; });
      }, 'state_changed').then(unsub => {
        if (dialog.open) unsubscribe = unsub;
        else unsub();
      }).catch(() => {});
      dialog.addEventListener('close', () => { unsubscribe?.(); dialog.remove(); }, { once: true });
      document.body.append(dialog);
      dialog.showModal();
      event.preventDefault();
      event.stopImmediatePropagation();
    } catch (_) {
      dialog?.remove();
    }
  }, true);
}
registerHaierDetails();
