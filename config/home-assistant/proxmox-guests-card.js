function mediaStorageModel(value) {
  if (!value || value.guest_id !== 104) return null;
  const keys = ['total_bytes', 'used_bytes', 'free_bytes', 'reserved_bytes', 'used_percent'];
  if (!keys.every(k => Number.isFinite(value[k]) && value[k] >= 0)
      || value.total_bytes <= 0 || value.used_percent > 100
      || Math.abs(value.total_bytes - value.used_bytes - value.free_bytes - value.reserved_bytes) > 1) return null;
  return Object.fromEntries(keys.map(k => [k, value[k]]));
}

function storageSize(bytes) {
  return `${new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1}).format(bytes / 1e9)} ГБ`;
}

function guestCardModel(entity, type) {
  const available = Boolean(entity && !['unknown', 'unavailable'].includes(entity.state)
    && Array.isArray(entity.attributes?.guests));
  const services = {
    'domain-router': ['Domain Router', 'mdi:routes'],
    media: ['Jellyfin', 'mdi:play-box-multiple'],
    torrserver: ['TorrServer', 'mdi:download-network'],
    'adguard-home': ['AdGuard Home', 'mdi:shield-check'],
    'tailscale-gateway': ['Tailscale Gateway', 'mdi:lan-connect'],
    vaultwarden: ['Vaultwarden', 'mdi:shield-key'],
    'content-factory': ['Content Factory', 'mdi:ubuntu'],
    'home-assistant': ['Home Assistant', 'mdi:home-assistant'],
    meshtastic: ['Meshtastic', 'mdi:radio-handheld'],
  };
  const labels = {
    running: ['Работает', 'mdi:check-circle-outline'],
    stopped: [type === 'vm' ? 'Остановлена' : 'Остановлен', 'mdi:stop-circle-outline'],
    paused: ['Пауза', 'mdi:pause-circle-outline'],
    unknown: ['Неизвестно', 'mdi:help-circle-outline'],
  };
  const rows = available ? entity.attributes.guests
    .filter(g => g && g.type === type && Number.isInteger(g.id))
    .sort((a, b) => a.id - b.id)
    .map(g => {
      const hostname = String(g.name || `guest-${g.id}`);
      const key = hostname.toLowerCase();
      const [name, icon] = Object.hasOwn(services, key) ? services[key]
        : [hostname, type === 'vm' ? 'mdi:monitor' : 'mdi:cube-outline'];
      const status = Object.hasOwn(labels, g.status) ? g.status : 'unknown';
      return {id: g.id, hostname, name, icon, status, label: labels[status][0], statusIcon: labels[status][1],
        storage: type === 'lxc' && g.id === 104 ? mediaStorageModel(entity.attributes.media_storage) : null};
    }) : [];
  return {available, rows, running: rows.filter(g => g.status === 'running').length};
}

const GUEST_CARD_CSS = `
  :host { display:block; min-width:0; letter-spacing:0; }
  ha-card { overflow:hidden; color:var(--primary-text-color); }
  header { padding:20px 20px 16px; border-bottom:1px solid var(--divider-color); }
  .heading { display:flex; align-items:center; gap:10px; min-width:0; }
  .heading ha-icon { --mdc-icon-size:22px; color:var(--primary-color); flex:none; }
  h2 { margin:0; font-size:20px; line-height:28px; font-weight:500; overflow-wrap:anywhere; }
  .summary { margin:5px 0 0 32px; color:var(--secondary-text-color); font-size:12px; line-height:18px; }
  ul { margin:0; padding:0; list-style:none; }
  li { display:grid; grid-template-columns:32px minmax(0,1fr) auto; align-items:center;
    column-gap:12px; min-height:68px; box-sizing:border-box; padding:12px 20px; }
  li + li { border-top:1px solid var(--divider-color); }
  .service-icon { --mdc-icon-size:24px; color:var(--secondary-text-color); }
  .identity { min-width:0; }
  .name { font-size:14px; line-height:21px; font-weight:500; overflow-wrap:anywhere; }
  .detail { color:var(--secondary-text-color); font-size:11px; line-height:17px; overflow-wrap:anywhere; }
  .status { display:flex; align-items:center; justify-content:flex-end; gap:5px;
    font-size:12px; line-height:20px; white-space:nowrap; }
  .status ha-icon { --mdc-icon-size:16px; flex:none; }
  .running { color:var(--success-color,#4caf50); }
  .stopped { color:var(--secondary-text-color); }
  .paused, .unknown { color:var(--warning-color,#ff9800); }
  .empty { margin:0; padding:24px 20px; color:var(--secondary-text-color); font-size:14px; }
  .storage { grid-column:2 / -1; margin-top:12px; min-width:0; }
  .storage-heading, .storage-values { display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; font-size:11px; line-height:18px; }
  .storage-heading { color:var(--secondary-text-color); margin-bottom:7px; }
  .storage-percent { color:var(--primary-text-color); font-variant-numeric:tabular-nums; }
  .storage-track { height:7px; border-radius:8px; overflow:hidden; background:var(--divider-color); }
  .storage-fill { height:100%; border-radius:8px; background:linear-gradient(90deg,#399c80,#68dcae); }
  .storage-fill.warning { background:var(--warning-color,#ff9800); }
  .storage-values { margin-top:7px; font-variant-numeric:tabular-nums; }
  .storage-note { font-size:10px; line-height:16px; color:var(--secondary-text-color); margin-top:3px; }
  @media (max-width:420px) {
    header { padding:18px 16px 14px; }
    li { padding:12px 16px; column-gap:10px; grid-template-columns:28px minmax(0,1fr) auto; }
  }
`;

class ProxmoxGuestsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: 'open'});
    const style = document.createElement('style');
    style.textContent = GUEST_CARD_CSS;
    this.card = document.createElement('ha-card');
    this.shadowRoot.append(style, this.card);
  }

  setConfig(config) {
    if (!config.entity || !['lxc', 'vm'].includes(config.guest_type)) {
      throw new Error('Укажите entity и guest_type: lxc или vm');
    }
    this.config = {...config};
    this.signature = null;
    this.render();
  }

  set hass(hass) {
    this.states = hass.states;
    this.render();
  }

  connectedCallback() {
    const event = new CustomEvent('context-request', {bubbles: true, composed: true});
    event.context = 'states';
    event.subscribe = true;
    event.callback = (states, unsubscribe) => {
      this.unsubscribe = unsubscribe;
      this.states = states;
      this.render();
    };
    this.dispatchEvent(event);
  }

  disconnectedCallback() {
    this.unsubscribe?.();
    this.unsubscribe = undefined;
  }

  getCardSize() {
    return 2 + (this.model?.rows.length || 1) * 1.4;
  }

  render() {
    if (!this.config) return;
    const model = guestCardModel(this.states?.[this.config.entity], this.config.guest_type);
    const signature = JSON.stringify(model);
    if (signature === this.signature) return;
    this.signature = signature;
    this.model = model;
    const make = (tag, className, text) => {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined) element.textContent = text;
      return element;
    };
    const icon = (name, className) => {
      const element = make('ha-icon', className);
      element.setAttribute('icon', name);
      element.setAttribute('aria-hidden', 'true');
      return element;
    };
    const header = make('header');
    const heading = make('div', 'heading');
    const vm = this.config.guest_type === 'vm';
    heading.append(icon(vm ? 'mdi:monitor-multiple' : 'mdi:cube-outline'),
      make('h2', '', this.config.title || (vm ? 'Виртуальные машины' : 'Контейнеры')));
    header.append(heading, make('div', 'summary', model.available
      ? `${model.running} из ${model.rows.length} работают` : 'Нет связи с мониторингом'));
    const list = make('ul');
    for (const guest of model.rows) {
      const row = make('li');
      const identity = make('div', 'identity');
      identity.append(make('div', 'name', guest.name),
        make('div', 'detail', `${vm ? 'VM' : 'LXC'} ${guest.id} · ${guest.hostname}`));
      const status = make('div', `status ${guest.status}`);
      status.append(icon(guest.statusIcon), make('span', '', guest.label));
      row.append(icon(guest.icon, 'service-icon'), identity, status);
      list.append(row);
    }
    this.card.replaceChildren(header, model.rows.length ? list : make('p', 'empty',
      model.available ? 'Нет узлов' : 'Данные недоступны'));
  }
}

if (!customElements.get('proxmox-guests-card')) {
  customElements.define('proxmox-guests-card', ProxmoxGuestsCard);
}

class ProxmoxStorageCard extends ProxmoxGuestsCard {
  setConfig(config) {
    if (!config.entity) throw new Error('Укажите entity');
    this.config = {...config}; this.signature = null; this.render();
  }
  getCardSize() { return 4; }
  render() {
    if (!this.config) return;
    const entity = this.states?.[this.config.entity];
    const disk = entity && !['unknown','unavailable'].includes(entity.state)
      ? mediaStorageModel(entity.attributes?.media_storage) : null;
    const signature = JSON.stringify(disk);
    if (signature === this.signature) return;
    this.signature = signature;
    const make = (tag, cls, text) => {
      const el = document.createElement(tag);
      if (cls) el.className = cls;
      if (text !== undefined) el.textContent = text;
      return el;
    };
    const style = make('style', '', `
      .body { padding:20px; }
      .overview { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:12px; }
      .caption { color:var(--secondary-text-color); font-size:13px; }
      .percent { font-size:30px; line-height:36px; font-weight:600; font-variant-numeric:tabular-nums; }
      .storage-track { height:10px; }
      .metrics { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:20px 0 16px; }
      .metric { min-width:0; }
      .metric + .metric { border-left:1px solid var(--divider-color); padding-left:20px; }
      .value { font-size:23px; font-weight:500; line-height:32px; font-variant-numeric:tabular-nums; }
      .free .value { color:#68dcae; }
      .foot { border-top:1px solid var(--divider-color); padding-top:12px; display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; font-size:11px; color:var(--secondary-text-color); }
    `);
    const header = make('header'); const heading = make('div','heading');
    const icon = make('ha-icon'); icon.setAttribute('icon','mdi:harddisk');
    heading.append(icon,make('h2','','Медиатека'));
    header.append(heading,make('div','summary','SSD · Jellyfin'));
    const body = make('div','body');
    if (disk) {
      const overview = make('div','overview');
      overview.append(make('span','caption','Заполнено'),make('span','percent',new Intl.NumberFormat('ru-RU').format(disk.used_percent)+'%'));
      const track = make('div','storage-track');
      for (const [key,value] of Object.entries({role:'progressbar','aria-label':'Занято на SSD медиатеки','aria-valuemin':'0','aria-valuemax':'100','aria-valuenow':String(disk.used_percent)})) track.setAttribute(key,value);
      track.setAttribute('title','Доля занятого места без резерва файловой системы, как в df.');
      const fill = make('div','storage-fill'+(disk.used_percent>=90?' warning':''));
      fill.setAttribute('style',`width:${disk.used_percent}%`); track.append(fill);
      const metrics = make('div','metrics');
      for (const [label,bytes,cls] of [['Занято',disk.used_bytes,''],['Свободно',disk.free_bytes,'free']]) {
        const metric = make('div','metric '+cls);
        metric.append(make('div','caption',label),make('div','value',storageSize(bytes)));metrics.append(metric);
      }
      const foot=make('div','foot');
      foot.append(make('span','','Всего '+storageSize(disk.total_bytes)),make('span','','Резерв ФС '+storageSize(disk.reserved_bytes)));
      body.append(overview,track,metrics,foot);
    } else body.append(make('div','caption','Нет данных о диске'));
    this.card.replaceChildren(style,header,body);
  }
}
if (!customElements.get('proxmox-storage-card')) customElements.define('proxmox-storage-card',ProxmoxStorageCard);
