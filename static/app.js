/*
  통학길 프론트엔드 (빌드 도구 없음, 순수 JS)
  - 서버 응답은 절대 innerHTML 로 넣지 않는다. 모든 화면은 h() 로 요소를 만들고 글자는 텍스트 노드로만 넣는다.
  - 서버는 구조화된 값만 주고, 화면 문구는 여기서 만든다 (같은 API 를 모바일 앱도 쓸 수 있게).
*/
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);

  /* ---------- DOM 헬퍼 ---------- */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') el.className = value;
        else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
        else el.setAttribute(key, value === true ? '' : String(value));
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function replace(container, ...nodes) {
    container.replaceChildren(...nodes.flat().filter(Boolean));
  }

  const message = (text, { error = false, title = '' } = {}) =>
    h('div', { class: 'message' + (error ? ' is-error' : ''), role: error ? 'alert' : 'status' },
      title ? h('strong', null, title) : null, text);

  /* ---------- API ---------- */
  const REQUEST_TIMEOUT_MS = 25000;

  async function api(path, params, signal) {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
    }
    const ctrl = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, REQUEST_TIMEOUT_MS);
    const onAbort = () => ctrl.abort();
    if (signal) {
      if (signal.aborted) ctrl.abort();
      else signal.addEventListener('abort', onAbort, { once: true });
    }

    let res;
    try {
      res = await fetch(url, { signal: ctrl.signal, headers: { Accept: 'application/json' } });
    } catch (err) {
      if (timedOut) throw new Error('응답이 너무 오래 걸려요. 잠시 후 다시 시도해 주세요.');
      if (err && err.name === 'AbortError') throw err;
      throw new Error('서버에 연결하지 못했어요. 네트워크를 확인하고 다시 시도해 주세요.');
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    }

    let body = null;
    try { body = await res.json(); } catch (_) { /* JSON 이 아닌 응답 */ }
    if (!res.ok) {
      const text = body && body.error && body.error.message;
      const error = new Error(text || '요청에 실패했어요 (HTTP ' + res.status + ').');
      error.status = res.status;
      throw error;
    }
    return body;
  }

  /* 화면(탭)마다 진행 중인 요청을 하나만 유지한다. 새 요청이 시작되면 이전 요청은 취소. */
  const inflight = {};
  function begin(key) {
    if (inflight[key]) inflight[key].abort();
    inflight[key] = new AbortController();
    return inflight[key].signal;
  }
  const isAbort = (err) => err && err.name === 'AbortError';

  /* ---------- 서식 ---------- */
  const KST = 'Asia/Seoul';

  function fmtClockTime(iso) {
    const d = iso ? new Date(iso) : new Date();
    if (Number.isNaN(d.getTime())) return '';
    return new Intl.DateTimeFormat('ko-KR', { timeZone: KST, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d);
  }

  function fmtDistance(m) {
    if (m === null || m === undefined) return '';
    return m >= 1000 ? (m / 1000).toFixed(1) + 'km' : m + 'm';
  }

  const minutesLabel = (n) => (n === 0 ? '곧' : n + '분');

  /* ---------- 공용 조각: 전광판 ---------- */
  function boardRow(ledText, desc, subdesc, { word = false } = {}) {
    return h('div', { class: 'board-row' },
      h('span', { class: 'led' + (word ? ' led-word' : '') }, ledText),
      h('span', { class: 'board-desc' }, desc, subdesc ? h('small', null, subdesc) : null));
  }

  function board({ title, sub, rows, notes, inline = false, label }) {
    return h('div', { class: 'board' + (inline ? ' is-inline' : ''), role: 'group', 'aria-label': label || title || '도착 정보' },
      title ? h('div', { class: 'board-head' }, h('h3', null, title), sub ? h('p', null, sub) : null) : null,
      rows,
      (notes || []).map((t) => h('p', { class: 'board-note' }, t)));
  }

  function lineClass(name) {
    const n = String(name || '').replace(/\s/g, '');
    if (n.includes('인천1')) return 'line-inc1';
    if (n.includes('인천2')) return 'line-inc2';
    return '';
  }

  function timingText(t) {
    if (!t) return '';
    const parts = [];
    if (t.first && t.last) parts.push('첫차 ' + t.first + ', 막차 ' + t.last);
    if (t.interval_min) parts.push('배차간격 약 ' + t.interval_min + '분');
    return parts.join(', ');
  }

  /* 지하철 열차 목록 → 전광판 행들 */
  function trainRows(trains, terminus) {
    return trains.map((t) => boardRow(t.time, t.remain_min === 0 ? '곧 출발' : '약 ' + t.remain_min + '분 후', terminus ? terminus + '행' : ''));
  }

  /* ---------- 탭 ---------- */
  const TABS = ['route', 'subway', 'bus', 'timetable'];
  let activeTab = 'route';
  const loadedAt = {};
  const STALE_MS = 20000;

  function activate(name, { focus = false, updateHash = true } = {}) {
    if (!TABS.includes(name)) name = 'route';
    activeTab = name;
    for (const tab of TABS) {
      const selected = tab === name;
      const button = $('#tab-' + tab);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      $('#panel-' + tab).hidden = !selected;
      if (selected && focus) button.focus();
    }
    if (updateHash) history.replaceState(null, '', '#' + name);
    if (name === 'subway') loadSubwayDefault();
    if (name === 'bus') loadBus();
    if (name === 'timetable') loadTimetable(currentDay);
  }

  function initTabs() {
    for (const tab of TABS) {
      const button = $('#tab-' + tab);
      button.addEventListener('click', () => activate(tab));
      button.addEventListener('keydown', (e) => {
        const i = TABS.indexOf(tab);
        let next = null;
        if (e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
        else if (e.key === 'ArrowLeft') next = TABS[(i + TABS.length - 1) % TABS.length];
        else if (e.key === 'Home') next = TABS[0];
        else if (e.key === 'End') next = TABS[TABS.length - 1];
        if (next) { e.preventDefault(); activate(next, { focus: true }); }
      });
    }
    window.addEventListener('hashchange', () => {
      const name = location.hash.replace('#', '');
      if (TABS.includes(name) && name !== activeTab) activate(name, { updateHash: false });
    });
  }

  /* ---------- 헤더 시계 (서버/기기 시간대와 무관하게 한국 시간) ---------- */
  function tickClock() {
    $('#clock').textContent = new Intl.DateTimeFormat('ko-KR', {
      timeZone: KST, month: 'long', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(new Date());
  }

  /* =====================================================================
     길찾기
     ===================================================================== */
  // start: 출발지. source 는 'geo'(현재 위치) | 'search'(자동완성 선택) | null(직접 입력한 정류장/역 이름).
  // goal: 도착지. 좌표를 선택했으면 coords, 아니면 입력한 이름 그대로 서버가 정류장/역으로 찾는다.
  const routeState = { start: { coords: null, source: null }, goal: { coords: null } };
  let lastRouteParams = null;      // 마지막으로 /api/route 에 보낸 파라미터 (세부 조회 때 index만 붙여 재사용)
  let lastCandidatesData = null;   // 마지막 약식 목록 응답 (세부 화면의 "다른 경로 보기" 버튼용)
  const startInput = $('#route-start');
  const goalInput = $('#route-goal');
  const locateStatus = $('#locate-status');
  const goalHint = $('#goal-hint');
  const routeResult = $('#route-result');

  function setLocateStatus(text, isError = false) {
    locateStatus.textContent = text;
    locateStatus.classList.toggle('is-error', isError);
  }

  function clearStartCoords() {
    routeState.start = { coords: null, source: null };
    $('#btn-swap').disabled = false;
  }

  function locate() {
    if (!('geolocation' in navigator)) {
      setLocateStatus('이 브라우저는 위치 기능을 지원하지 않아요. 역 이름을 직접 입력해 주세요.', true);
      return;
    }
    if (!window.isSecureContext) {
      setLocateStatus('위치는 HTTPS 주소에서만 쓸 수 있어요. 역 이름을 직접 입력해 주세요.', true);
      return;
    }
    const button = $('#btn-locate');
    button.disabled = true;
    setLocateStatus('위치를 확인하는 중이에요…');
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const coords = { x: pos.coords.longitude, y: pos.coords.latitude };
        routeState.start = { coords, source: 'geo' };
        startInput.value = '현재 위치';
        $('#btn-swap').disabled = true;
        setLocateStatus('주소를 확인하는 중이에요…');
        button.disabled = false;

        const signal = begin('geocode');
        try {
          const data = await api('/api/geocode/reverse', { x: coords.x.toFixed(6), y: coords.y.toFixed(6) }, signal);
          // 그 사이 위치를 다시 눌렀거나 좌표를 지웠으면 옛 결과는 반영하지 않는다.
          if (routeState.start.coords === coords) setLocateStatus('현재 위치: ' + data.address);
        } catch (err) {
          if (!isAbort(err) && routeState.start.coords === coords) {
            setLocateStatus('현재 위치는 확인했지만 정확한 주소는 찾지 못했어요. 출발지로는 그대로 쓸 수 있어요.');
          }
        }
      },
      (err) => {
        const text = err.code === 1
          ? '위치 권한이 꺼져 있어요. 브라우저 설정에서 허용하거나 역 이름을 입력해 주세요.'
          : err.code === 3
            ? '위치를 확인하는 데 시간이 너무 오래 걸려요. 역 이름을 입력해 주세요.'
            : '현재 위치를 알 수 없어요. 역 이름을 입력해 주세요.';
        setLocateStatus(text, true);
        button.disabled = false;
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
    );
  }

  /* ---------- 장소 자동완성 (출발/도착 공용) ---------- */
  function debounce(fn, wait) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function suggestionItem(place, index, activeIndex, onPick) {
    return h('li', {
      role: 'option', id: 'sugg-' + index, 'aria-selected': String(index === activeIndex),
      class: 'suggestion' + (index === activeIndex ? ' is-active' : ''),
      // mousedown(클릭보다 먼저 발생)에서 preventDefault 해야 입력창이 blur 되기 전에 선택을 처리할 수 있다.
      onmousedown: (e) => { e.preventDefault(); onPick(place); },
    },
      h('span', { class: 'suggestion-name' }, place.name),
      h('span', { class: 'suggestion-meta' }, [place.kind, place.address].filter(Boolean).join(' · ')));
  }

  /* input(검색창) + listEl(드롭다운 ul)을 자동완성으로 묶는다. onPick(place)/onTyping()은 호출부가 정의. */
  function setupAutocomplete(input, listEl, searchKey, { onPick, onTyping }) {
    let items = [];
    let activeIndex = -1;

    function close() {
      listEl.hidden = true;
      listEl.replaceChildren();
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      items = [];
      activeIndex = -1;
    }

    function renderList() {
      if (!items.length) { close(); return; }
      listEl.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      replace(listEl, items.map((p, i) => suggestionItem(p, i, activeIndex, pick)));
      if (activeIndex >= 0) input.setAttribute('aria-activedescendant', 'sugg-' + activeIndex);
      else input.removeAttribute('aria-activedescendant');
    }

    function pick(place) {
      input.value = place.name;
      close();
      onPick(place);
    }

    const cache = new Map(); // 같은 검색어를 다시 치면 API를 또 부르지 않는다 (ODsay 일일 쿼터 절약)
    const runSearch = debounce(async (q) => {
      if (cache.has(q)) {
        items = cache.get(q);
        activeIndex = -1;
        renderList();
        return;
      }
      const signal = begin(searchKey);
      try {
        const data = await api('/api/places/search', { q }, signal);
        items = data.results || [];
        cache.set(q, items);
        activeIndex = -1;
        renderList();
      } catch (err) {
        if (!isAbort(err)) close();
      }
    }, 450);

    input.addEventListener('input', () => {
      onTyping();
      const q = input.value.trim();
      if (q.length < 2) { close(); return; }
      runSearch(q);
    });

    input.addEventListener('keydown', (e) => {
      if (listEl.hidden || !items.length) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        activeIndex = (activeIndex + 1) % items.length;
        renderList();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        activeIndex = (activeIndex - 1 + items.length) % items.length;
        renderList();
      } else if (e.key === 'Enter' && activeIndex >= 0) {
        e.preventDefault();
        pick(items[activeIndex]);
      } else if (e.key === 'Escape') {
        close();
      }
    });

    input.addEventListener('blur', close);
  }

  /* 구간 하나의 실시간 표시 */
  function realtimeView(step) {
    const rt = step.realtime;
    if (!rt) return null;

    if (rt.status === 'error') {
      return h('p', { class: 'note' }, '실시간 정보를 불러오지 못했어요. 경로 안내는 그대로 볼 수 있어요.');
    }

    if (rt.kind === 'bus') {
      if (rt.status === 'ok') {
        const rows = rt.arrivals.slice(0, 2).map((a) => boardRow(
          minutesLabel(a.minutes),
          a.minutes === 0 ? '곧 도착' : a.minutes + '분 후 도착',
          a.prev_stations !== null && a.prev_stations !== undefined ? a.prev_stations + '개 정류장 전' : '',
          { word: a.minutes === 0 },
        ));
        return board({ rows, inline: true, label: (rt.route_no || '') + '번 버스 실시간 도착' });
      }
      const timing = timingText(rt.route_info);
      return board({
        inline: true, label: '버스 도착 정보',
        notes: ['지금 도착 예정인 버스가 없어요.'].concat(timing ? [timing] : []),
      });
    }

    if (rt.kind === 'subway') {
      const notes = [];
      if (rt.fallback) notes.push('오늘 운행 정보가 없어 평일 시간표 기준이에요.');
      if (rt.status === 'ok') {
        return board({ rows: trainRows(rt.trains, rt.terminus), notes, inline: true, label: '다음 열차 (시간표 기준)' });
      }
      return board({ inline: true, label: '다음 열차', notes: ['오늘 남은 열차가 없어요 (' + rt.terminus + '행).'].concat(notes) });
    }
    return null;
  }

  function renderStep(step) {
    if (step.type === 'walk') {
      return h('li', { class: 'step step-walk' },
        h('p', { class: 'step-title' }, '도보 ' + fmtDistance(step.distance_m)),
        step.minutes !== null && step.minutes !== undefined ? h('p', { class: 'step-meta' }, '약 ' + step.minutes + '분') : null);
    }
    const isBus = step.type === 'bus';
    const lc = isBus ? '' : lineClass(step.line);
    const unit = isBus ? '정류장' : '역';
    const badge = isBus
      ? h('span', { class: 'badge badge-bus' }, step.line || '버스')
      : h('span', { class: 'badge badge-line ' + lc }, step.line || '지하철');
    const count = step.station_count ? step.station_count + '개 ' + unit + ' 이동' : '';
    const time = step.minutes !== null && step.minutes !== undefined ? '약 ' + step.minutes + '분' : '';
    return h('li', { class: 'step ' + (isBus ? 'step-bus' : 'step-subway ' + lc) },
      h('p', { class: 'step-title' }, badge, h('span', null, (step.from || '?') + ' 승차')),
      h('p', { class: 'step-sub' }, (step.way ? step.way + ' 방면, ' : '') + (step.to || '?') + '에서 하차'),
      (count || time) ? h('p', { class: 'step-meta' }, [count, time].filter(Boolean).join(', ')) : null,
      realtimeView(step));
  }

  /* ---------- 경로 후보 목록 (약식) ---------- */
  function candidateLineSummary(c) {
    return c.lines.length ? c.lines.join(' → ') : '도보로만 이동';
  }

  function candidateCard(c) {
    return h('li', null, h('button', {
      class: 'route-option', type: 'button', onclick: () => loadRouteDetail(c.index),
    },
      h('p', { class: 'route-option-lines' }, candidateLineSummary(c)),
      h('dl', { class: 'route-option-stats' },
        c.total_minutes !== null && c.total_minutes !== undefined
          ? h('div', null, h('dt', null, '소요'), h('dd', null, c.total_minutes + '분')) : null,
        h('div', null, h('dt', null, '환승'), h('dd', null, (c.transfers || 0) + '회')),
        c.fare !== null && c.fare !== undefined
          ? h('div', null, h('dt', null, '요금'), h('dd', null, c.fare.toLocaleString('ko-KR') + '원')) : null,
        c.total_distance_m ? h('div', null, h('dt', null, '거리'), h('dd', null, fmtDistance(c.total_distance_m))) : null)));
  }

  function renderCandidates(data) {
    const title = h('h2', { class: 'route-title', tabindex: '-1' }, data.start.name + ' → ' + data.goal.name);
    const kinds = h('p', { class: 'route-kinds' }, '검색된 곳: 출발 ' + data.start.name + ' (' + data.start.kind + '), 도착 ' + data.goal.name + ' (' + data.goal.kind + ')');
    if (!data.candidates.length) {
      replace(routeResult, title, kinds, message('이 조건으로는 경로를 찾지 못했어요.', { error: true }));
      title.focus({ preventScroll: false });
      return;
    }
    const hint = h('p', { class: 'lede' }, data.candidates.length + '개의 경로를 찾았어요. 하나를 눌러 세부 정보를 보세요.');
    const list = h('ul', { class: 'route-options' }, data.candidates.map(candidateCard));
    replace(routeResult, title, kinds, hint, list);
    title.focus({ preventScroll: false });
  }

  /* ---------- 경로 세부 (실시간 포함) ---------- */
  function renderRoute(data) {
    const s = data.summary || {};
    const totals = h('dl', { class: 'totals' },
      s.total_minutes !== null && s.total_minutes !== undefined
        ? h('div', null, h('dt', null, '소요'), h('dd', null, s.total_minutes + '분')) : null,
      s.fare !== null && s.fare !== undefined
        ? h('div', null, h('dt', null, '요금'), h('dd', null, s.fare.toLocaleString('ko-KR') + '원')) : null,
      h('div', null, h('dt', null, '환승'), h('dd', null, (s.transfers || 0) + '회')),
      s.total_distance_m ? h('div', null, h('dt', null, '거리'), h('dd', null, fmtDistance(s.total_distance_m))) : null);

    const backBtn = (lastCandidatesData && lastCandidatesData.candidates.length > 1)
      ? h('button', { class: 'btn btn-ghost btn-small back-btn', type: 'button', onclick: () => renderCandidates(lastCandidatesData) },
          '← 다른 경로 보기')
      : null;
    const title = h('h2', { class: 'route-title', tabindex: '-1' }, data.start.name + ' → ' + data.goal.name);
    const kinds = h('p', { class: 'route-kinds' }, '검색된 곳: 출발 ' + data.start.name + ' (' + data.start.kind + '), 도착 ' + data.goal.name + ' (' + data.goal.kind + ')');
    const rail = h('ol', { class: 'rail' },
      data.steps.map(renderStep),
      h('li', { class: 'step step-end' }, h('p', { class: 'step-title' }, '도착 ' + data.goal.name)));
    const footer = data.realtime_enabled
      ? h('p', { class: 'note' }, '실시간 정보는 ' + fmtClockTime(data.updated_at) + ' 기준이에요. 지하철은 시간표 기준이에요.')
      : h('p', { class: 'note' }, '이 서버에서는 실시간 정보를 쓸 수 없어 경로만 보여드려요.');
    replace(routeResult, backBtn, title, kinds, totals, rail, footer);
    title.focus({ preventScroll: false });
  }

  async function loadRouteDetail(index) {
    if (!lastRouteParams) return;
    routeResult.setAttribute('aria-busy', 'true');
    replace(routeResult, message('경로 세부 정보를 불러오는 중이에요. 실시간 정보까지 확인하느라 몇 초 걸릴 수 있어요.'));
    const signal = begin('route-detail');
    try {
      renderRoute(await api('/api/route/detail', { ...lastRouteParams, index }, signal));
    } catch (err) {
      if (!isAbort(err)) replace(routeResult, message(err.message, { error: true, title: '경로 세부 정보를 불러오지 못했어요' }));
    } finally {
      routeResult.removeAttribute('aria-busy');
    }
  }

  async function submitRoute(e) {
    e.preventDefault();
    const goal = goalInput.value.trim();
    const start = startInput.value.trim();
    if (!routeState.start.coords && !start) {
      replace(routeResult, message('출발지를 입력하거나 현재 위치를 눌러 주세요.', { error: true }));
      startInput.focus();
      return;
    }
    if (!routeState.goal.coords && !goal) {
      replace(routeResult, message('도착지를 입력해 주세요.', { error: true }));
      goalInput.focus();
      return;
    }
    const params = {};
    if (routeState.start.coords) {
      params.start_x = routeState.start.coords.x.toFixed(6);
      params.start_y = routeState.start.coords.y.toFixed(6);
    } else {
      params.start = start;
    }
    if (routeState.goal.coords) {
      params.goal_x = routeState.goal.coords.x.toFixed(6);
      params.goal_y = routeState.goal.coords.y.toFixed(6);
      if (goal) params.goal = goal; // 표시용 이름 (서버가 route.goal.name 으로 그대로 돌려줌)
    } else {
      params.goal = goal;
    }

    const submit = $('#route-submit');
    submit.disabled = true;
    routeResult.setAttribute('aria-busy', 'true');
    replace(routeResult, message('경로를 찾는 중이에요…'));
    const signal = begin('route');
    try {
      const data = await api('/api/route', params, signal);
      lastRouteParams = params;
      lastCandidatesData = data;
      if (data.candidates.length === 1) {
        await loadRouteDetail(0);
      } else {
        renderCandidates(data);
      }
    } catch (err) {
      if (!isAbort(err)) replace(routeResult, message(err.message, { error: true, title: '경로를 찾지 못했어요' }));
    } finally {
      submit.disabled = false;
      routeResult.removeAttribute('aria-busy');
    }
  }

  function initRoute() {
    $('#route-form').addEventListener('submit', submitRoute);
    $('#btn-locate').addEventListener('click', locate);

    setupAutocomplete(startInput, $('#start-suggestions'), 'suggest-start', {
      onPick: (place) => {
        routeState.start = { coords: { x: place.x, y: place.y }, source: 'search' };
        $('#btn-swap').disabled = false;
        setLocateStatus('');
      },
      onTyping: () => { if (routeState.start.coords) { clearStartCoords(); setLocateStatus(''); } },
    });
    startInput.addEventListener('focus', () => { if (routeState.start.coords) startInput.select(); });

    setupAutocomplete(goalInput, $('#goal-suggestions'), 'suggest-goal', {
      onPick: (place) => {
        routeState.goal = { coords: { x: place.x, y: place.y } };
        goalHint.textContent = [place.kind, place.address].filter(Boolean).join(' · ');
      },
      onTyping: () => { if (routeState.goal.coords) { routeState.goal = { coords: null }; goalHint.textContent = ''; } },
    });

    $('#btn-swap').addEventListener('click', () => {
      if (routeState.start.source === 'geo') return; // 현재 위치는 도착지로 옮기지 않는다
      const tmpValue = startInput.value;
      startInput.value = goalInput.value;
      goalInput.value = tmpValue;
      const oldStart = routeState.start;
      routeState.start = routeState.goal.coords ? { coords: routeState.goal.coords, source: 'search' } : { coords: null, source: null };
      routeState.goal = { coords: oldStart.coords };
      goalHint.textContent = '';
      setLocateStatus('');
    });
  }

  /* =====================================================================
     지하철
     ===================================================================== */
  const subwayDefault = $('#subway-default');
  const subwayResult = $('#subway-result');

  function renderSubwayDefault(data) {
    const boards = data.routes.map((r) => {
      const title = r.from + ' → ' + r.to;
      const sub = r.terminus ? r.terminus + '행' : r.way + '행';
      if (r.status === 'ok') return board({ title, sub, rows: trainRows(r.trains) });
      const text = {
        no_more_trains: '오늘 남은 열차가 없어요.',
        not_found: '운행 정보를 찾지 못했어요.',
        error: '지금은 불러오지 못했어요. 잠시 후 새로고침해 주세요.',
      }[r.status] || '정보가 없어요.';
      return board({ title, sub, notes: [text] });
    });
    replace(subwayDefault,
      h('h3', { class: 'group-title' }, data.line + ' 마전역과 주안역'),
      data.fallback ? h('p', { class: 'note' }, '오늘 운행 정보가 없는 역이 있어 평일 시간표 기준으로 보여드려요.') : null,
      boards);
  }

  async function loadSubwayDefault({ force = false } = {}) {
    if (!force && Date.now() - (loadedAt.subway || 0) < STALE_MS) return;
    const button = $('#subway-refresh');
    button.disabled = true;
    if (!subwayDefault.firstChild) replace(subwayDefault, message('불러오는 중이에요…'));
    const signal = begin('subway');
    try {
      renderSubwayDefault(await api('/api/subway', {}, signal));
      loadedAt.subway = Date.now();
    } catch (err) {
      if (!isAbort(err)) replace(subwayDefault, message(err.message, { error: true, title: '지하철 정보를 불러오지 못했어요' }));
    } finally {
      button.disabled = false;
    }
  }

  function renderSubwayStation(data) {
    const boards = data.directions.map((d) => (d.status === 'ok'
      ? board({ title: d.terminus + '행', rows: trainRows(d.trains) })
      : board({ title: d.terminus + '행', notes: ['오늘 남은 열차가 없어요.'] })));
    replace(subwayResult,
      h('h3', { class: 'group-title' }, data.station + (data.line ? ' (' + data.line + ')' : '') + ' 다음 열차'),
      data.fallback ? h('p', { class: 'note' }, '오늘 운행 정보가 없어 평일 시간표 기준으로 보여드려요.') : null,
      boards.length ? boards : message('운행 정보가 없어요.'));
  }

  async function submitSubway(e) {
    e.preventDefault();
    const line = $('#subway-line').value.trim();
    const station = $('#subway-station').value.trim();
    if (!station) {
      replace(subwayResult, message('역 이름을 입력해 주세요.', { error: true }));
      $('#subway-station').focus();
      return;
    }
    replace(subwayResult, message('불러오는 중이에요…'));
    const signal = begin('subway-search');
    try {
      renderSubwayStation(await api('/api/subway', { line, station }, signal));
    } catch (err) {
      if (!isAbort(err)) replace(subwayResult, message(err.message, { error: true, title: '열차 정보를 찾지 못했어요' }));
    }
  }

  function initSubway() {
    $('#subway-refresh').addEventListener('click', () => loadSubwayDefault({ force: true }));
    $('#subway-form').addEventListener('submit', submitSubway);
  }

  /* =====================================================================
     버스 (511번)
     ===================================================================== */
  const busArrivals = $('#bus-arrivals');
  const busLocations = $('#bus-locations');
  const busUpdated = $('#bus-updated');
  const BUS_REFRESH_MS = 20000;

  function renderBusArrivals(data) {
    const boards = data.stops.map((s) => {
      const title = s.name;
      const sub = s.toward + ' 방면';
      if (s.status === 'ok') {
        const rows = s.arrivals.map((a) => boardRow(
          minutesLabel(a.minutes),
          a.minutes === 0 ? '곧 도착' : a.minutes + '분 후 도착',
          [a.prev_stations !== null && a.prev_stations !== undefined ? a.prev_stations + '개 정류장 전' : '',
            a.vehicle_no ? '차량 ' + a.vehicle_no + ' (추정)' : ''].filter(Boolean).join(', '),
          { word: a.minutes === 0 },
        ));
        return board({ title, sub, rows });
      }
      const text = s.status === 'no_arrival' ? '지금 도착 예정인 511번이 없어요.' : (s.error || '정보를 불러오지 못했어요.');
      return board({ title, sub, notes: [text] });
    });
    replace(busArrivals, boards);
  }

  function renderBusLocations(data) {
    if (!data.buses.length) {
      replace(busLocations, message('지금 운행 중인 511번 버스가 없어요.'));
      return;
    }
    replace(busLocations, h('ul', { class: 'plain-list' },
      data.buses.map((b) => h('li', null,
        h('span', { class: 'strong' }, b.vehicle_no || '차량번호 없음'),
        h('span', { class: 'dim' }, (b.stop_name || '위치 확인 중') + ' 통과')))));
  }

  async function loadBus({ force = false, quiet = false } = {}) {
    if (!force && !quiet && Date.now() - (loadedAt.bus || 0) < 8000) return;
    const button = $('#bus-refresh');
    button.disabled = true;
    if (!quiet && !busArrivals.firstChild) replace(busArrivals, message('불러오는 중이에요…'));
    const signal = begin('bus');
    // 도착 정보와 위치 정보는 서로 독立적이라 동시에 요청하고, 하나가 실패해도 다른 하나는 보여준다.
    const [arr, loc] = await Promise.allSettled([api('/api/bus', {}, signal), api('/api/bus/locations', {}, signal)]);
    button.disabled = false;
    if ((arr.status === 'rejected' && isAbort(arr.reason)) || (loc.status === 'rejected' && isAbort(loc.reason))) return;

    if (arr.status === 'fulfilled') {
      renderBusArrivals(arr.value);
      busUpdated.textContent = fmtClockTime(arr.value.updated_at) + ' 기준이에요. 이 화면을 보는 동안 20초마다 새로 불러와요.';
      loadedAt.bus = Date.now();
    } else if (!quiet || !busArrivals.firstChild) {
      replace(busArrivals, message(arr.reason.message, { error: true, title: '버스 도착 정보를 불러오지 못했어요' }));
    } else {
      busUpdated.textContent = '새로 불러오지 못해 이전 정보를 보여주고 있어요.';
    }

    if (loc.status === 'fulfilled') renderBusLocations(loc.value);
    else if (!quiet || !busLocations.firstChild) replace(busLocations, message(loc.reason.message, { error: true, title: '버스 위치를 불러오지 못했어요' }));
  }

  function initBus() {
    $('#bus-refresh').addEventListener('click', () => loadBus({ force: true }));
    // 버스 탭이 열려 있고 화면이 보일 때만 자동 새로고침 (서버는 15초 캐시를 쓰므로 API 호출은 늘지 않는다)
    setInterval(() => {
      if (activeTab === 'bus' && document.visibilityState === 'visible') loadBus({ force: true, quiet: true });
    }, BUS_REFRESH_MS);
    document.addEventListener('visibilitychange', () => {
      if (activeTab === 'bus' && document.visibilityState === 'visible') loadBus();
    });
  }

  /* =====================================================================
     시간표
     ===================================================================== */
  const timetableDays = $('#timetable-days');
  const timetableResult = $('#timetable-result');
  let currentDay = null;
  let dayButtonsBuilt = false;

  function buildDayButtons(days, today) {
    replace(timetableDays, days.map((day) => h('button', {
      class: 'day' + (day === today ? ' is-today' : ''),
      type: 'button',
      'data-day': day,
      'aria-pressed': 'false',
      onclick: () => loadTimetable(day),
    }, day.charAt(0), day === today ? h('span', { class: 'sr-only' }, ' (오늘)') : null)));
    dayButtonsBuilt = true;
  }

  function splitTime(range) {
    const m = /(\d{1,2}:\d{2})\s*~\s*(\d{1,2}:\d{2})/.exec(range || '');
    return m ? [m[1], m[2]] : [range || '', ''];
  }

  function renderTimetable(data) {
    if (!dayButtonsBuilt) buildDayButtons(data.days, data.today);
    for (const btn of timetableDays.querySelectorAll('.day')) {
      btn.setAttribute('aria-pressed', String(btn.dataset.day === data.day));
      btn.setAttribute('aria-label', btn.dataset.day + (btn.dataset.day === data.today ? ' (오늘)' : ''));
    }
    const heading = h('h3', { class: 'group-title' }, data.day === data.today ? '오늘 ' + data.day : data.day);
    if (!data.classes.length) {
      replace(timetableResult, heading, message('이 요일에는 수업이 없어요.'));
      return;
    }
    replace(timetableResult, heading, h('ul', { class: 'classes' },
      data.classes.map((c) => {
        const [start, end] = splitTime(c.time);
        return h('li', null,
          h('p', { class: 'class-time' }, start, end ? h('small', null, end + '까지') : null),
          h('div', null, h('p', { class: 'class-name' }, c.subject), h('p', { class: 'class-room' }, c.room)));
      })));
  }

  async function loadTimetable(day) {
    const signal = begin('timetable');
    try {
      const data = await api('/api/timetable', { day: day || '' }, signal);
      currentDay = data.day;
      renderTimetable(data);
    } catch (err) {
      if (!isAbort(err)) replace(timetableResult, message(err.message, { error: true, title: '시간표를 불러오지 못했어요' }));
    }
  }

  /* ---------- 시작 ---------- */
  initTabs();
  initRoute();
  initSubway();
  initBus();
  tickClock();
  setInterval(tickClock, 30000);
  activate(TABS.includes(location.hash.replace('#', '')) ? location.hash.replace('#', '') : 'route', { updateHash: false });
})();