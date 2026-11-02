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
        machine: bySuffix('haier_evo', 'machine'),
        status: bySuffix('haier_evo', 'machine'), phase: bySuffix('sensor', 'phase'),
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
    const ids = ['machine', 'program', 'remote', 'status', 'phase', 'current_program', 'duration'];
    const signature = JSON.stringify(ids.map(key => {
      const state = hass.states[this.entities[key]];
      return [state?.state, state?.attributes?.options, state?.attributes?.available_actions, state?.attributes?.program_catalog, state?.attributes?.program_settings, state?.attributes?.active_program_details];
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
    if (['start', 'pause', 'resume', 'cancel'].includes(key)) {
      return !!this.state('machine')?.attributes?.available_actions?.includes(key);
    }
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
    const focused = this.shadowRoot.activeElement;
    const searchFocused = focused?.id === 'program-search';
    const focusedSetting = focused?.dataset?.settingKey;
    const focusedDraft = focusedSetting ? focused.value : null;
    const focusedCommitted = focusedSetting ? focused.dataset.committedValue : null;
    const focusedProgram = focusedSetting ? focused.dataset.program : null;
    let settingFocusTarget = null;
    const searchStart = searchFocused ? focused.selectionStart : null;
    const searchEnd = searchFocused ? focused.selectionEnd : null;
    const listScroll = this.shadowRoot.getElementById('program-list')?.scrollTop || 0;
    const settingsOpen = !!this.shadowRoot.getElementById('program-settings')?.open;
    const c = { ...this.entities, ...this.config };
    const reportedStatus = this.value('status');
    const status = reportedStatus === 'not_reported' ? null : reportedStatus;
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
    if (!active) this.detailsOpen = false;
    if (status === 'running') this.motionSince ??= Date.now();
    else this.motionSince = null;
    const motionOffset = this.motionSince === null ? 0 : (Date.now() - this.motionSince) / 1000;
    const actualDetails = this.state('machine')?.attributes?.active_program_details;
    const rawRemaining = actualDetails?.remaining_minutes ?? this.value('duration');
    const numeric = value => typeof value === 'number' || (typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value.trim())) ? Number(value) : NaN;
    const remainingMinutes = numeric(rawRemaining);
    const totalMinutes = numeric(actualDetails?.total_minutes);
    const progress = active && Number.isFinite(remainingMinutes) && remainingMinutes >= 0 && Number.isFinite(totalMinutes) && totalMinutes > remainingMinutes ? (totalMinutes - remainingMinutes) / totalMinutes * 100 : null;
    const uncertain = !status;
    const canStart = connected && selectedProgram && this.usable('start') && (status === 'idle' || uncertain) && !this.pending;
    const action = status === 'running' ? 'pause' : status === 'paused' ? 'resume' : null;
    const canToggle = connected && action && this.usable(action) && !this.pending;
    const canCancel = connected && (active || uncertain) && this.usable('cancel') && !this.pending;
    const canChooseToggle = connected && uncertain && !this.pending && (this.usable('pause') || this.usable('resume'));
    const isDryer = c.kind === 'dryer';
    const device = this._hass.devices?.[c.device];
    if (!connected || active || this.pending) this.programMenuOpen = false;
    const startButton = `<button class="${active ? 'destructive' : 'primary'}" id="start" aria-controls="start-choices" aria-expanded="false"><ha-icon id="start-icon"></ha-icon><span id="start-label"></span></button>`;
    const toggleButton = `<button class="${active ? 'primary' : ''}" id="toggle" aria-controls="toggle-choices" aria-expanded="false"><ha-icon id="toggle-icon"></ha-icon><span id="toggle-label"></span></button>`;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, inherit); }
        ha-card { padding: 22px; overflow: hidden; }
        h2, p { margin: 0; }
        .heading { position: relative; display: flex; gap: 12px; align-items: center; padding-left: 44px; }
        .heading .back { position: absolute; left: 0; top: 0; width: 40px; min-height: 40px; padding: 0; background: transparent; }
        .heading > div { min-width: 0; }
        .heading ha-icon { color: var(--primary-color); --mdc-icon-size: 25px; }
        .heading .remote-button { margin-left: auto; flex-shrink: 0; padding: 0; width: 44px; }
        .remote-info { margin-top: 10px; padding: 12px; border-radius: 9px; background: var(--secondary-background-color); font-size: 13px; line-height: 1.45; }
        [hidden] { display: none !important; }
        h2 { font-size: 19px; font-weight: 600; line-height: 1.3; overflow-wrap: anywhere; }
        .connection { margin-top: 3px; color: var(--secondary-text-color); font-size: 13px; }
        .visual { display: flex; min-height: 112px; align-items: center; justify-content: center; gap: 24px; margin: 16px 0; }
        .drum { position: relative; width: 112px; height: 112px; border-radius: 50%; background: var(--secondary-background-color); display: grid; place-items: center; flex-shrink: 0; }
        .drum svg { width: 100%; height: 100%; overflow: visible; }
        .machine-body { fill: var(--card-background-color, var(--primary-background-color)); stroke: var(--secondary-text-color); stroke-width: 1.5; }
        .machine-glass { fill: var(--secondary-background-color); stroke: var(--secondary-text-color); stroke-width: 1.3; }
        .machine-marks { fill: var(--secondary-text-color); }
        .laundry { transform-origin: 56px 59px; }
        .laundry path:first-child { fill: var(--primary-color); opacity: .8; }
        .laundry path:last-child { fill: var(--secondary-text-color); opacity: .65; }
        .water { fill: var(--primary-color); opacity: .22; }
        .bubbles { fill: var(--primary-color); opacity: .65; }
        .heat { fill: none; stroke: var(--primary-color); stroke-width: 1.5; stroke-linecap: round; opacity: .8; }
        .meter-track { fill: none; stroke: var(--divider-color); stroke-width: 1.5; }
        .meter-value { fill: none; stroke: var(--primary-color); stroke-width: 2.5; stroke-linecap: round; }
        .drum.running .laundry { animation: tumble 7s ease-in-out infinite; animation-delay: var(--motion-delay); }
        .drum.running .bubbles, .drum.running .heat { animation: drift 3s ease-in-out infinite; animation-delay: var(--motion-delay); }
        @keyframes tumble { 0% { transform: rotate(0); } 40%, 52% { transform: rotate(180deg); } 92%, 100% { transform: rotate(360deg); } }
        @keyframes drift { 0%, 100% { transform: translateY(2px); opacity: .35; } 50% { transform: translateY(-3px); opacity: .8; } }
        @media (prefers-reduced-motion: reduce) { .drum.running .laundry, .drum.running .bubbles, .drum.running .heat { animation: none; } }
        .summary { min-width: 0; max-width: 250px; }
        .status { font-size: 21px; font-weight: 600; line-height: 1.25; overflow-wrap: anywhere; }
        .phase, .current { margin-top: 7px; font-size: 14px; color: var(--secondary-text-color); overflow-wrap: anywhere; }
        .fields { display: grid; gap: 8px; }
        label { color: var(--secondary-text-color); font-size: 13px; }
        .program-trigger { box-sizing: border-box; width: 100%; min-height: 48px; padding: 0 12px; border: 1px solid var(--divider-color); border-radius: 9px; background: var(--secondary-background-color); color: var(--primary-text-color); font: inherit; justify-content: flex-start; text-align: left; }
        .program-options { border-radius: 9px; background: var(--secondary-background-color); }
        .search-row { display: flex; align-items: center; gap: 8px; padding: 8px; }
        .search-row input { box-sizing: border-box; width: 100%; min-width: 0; min-height: 44px; padding: 0 10px; border: 1px solid var(--divider-color); border-radius: 9px; background: var(--card-background-color, var(--primary-background-color)); color: var(--primary-text-color); font: inherit; }
        .search-row input:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
        .search-row button { flex-shrink: 0; }
        .search-row button[hidden], .program-option[hidden] { display: none; }
        .program-list { max-height: 300px; overflow-y: auto; }
        .program-group { padding-top: 8px; }
        .program-group-title { margin: 0; padding: 6px 14px 2px; font-size: 15px; font-weight: 600; }
        .program-group-description { padding: 0 14px 8px; color: var(--secondary-text-color); font-size: 12px; line-height: 1.4; }
        .program-option { width: 100%; justify-content: flex-start; align-items: flex-start; text-align: left; border-radius: 0; padding: 10px 14px; }
        .program-copy { display: grid; gap: 3px; min-width: 0; }
        .program-duration, .program-description { color: var(--secondary-text-color); font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
        .program-empty { padding: 12px 16px; color: var(--secondary-text-color); font-size: 14px; }
        .program-settings { margin-top: 16px; }
        .program-settings summary { cursor: pointer; min-height: 44px; display: flex; align-items: center; gap: 8px; font-weight: 600; }
        .program-settings summary:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
        .settings-chevron { margin-left: auto; }
        .program-settings[open] .settings-chevron { transform: rotate(180deg); }
        .settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 8px; }
        .setting-field { min-width: 0; display: grid; gap: 5px; align-content: start; }
        .setting-field label { display: flex; gap: 5px; align-items: center; color: var(--primary-text-color); overflow-wrap: anywhere; }
        .setting-field label span { min-width: 0; overflow-wrap: anywhere; }
        .setting-field ha-icon { --mdc-icon-size: 18px; flex-shrink: 0; }
        .setting-field select, .setting-field input { box-sizing: border-box; width: 100%; min-width: 0; min-height: 44px; padding: 0 8px; border: 1px solid var(--divider-color); border-radius: 9px; background: var(--secondary-background-color); color: var(--primary-text-color); font: inherit; }
        .setting-field select:focus-visible, .setting-field input:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
        .setting-field select:disabled, .setting-field input:disabled { opacity: .6; }
        .setting-hint { color: var(--secondary-text-color); font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
        .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
        .actions[hidden] { display: none; }
        .actions.main-actions > .primary { flex: 1 1 auto; }
        .secondary-actions { display: flex; gap: 8px; margin-top: 12px; }
        .secondary-actions > button { flex: 1 1 0; min-width: 0; padding: 6px 8px; line-height: 1.25; text-align: center; }
        .secondary-actions > button span { min-width: 0; overflow-wrap: anywhere; }
        #details-view { margin-top: 14px; }
        #details-title { margin: 14px 0 0; }
        button { min-height: 44px; border: 0; border-radius: 9px; padding: 0 14px; background: var(--secondary-background-color); color: var(--primary-text-color); font: inherit; cursor: pointer; display: inline-flex; gap: 7px; align-items: center; justify-content: center; }
        button.primary { background: var(--primary-color); color: var(--text-primary-color, white); }
        button.destructive { color: var(--error-color); }
        button ha-icon { --mdc-icon-size: 19px; }
        button:disabled { opacity: .48; cursor: not-allowed; }
        button:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
        .note { margin-top: 13px; font-size: 13px; line-height: 1.4; color: var(--secondary-text-color); }
        .error { margin-top: 10px; color: var(--error-color); font-size: 13px; }
        @media (max-width: 380px) { ha-card { padding: 17px; } .visual { gap: 16px; } .drum { width: 88px; height: 88px; } .settings-grid:not(#active-fields) { grid-template-columns: 1fr; } }
      </style>
      <ha-card>
        <div class="heading"><button type="button" class="back" id="details-back" aria-label="Назад" hidden><ha-icon icon="mdi:arrow-left"></ha-icon></button><div><h2 id="name"></h2><p class="connection" id="connection"></p></div><button type="button" class="remote-button" id="remote-info-button" aria-controls="remote-info" aria-expanded="false"><ha-icon icon="mdi:access-point"></ha-icon></button></div>
        <p class="remote-info" id="remote-info" hidden>Включение и выключение дистанционного управления выполняется на панели машины.</p>
        <div id="main-view"><div class="visual"><div class="drum" id="drum" aria-hidden="true" style="--motion-delay:-${motionOffset}s"><svg viewBox="0 0 112 112"><circle class="meter-track" cx="56" cy="56" r="53"/><circle class="meter-value" id="meter" cx="56" cy="56" r="53" pathLength="100" transform="rotate(-90 56 56)" hidden/><rect class="machine-body" x="29" y="17" width="54" height="78" rx="7"/><path class="machine-body" d="M29 32h54"/><circle class="machine-marks" cx="36" cy="24.5" r="1.7"/><circle class="machine-marks" cx="42" cy="24.5" r="1.7"/><circle class="machine-glass" cx="56" cy="59" r="21"/><clipPath id="drum-clip"><circle cx="56" cy="59" r="18"/></clipPath><g clip-path="url(#drum-clip)"><g class="laundry"><path d="M43 49q8-5 14 1t-1 13q-7 5-14-1t1-13Z"/><path d="M60 58q8-4 10 4t-5 9q-7-1-5-13Z"/></g><path class="water" id="water" d="M34 65q6-3 12 0t12 0t12 0t12 0v18H34Z"/><g class="bubbles" id="bubbles"><circle cx="47" cy="53" r="1.4"/><circle cx="65" cy="48" r="1.2"/><circle cx="70" cy="58" r="1.5"/></g></g><g class="heat" id="heat"><path d="M47 87q-3-3 0-6t0-6"/><path d="M56 87q-3-3 0-6t0-6"/><path d="M65 87q-3-3 0-6t0-6"/></g></svg></div><div class="summary"><p class="status" id="status"></p><p class="phase" id="phase"></p><p class="current" id="current"></p><p class="current" id="remaining"></p><p class="current" id="completion"></p></div></div>
        <div class="fields" id="program-fields"><label>Выбранная программа</label><p class="current" id="selected-name"></p><button type="button" class="program-trigger" id="program" aria-controls="program-options" aria-expanded="false"><ha-icon id="selected-icon"></ha-icon><span>Все программы</span></button><div class="program-options" id="program-options" hidden><div class="search-row"><input id="program-search" type="search" aria-label="Поиск программы" placeholder="Найти программу" autocomplete="off"><button type="button" id="clear-search" aria-label="Очистить поиск">Очистить</button></div><div class="program-list" id="program-list"></div><p class="program-empty" id="program-empty" role="status" aria-live="polite" hidden></p></div></div>
        <details class="program-settings" id="program-settings" hidden><summary><ha-icon icon="mdi:tune-variant"></ha-icon>Параметры программы<ha-icon class="settings-chevron" icon="mdi:chevron-down"></ha-icon></summary><p class="setting-hint" id="settings-error" role="status" hidden></p><div class="settings-grid" id="settings-grid"></div></details>
        <div class="actions main-actions">${active ? toggleButton + startButton : startButton + toggleButton}</div>
        <div class="actions" id="start-choices" hidden><button id="choose-start">Запустить программу</button><button id="cancel">Отменить программу</button></div>
        <div class="actions" id="toggle-choices" hidden><button id="choose-pause">Пауза</button><button id="choose-resume">Продолжить</button>
        </div>
        <p class="note" id="note"></p><p class="error" id="error" role="alert"></p>
        <div class="secondary-actions"><button type="button" id="show-details" hidden><ha-icon icon="mdi:information-outline"></ha-icon><span>Детали программы</span></button><button type="button" id="native"><ha-icon icon="mdi:history"></ha-icon><span>История и настройки</span></button></div></div>
        <section id="details-view" hidden aria-labelledby="details-title"><h3 id="details-title">Детали программы</h3><p class="current" id="active-program"></p><div class="settings-grid" id="active-fields"></div><p class="note" id="active-empty" hidden>Машина не передала параметры текущей программы.</p></section>
      </ha-card>`;
    const $ = id => this.shadowRoot.getElementById(id);
    $('name').textContent = c.name || device?.name_by_user || device?.name || (isDryer ? 'Сушильная машина' : 'Стиральная машина');
    $('drum').classList.toggle('running', status === 'running');
    if (isDryer) { $('water').setAttribute('hidden', ''); $('bubbles').setAttribute('hidden', ''); }
    else $('heat').setAttribute('hidden', '');
    if (progress !== null) {
      $('meter').removeAttribute('hidden');
      $('meter').setAttribute('stroke-dasharray', `${progress} 100`);
    }
    $('connection').textContent = remote === 'on' ? 'Дистанционное управление включено' : remote === 'off' ? 'Дистанционное управление выключено' : 'Состояние дистанционного управления неизвестно';
    $('remote-info-button').setAttribute('aria-label', $('connection').textContent);
    $('remote-info-button').setAttribute('aria-expanded', String(!!this.remoteInfoOpen));
    $('remote-info').hidden = !this.remoteInfoOpen;
    $('remote-info-button').addEventListener('click', () => {
      this.remoteInfoOpen = !this.remoteInfoOpen;
      $('remote-info').hidden = !this.remoteInfoOpen;
      $('remote-info-button').setAttribute('aria-expanded', String(this.remoteInfoOpen));
    });
    $('status').textContent = remote === null ? 'Нет данных об устройстве' : statusText;
    const phase = this.value('phase');
    $('phase').textContent = remote !== null && phase && !/^(Код|Code)\s/.test(phase) && phase !== statusText && !(['idle', 'running', 'paused'].includes(status) && phase === 'Режим ожидания') ? phase : '';
    const current = this.value('current_program');
    const actualName = typeof actualDetails?.program === 'string' && actualDetails.program.trim() ? actualDetails.program : (current === 'Нет активной программы' ? '' : current);
    $('current').textContent = active ? actualName || '' : '';
    const minutes = remainingMinutes;
    if (active && Number.isFinite(minutes) && minutes >= 0 && minutes <= 10080) {
      const rounded = Math.round(minutes);
      $('remaining').textContent = `Осталось ${String(Math.floor(rounded / 60)).padStart(2, '0')}:${String(rounded % 60).padStart(2, '0')}`;
      if (status === 'running') {
        const end = new Date(Date.now() + rounded * 60000);
        $('completion').textContent = `Примерно до ${end.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
      }
    }
    $('main-view').hidden = !!this.detailsOpen;
    $('details-view').hidden = !this.detailsOpen;
    $('details-back').hidden = !this.detailsOpen;
    $('show-details').hidden = !active;
    $('program-fields').hidden = active;
    $('show-details').addEventListener('click', () => {
      this.detailsOpen = true;
      this.render();
      this.shadowRoot.getElementById('details-back').focus();
    });
    $('details-back').addEventListener('click', () => {
      this.detailsOpen = false;
      this.render();
      this.shadowRoot.getElementById('show-details').focus();
    });
    $('active-program').textContent = actualName || 'Название программы не передано';
    const activeFields = Array.isArray(actualDetails?.fields) ? actualDetails.fields : [];
    for (const field of activeFields) {
      if (!field || typeof field.title !== 'string' || typeof field.value !== 'string' || !field.value.trim()) continue;
      const item = document.createElement('div');
      const label = document.createElement('label');
      const icon = document.createElement('ha-icon');
      const title = document.createElement('span');
      const value = document.createElement('p');
      const description = document.createElement('p');
      item.className = 'setting-field';
      icon.setAttribute('icon', typeof field.icon === 'string' && /^mdi:[a-z0-9-]+$/.test(field.icon) ? field.icon : this.settingIcon(field.title));
      title.textContent = field.title;
      value.textContent = field.value;
      description.className = 'setting-hint';
      description.textContent = typeof field.description === 'string' ? field.description : '';
      label.append(icon, title);
      item.append(label, value, description);
      $('active-fields').append(item);
    }
    $('active-empty').hidden = !!$('active-fields').children.length;
    const select = $('program');
    $('selected-icon').setAttribute('icon', this.programIcon(selected));
    $('selected-name').textContent = selected || 'Нет доступных программ';
    select.setAttribute('aria-label', `Все программы. Выбрана: ${selected || 'нет доступных программ'}`);
    select.setAttribute('aria-expanded', String(!!this.programMenuOpen));
    $('program-options').hidden = !this.programMenuOpen;
    const search = $('program-search');
    search.value = this.programQuery || '';
    this.programEntries = [];
    this.programGroups = [];
    const addProgram = (program, container, group = null) => {
      const element = document.createElement('button');
      const icon = document.createElement('ha-icon');
      const copy = document.createElement('span');
      const label = document.createElement('span');
      element.type = 'button';
      element.className = 'program-option';
      element.dataset.option = program.option;
      icon.setAttribute('icon', this.programIcon(program.name));
      copy.className = 'program-copy';
      label.textContent = program.name;
      copy.append(label);
      if (typeof program.duration === 'string' && program.duration.trim()) {
        const duration = document.createElement('span');
        duration.className = 'program-duration';
        duration.textContent = program.duration;
        copy.append(duration);
      }
      if (typeof program.description === 'string' && program.description.trim()) {
        const description = document.createElement('span');
        description.className = 'program-description';
        description.textContent = program.description;
        copy.append(description);
      }
      element.append(icon, copy);
      element.addEventListener('click', () => {
        this.programMenuOpen = false;
        this.programQuery = '';
        this.send('select', 'select_option', { entity_id: c.program, option: program.option });
      });
      container.append(element);
      const entry = { element, search: [program.name, program.description, program.duration, group?.title, group?.description].filter(value => typeof value === 'string').join(' ') };
      this.programEntries.push(entry);
      if (group) group.entries.push(entry);
    };
    const catalog = this.state('machine')?.attributes?.program_catalog;
    const selectable = new Set(options);
    const groups = Array.isArray(catalog) ? catalog.filter(group => group && typeof group.title === 'string' && Array.isArray(group.programs)).map(group => ({
      title: group.title.trim() || 'Программы', description: typeof group.description === 'string' ? group.description : '',
      programs: group.programs.filter(program => program && typeof program.option === 'string' && typeof program.name === 'string' && selectable.has(program.option)),
    })).filter(group => group.programs.length) : [];
    if (groups.length) {
      if (unselected && selectable.has(unselected)) addProgram({ option: unselected, name: unselected }, $('program-list'));
      const represented = new Set(groups.flatMap(group => group.programs.map(program => program.option)));
      const remaining = options.filter(option => option !== unselected && !represented.has(option));
      if (remaining.length) groups.push({ title: 'Другие программы', description: '', programs: remaining.map(option => ({ option, name: option })) });
      for (const group of groups) {
        const section = document.createElement('section');
        const title = document.createElement('h3');
        group.entries = [];
        group.element = section;
        section.className = 'program-group';
        title.className = 'program-group-title';
        title.textContent = group.title;
        section.append(title);
        if (group.description.trim()) {
          const description = document.createElement('p');
          description.className = 'program-group-description';
          description.textContent = group.description;
          section.append(description);
        }
        for (const program of group.programs) addProgram(program, section, group);
        $('program-list').append(section);
        this.programGroups.push(group);
      }
    } else {
      for (const option of options) addProgram({ option, name: option }, $('program-list'));
    }
    this.filterPrograms();
    $('program-list').scrollTop = listScroll;
    search.addEventListener('input', () => {
      this.programQuery = search.value;
      $('program-list').scrollTop = 0;
      this.filterPrograms();
    });
    $('clear-search').addEventListener('click', () => {
      this.programQuery = '';
      search.value = '';
      this.filterPrograms();
      search.focus();
    });
    $('program-options').addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      this.programMenuOpen = false;
      this.programQuery = '';
      this.render();
      this.shadowRoot.getElementById('program').focus();
    });
    select.disabled = !connected || !this.usable('program') || !options.length || active || this.pending;
    select.addEventListener('click', () => {
      this.programMenuOpen = !this.programMenuOpen;
      if (!this.programMenuOpen) this.programQuery = '';
      this.render();
      this.shadowRoot.getElementById(this.programMenuOpen ? 'program-search' : 'program').focus();
    });
    const settings = this.state('machine')?.attributes?.program_settings;
    const settingsFields = settings?.program === selected && selectedProgram && Array.isArray(settings.fields) ? settings.fields : [];
    const details = $('program-settings');
    const settingsGrid = $('settings-grid');
    const keys = new Set();
    for (const field of settingsFields) {
      if (!field || typeof field.key !== 'string' || keys.has(field.key) || !['select', 'number'].includes(field.kind)) continue;
      keys.add(field.key);
      const container = document.createElement('div');
      const label = document.createElement('label');
      const icon = document.createElement('ha-icon');
      const title = document.createElement('span');
      const control = document.createElement(field.kind === 'select' ? 'select' : 'input');
      const hint = document.createElement('p');
      const id = `setting-${keys.size}`;
      const value = settings.values?.[field.key] ?? field.value;
      const options = Array.isArray(field.options) ? field.options.filter(option => option && option.value !== undefined && option.value !== null) : [];
      container.className = 'setting-field';
      label.htmlFor = id;
      icon.setAttribute('icon', this.settingIcon(field.title));
      title.textContent = typeof field.title === 'string' && field.title.trim() ? field.title : 'Параметр';
      label.append(icon, title);
      control.id = id;
      control.dataset.settingKey = field.key;
      control.dataset.committedValue = value == null ? '' : String(value);
      control.dataset.program = selected;
      if (field.key === focusedSetting && selected === focusedProgram) settingFocusTarget = control;
      control.disabled = !!field.disabled || !connected || active || this.pending || !c.machine;
      if (field.kind === 'select') {
        if (!options.length) control.disabled = true;
        for (const option of options) {
          const item = document.createElement('option');
          item.value = String(option.value);
          item.textContent = typeof option.label === 'string' && option.label.trim() ? option.label : String(option.value);
          control.append(item);
        }
        if (value !== null && value !== undefined && !options.some(option => String(option.value) === String(value))) {
          const item = document.createElement('option');
          item.value = String(value);
          item.textContent = String(value);
          control.append(item);
        }
      } else {
        control.type = 'number';
        control.required = true;
        control.step = 'any';
        for (const bound of ['min', 'max', 'step']) {
          if (field[bound] !== null && field[bound] !== undefined) control[bound] = String(field[bound]);
        }
      }
      control.value = value === null || value === undefined ? '' : String(value);
      control.addEventListener('change', () => {
        if (control.disabled || (field.kind === 'number' && !control.value) || !control.checkValidity()) {
          if (!control.disabled) control.reportValidity();
          return;
        }
        if (control.value === String(value ?? '')) return;
        this.send('haier_evo', 'configure_program', { entity_id: c.machine, program: selected, parameters: { [field.key]: control.value } });
      });
      hint.className = 'setting-hint';
      const selectedOption = options.find(option => String(option.value) === String(value));
      hint.textContent = [field.description, selectedOption?.description, field.kind === 'number' ? field.unit : '', field.disabled ? 'Недоступно для изменения' : '']
        .filter(text => typeof text === 'string' && text.trim()).join(' · ');
      if (hint.textContent) {
        hint.id = `${id}-hint`;
        control.setAttribute('aria-describedby', hint.id);
      }
      container.append(label, control, hint);
      settingsGrid.append(container);
    }
    const settingsError = settings?.program === selected && typeof settings.error === 'string' ? settings.error.trim() : '';
    $('settings-error').textContent = settingsError ? `Не удалось загрузить параметры: ${settingsError}` : '';
    $('settings-error').hidden = !settingsError;
    details.hidden = active || (!settingsGrid.children.length && !settingsError);
    details.open = settingsOpen;
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
    $('note').textContent = !connected ? 'Управление недоступно, пока нет связи или выключен удалённый доступ.' : !status ? 'Состояние цикла не получено. Выберите действие вручную.' : ['finished', 'cancelled'].includes(status) ? 'Для запуска дождитесь подтверждённого режима ожидания.' : active ? 'Отмена требует подтверждения.' : 'Запуск требует подтверждения.';
    $('error').textContent = this.error;
    const start = () => {
      if (canStart && window.confirm(`Запустить программу «${selected}»?`)) this.send('haier_evo', 'execute_action', { entity_id: c.machine, action: 'start' });
    };
    const cancel = () => {
      if (canCancel && window.confirm('Отменить текущую программу?')) this.send('haier_evo', 'execute_action', { entity_id: c.machine, action: 'cancel' });
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
      if (canToggle) this.send('haier_evo', 'execute_action', { entity_id: c.machine, action });
      else if (canChooseToggle) {
        $('toggle-choices').hidden = !$('toggle-choices').hidden;
        $('toggle').setAttribute('aria-expanded', String(!$('toggle-choices').hidden));
      }
    });
    $('choose-start').addEventListener('click', start);
    $('cancel').addEventListener('click', cancel);
    for (const choice of ['pause', 'resume']) {
      $('choose-' + choice).addEventListener('click', () => {
        if (canChooseToggle && this.usable(choice)) this.send('haier_evo', 'execute_action', { entity_id: c.machine, action: choice });
      });
    }
    $('native').addEventListener('click', () => this.dispatchEvent(new CustomEvent('haier-show-native', { bubbles: true, composed: true })));
    if (settingFocusTarget && settingsOpen && !settingFocusTarget.disabled) {
      if (settingFocusTarget.type === 'number' && settingFocusTarget.dataset.committedValue === focusedCommitted) {
        settingFocusTarget.value = focusedDraft;
      }
      settingFocusTarget.focus({ preventScroll: true });
    }
    if (focused?.id === 'details-back') $(active && this.detailsOpen ? 'details-back' : 'program').focus({ preventScroll: true });
    if (searchFocused && this.programMenuOpen) {
      search.focus({ preventScroll: true });
      if (searchStart !== null && searchEnd !== null) search.setSelectionRange(searchStart, searchEnd);
    }
    this.onViewChange?.(!!this.detailsOpen);
  }

  filterPrograms() {
    const normalize = value => String(value).toLocaleLowerCase().replace(/ё/g, 'е').trim();
    const terms = normalize(this.programQuery || '').split(/\s+/).filter(Boolean);
    let matches = 0;
    for (const entry of this.programEntries) {
      const name = normalize(entry.search);
      entry.element.hidden = !terms.every(term => name.includes(term));
      if (!entry.element.hidden) matches++;
    }
    for (const group of this.programGroups) {
      group.element.hidden = !group.entries.some(entry => !entry.element.hidden);
    }
    this.shadowRoot.getElementById('clear-search').hidden = !this.programQuery;
    const empty = this.shadowRoot.getElementById('program-empty');
    empty.hidden = matches > 0;
    empty.textContent = matches ? '' : 'Программы не найдены';
  }

  settingIcon(title = '') {
    const text = String(title).toLocaleLowerCase();
    if (/температур|temperature/.test(text)) return 'mdi:thermometer';
    if (/отжим|скорост|spin/.test(text)) return 'mdi:rotate-right';
    if (/загрязнен|soil/.test(text)) return 'mdi:tshirt-crew';
    if (/сушк|dryer/.test(text)) return 'mdi:tumble-dryer';
    return 'mdi:tune-variant';
  }

  programIcon(name = '') {
    const text = String(name).toLocaleLowerCase().split(' - ').at(-1);
    const icons = [
      [/очистк.*барабан|drum.*clean/, 'mdi:washing-machine'],
      [/анти.?аллерг|allerg/, 'mdi:shield-check'],
      [/(^|[^a-z])uv([^a-z]|$)|ультрафиолет|уф.?обработ/, 'mdi:white-balance-sunny'],
      [/освеж|refresh/, 'mdi:refresh'], [/отжим|spin/, 'mdi:rotate-right'],
      [/авто|auto/, 'mdi:autorenew'], [/смешан|mixed/, 'mdi:tshirt-crew'],
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
        entry.entity_id.startsWith('haier_evo.') && entry.unique_id?.endsWith('_machine'))
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
        dialog[data-haier-laundry] { box-sizing: border-box; width: min(520px, calc(100vw - 24px)); max-width: none; max-height: calc(100dvh - 24px); padding: 0; border: 0; border-radius: 18px; background: var(--card-background-color, var(--primary-background-color)); color: var(--primary-text-color); box-shadow: 0 16px 50px #0006; }
        dialog[data-haier-laundry]::backdrop { background: #0009; }
        dialog[data-haier-laundry] .close { position: sticky; top: 8px; z-index: 1; float: left; width: 40px; height: 40px; margin: 8px -48px -48px 8px; border: 0; border-radius: 50%; background: transparent; color: var(--primary-text-color); cursor: pointer; }
        dialog[data-haier-laundry] .close:focus-visible { outline: 3px solid var(--primary-color); outline-offset: -3px; }
        dialog[data-haier-laundry] haier-laundry-detail { display: block; }
      </style><button class="close" type="button" aria-label="Закрыть"><ha-icon icon="mdi:close"></ha-icon></button><haier-laundry-detail></haier-laundry-detail>`;
      const details = dialog.querySelector('haier-laundry-detail');
      details.onViewChange = subview => { dialog.querySelector('.close').hidden = subview; };
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
