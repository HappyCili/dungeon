(() => {
  "use strict";

  const initialState = JSON.parse(document.getElementById("initial-state").textContent);
  const state = {
    config: initialState.config,
    daily: initialState.daily,
    arena: null,
    treasure: null,
    dungeon: null,
    dungeonRewards: [],
    dungeonDraw: null,
    activeJobId: initialState.config.active_job_id,
    lastSequence: 0,
    pollTimer: null,
    toastTimer: null,
  };

  const elements = {
    connectionStatus: document.getElementById("connection-status"),
    loginForm: document.getElementById("login-form"),
    username: document.getElementById("username"),
    password: document.getElementById("password"),
    rememberPassword: document.getElementById("remember-password"),
    togglePassword: document.getElementById("toggle-password"),
    loginButton: document.getElementById("login-button"),
    loginMessage: document.getElementById("login-message"),
    zoneSelect: document.getElementById("zone-select"),
    taskTableBody: document.getElementById("task-table-body"),
    activityScore: document.getElementById("activity-score"),
    nextReward: document.getElementById("next-reward"),
    completedCount: document.getElementById("completed-count"),
    refreshCountdown: document.getElementById("refresh-countdown"),
    selectAvailable: document.getElementById("select-available"),
    clearSelection: document.getElementById("clear-selection"),
    refreshDaily: document.getElementById("refresh-daily"),
    runDaily: document.getElementById("run-daily"),
    stopDaily: document.getElementById("stop-daily"),
    rounds: document.getElementById("arena-rounds"),
    decreaseRounds: document.getElementById("rounds-decrease"),
    increaseRounds: document.getElementById("rounds-increase"),
    refreshOnExhaustion: document.getElementById("refresh-on-exhaustion"),
    refreshArena: document.getElementById("refresh-arena"),
    runArena: document.getElementById("run-arena"),
    stopArena: document.getElementById("stop-arena"),
    arenaScore: document.getElementById("arena-score"),
    arenaOpponents: document.getElementById("arena-opponents"),
    arenaDailyReward: document.getElementById("arena-daily-reward"),
    arenaRequested: document.getElementById("arena-requested"),
    arenaCompleted: document.getElementById("arena-completed"),
    arenaWinLoss: document.getElementById("arena-win-loss"),
    arenaStage: document.getElementById("arena-stage"),
    arenaScoreDelta: document.getElementById("arena-score-delta"),
    arenaCoinDelta: document.getElementById("arena-coin-delta"),
    arenaLastResult: document.getElementById("arena-last-result"),
    treasureArea: document.getElementById("treasure-area"),
    treasureTimes: document.getElementById("treasure-times"),
    decreaseTreasureTimes: document.getElementById("treasure-times-decrease"),
    increaseTreasureTimes: document.getElementById("treasure-times-increase"),
    refreshTreasure: document.getElementById("refresh-treasure"),
    runTreasure: document.getElementById("run-treasure"),
    stopTreasure: document.getElementById("stop-treasure"),
    treasureUsed: document.getElementById("treasure-used"),
    treasureLimit: document.getElementById("treasure-limit"),
    treasureAvailable: document.getElementById("treasure-available"),
    treasureRequestLimit: document.getElementById("treasure-request-limit"),
    dungeonSelect: document.getElementById("dungeon-select"),
    refreshDungeon: document.getElementById("refresh-dungeon"),
    runDungeon: document.getElementById("run-dungeon"),
    stopDungeon: document.getElementById("stop-dungeon"),
    dungeonName: document.getElementById("dungeon-name"),
    dungeonHighestScore: document.getElementById("dungeon-highest-score"),
    dungeonDrawProgress: document.getElementById("dungeon-draw-progress"),
    dungeonRewardSummary: document.getElementById("dungeon-reward-summary"),
    dungeonRewardList: document.getElementById("dungeon-reward-list"),
    jobStatus: document.getElementById("job-status"),
    jobProgress: document.getElementById("job-progress"),
    logList: document.getElementById("log-list"),
    toast: document.getElementById("toast"),
  };

  const jobStatusLabels = {
    queued: "排队中",
    running: "运行中",
    stopping: "停止中",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已停止",
  };

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || "请求失败");
    }
    return data;
  }

  function showToast(message, isError = false) {
    window.clearTimeout(state.toastTimer);
    elements.toast.textContent = message;
    elements.toast.classList.toggle("is-error", isError);
    elements.toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      elements.toast.hidden = true;
    }, 3600);
  }

  function setConnectionStatus(connection) {
    elements.connectionStatus.className = `status-indicator status-indicator--${connection.status}`;
    elements.connectionStatus.replaceChildren();
    const dot = document.createElement("span");
    dot.className = "status-indicator__dot";
    elements.connectionStatus.append(dot, document.createTextNode(connection.label));
  }

  function renderZones(config) {
    const currentId = config.zone.id;
    const fragment = document.createDocumentFragment();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "请选择区服";
    fragment.append(placeholder);
    config.zones.forEach((zone) => {
      const option = document.createElement("option");
      option.value = zone.id;
      option.dataset.zoneName = zone.name;
      option.textContent = zone.name;
      option.selected = zone.id === currentId;
      fragment.append(option);
    });
    elements.zoneSelect.replaceChildren(fragment);
    elements.zoneSelect.disabled = config.zones.length === 0;
  }

  function applyConfig(config) {
    state.config = config;
    elements.username.value = config.account.username;
    elements.rememberPassword.checked = config.account.remember_password;
    elements.rounds.value = String(config.arena.rounds);
    elements.refreshOnExhaustion.checked = config.arena.refresh_on_exhaustion;
    elements.treasureTimes.value = String(config.treasure.times);
    document.querySelectorAll("[data-outcome]").forEach((button) => {
      const selected = button.dataset.outcome === config.arena.outcome;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    setConnectionStatus(config.connection);
    renderZones(config);
    if (state.treasure) {
      renderTreasure(state.treasure);
    }
    if (state.arena) {
      renderArenaStatus(state.arena);
    }
    if (state.dungeon) {
      renderDungeon(state.dungeon);
    }
  }

  function resultClass(result) {
    if (result === "完成") {
      return "result-text result-text--success";
    }
    if (result === "运行中") {
      return "result-text result-text--running";
    }
    if (result.includes("跳过")) {
      return "result-text result-text--muted";
    }
    return "result-text";
  }

  function taskCell(text, className = "") {
    const cell = document.createElement("td");
    cell.textContent = text;
    if (className) {
      cell.className = className;
    }
    return cell;
  }

  function renderDaily(daily) {
    state.daily = daily;
    if (!daily) {
      elements.activityScore.textContent = "--";
      elements.nextReward.textContent = "--";
      elements.completedCount.textContent = "--";
      elements.refreshCountdown.textContent = "--:--:--";
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 7;
      cell.textContent = "登录并选择区服后刷新日常状态";
      row.append(cell);
      elements.taskTableBody.replaceChildren(row);
      return;
    }
    const { summary } = daily;
    elements.activityScore.textContent = String(summary.activity_score);
    elements.nextReward.textContent = summary.next_reward === null ? "已达成" : String(summary.next_reward);
    elements.completedCount.textContent = `${summary.completed_count}/${summary.total_count}`;
    elements.refreshCountdown.textContent = summary.refresh_countdown;

    const fragment = document.createDocumentFragment();
    daily.tasks.forEach((task) => {
      const row = document.createElement("tr");
      row.dataset.taskId = String(task.id);
      const selectCell = document.createElement("td");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "task-checkbox";
      checkbox.dataset.taskId = String(task.id);
      checkbox.checked = task.selected;
      checkbox.disabled = !task.available;
      checkbox.setAttribute("aria-label", `选择 ${task.name}`);
      if (!task.available) {
        checkbox.title = task.gap;
      }
      checkbox.addEventListener("change", () => updateSelectionFromTable());
      selectCell.append(checkbox);
      row.append(
        selectCell,
        taskCell(task.name, "task-name"),
        taskCell(`${task.target} 次`),
        taskCell(`${task.progress}/${task.target}`),
        taskCell(String(task.activity_score), "numeric-cell"),
      );
      const implementationCell = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = `state-badge ${task.available ? "state-badge--available" : "state-badge--muted"}`;
      badge.title = task.gap;
      badge.textContent = task.implementation_status;
      implementationCell.append(badge);
      const resultCell = document.createElement("td");
      const result = document.createElement("span");
      result.className = resultClass(task.result);
      result.textContent = task.result;
      resultCell.append(result);
      row.append(implementationCell, resultCell);
      fragment.append(row);
    });
    elements.taskTableBody.replaceChildren(fragment);
  }

  function selectedTaskIds() {
    return [...elements.taskTableBody.querySelectorAll(".task-checkbox:checked")]
      .map((checkbox) => Number(checkbox.dataset.taskId));
  }

  async function persistSelection(taskIds) {
    const data = await api("/api/daily-tasks/selection", {
      method: "PUT",
      body: JSON.stringify({ task_ids: taskIds }),
    });
    applyConfig(data.config);
    if (state.daily) {
      state.daily.tasks.forEach((task) => {
        task.selected = taskIds.includes(task.id);
      });
      renderDaily(state.daily);
    }
  }

  async function updateSelectionFromTable() {
    try {
      await persistSelection(selectedTaskIds());
    } catch (error) {
      showToast(error.message, true);
      renderDaily(state.daily);
    }
  }

  function renderArenaStats(stats) {
    if (!stats) {
      elements.arenaRequested.textContent = String(state.config.arena.rounds);
      elements.arenaCompleted.textContent = "0";
      elements.arenaWinLoss.textContent = "0 / 0";
      elements.arenaStage.textContent = "空闲";
      elements.arenaScoreDelta.textContent = "+0";
      elements.arenaCoinDelta.textContent = "+0";
      elements.arenaLastResult.textContent = "等待开始";
      return;
    }
    elements.arenaRequested.textContent = String(stats.requested_rounds);
    elements.arenaCompleted.textContent = String(stats.completed_rounds);
    elements.arenaWinLoss.textContent = `${stats.wins} / ${stats.losses}`;
    elements.arenaStage.textContent = stats.stage;
    elements.arenaScoreDelta.textContent = `${stats.score_delta >= 0 ? "+" : ""}${stats.score_delta}`;
    elements.arenaCoinDelta.textContent = `${stats.dragon_coin_delta >= 0 ? "+" : ""}${stats.dragon_coin_delta}`;
    elements.arenaLastResult.textContent = stats.last_result;
    if (Number.isFinite(stats.score)) {
      elements.arenaScore.textContent = String(stats.score);
    }
    if (stats.opponents) {
      elements.arenaOpponents.textContent = `${stats.opponents.available} / ${stats.opponents.total}`;
    }
    if (stats.daily_reward) {
      elements.arenaDailyReward.textContent = String(stats.daily_reward.count);
    }
  }

  function renderArenaStatus(arena) {
    state.arena = arena;
    if (!arena) {
      elements.arenaScore.textContent = "--";
      elements.arenaOpponents.textContent = "--";
      elements.arenaDailyReward.textContent = "--";
      return;
    }
    elements.arenaScore.textContent = String(arena.score);
    elements.arenaOpponents.textContent = `${arena.opponents.available} / ${arena.opponents.total}`;
    elements.arenaDailyReward.textContent = String(arena.daily_reward.count);
  }

  function renderTreasure(treasure) {
    state.treasure = treasure;
    const selectedAreaId = state.config.treasure.area_id;
    const fragment = document.createDocumentFragment();
    const placeholder = document.createElement("option");
    placeholder.value = "";

    if (!treasure) {
      placeholder.textContent = "请先刷新聚宝之地状态";
      fragment.append(placeholder);
      elements.treasureArea.replaceChildren(fragment);
      elements.treasureArea.disabled = true;
      elements.treasureUsed.textContent = "--";
      elements.treasureLimit.textContent = "--";
      elements.treasureAvailable.textContent = "--";
      elements.treasureRequestLimit.textContent = "30";
      return;
    }

    placeholder.textContent = treasure.areas.length ? "请选择地图" : "当前没有可扫荡地图";
    fragment.append(placeholder);
    treasure.areas.forEach((area) => {
      const option = document.createElement("option");
      option.value = String(area.id);
      option.textContent = area.name;
      option.selected = area.id === selectedAreaId;
      fragment.append(option);
    });
    elements.treasureArea.replaceChildren(fragment);
    elements.treasureArea.value = treasure.areas.some((area) => area.id === selectedAreaId)
      ? String(selectedAreaId)
      : "";

    const requestLimit = treasure.sweep.request_limit;
    const configuredTimes = state.config.treasure.times;
    const visibleTimes = requestLimit > 0
      ? Math.min(Math.max(configuredTimes, 1), requestLimit)
      : 1;
    elements.treasureTimes.min = "1";
    elements.treasureTimes.max = String(Math.max(requestLimit, 1));
    elements.treasureTimes.value = String(visibleTimes);
    elements.treasureUsed.textContent = String(treasure.sweep.used);
    elements.treasureLimit.textContent = String(treasure.sweep.limit);
    elements.treasureAvailable.textContent = String(treasure.sweep.available);
    elements.treasureRequestLimit.textContent = String(requestLimit);
  }

  function renderDungeon(dungeon) {
    state.dungeon = dungeon;
    const fragment = document.createDocumentFragment();
    const placeholder = document.createElement("option");
    placeholder.value = "";

    if (!dungeon) {
      placeholder.textContent = "请先刷新地下城状态";
      fragment.append(placeholder);
      elements.dungeonSelect.replaceChildren(fragment);
      elements.dungeonSelect.disabled = true;
      elements.dungeonName.textContent = "--";
      elements.dungeonHighestScore.textContent = "--";
      elements.dungeonDrawProgress.textContent = "-- / --";
      return;
    }

    const selectedDungeonId = state.config.dungeon.dungeon_id;
    placeholder.textContent = dungeon.dungeons.length ? "请选择地下城" : "当前没有可扫荡地下城";
    fragment.append(placeholder);
    dungeon.dungeons.forEach((entry) => {
      const option = document.createElement("option");
      option.value = String(entry.id);
      option.textContent = `${entry.name} · 最高分 ${entry.highest_score}`;
      option.selected = entry.id === selectedDungeonId;
      fragment.append(option);
    });
    elements.dungeonSelect.replaceChildren(fragment);
    const selected = dungeon.dungeons.find((entry) => entry.id === selectedDungeonId) || null;
    elements.dungeonSelect.value = selected ? String(selected.id) : "";
    elements.dungeonName.textContent = selected ? selected.name : "--";
    elements.dungeonHighestScore.textContent = selected ? String(selected.highest_score) : "--";
    elements.dungeonDrawProgress.textContent = `${dungeon.draw.used} / ${dungeon.draw.total}`;
  }

  function dungeonRewardLabel(reward) {
    const quantity = Number.isInteger(reward.quantity) && reward.quantity > 0
      ? ` × ${reward.quantity}`
      : "";
    return `${reward.name}${quantity}`;
  }

  function renderDungeonRewards(rewards = [], draw = null) {
    state.dungeonRewards = rewards;
    state.dungeonDraw = draw;
    const fragment = document.createDocumentFragment();
    if (rewards.length === 0) {
      const item = document.createElement("li");
      item.className = "dungeon-reward-empty";
      item.textContent = draw?.reward_notice_received
        ? "服务端结算未包含可展示的奖励条目"
        : draw
          ? "已完成抽取，但服务端未返回奖励结算明细"
          : "扫荡并全部抽取后在此显示服务端返回的奖励数据";
      fragment.append(item);
      elements.dungeonRewardSummary.textContent = draw
        ? draw.reward_notice_received
          ? "已完成抽取，0 项服务端结算奖励"
          : "已完成抽取，等待服务端奖励结算"
        : "尚未获取奖励";
    } else {
      rewards.forEach((reward) => {
        const item = document.createElement("li");
        item.className = "dungeon-reward-item";
        const name = document.createElement("strong");
        name.textContent = dungeonRewardLabel(reward);
        item.append(name);
        fragment.append(item);
      });
      const allDrawn = draw?.all_drawn ? "已全部抽取" : "已完成抽取";
      elements.dungeonRewardSummary.textContent = `${allDrawn}，${rewards.length} 项奖励`;
    }
    elements.dungeonRewardList.replaceChildren(fragment);
  }

  function logTime(timestamp) {
    if (!timestamp) {
      return "--:--:--";
    }
    const match = timestamp.match(/T(\d{2}:\d{2}:\d{2})/);
    return match ? match[1] : timestamp.slice(-8);
  }

  function appendEvent(event) {
    const item = document.createElement("li");
    item.className = `log-item log-item--${event.level}`;
    const time = document.createElement("time");
    time.textContent = logTime(event.timestamp);
    const feature = document.createElement("span");
    const featureLabels = { daily: "日常", arena: "竞技场", treasure: "聚宝", dungeon: "地下城" };
    feature.textContent = featureLabels[event.feature] || "系统";
    const message = document.createElement("p");
    message.textContent = event.message;
    item.append(time, feature, message);
    elements.logList.append(item);
    while (elements.logList.children.length > 80) {
      elements.logList.firstElementChild.remove();
    }
    elements.logList.scrollTop = elements.logList.scrollHeight;
  }

  function isActiveJobStatus(status) {
    return ["queued", "running", "stopping"].includes(status);
  }

  function setJobControls(status = "idle") {
    const active = isActiveJobStatus(status);
    const gameSessionConfigured = state.config.connection.status === "available" && Boolean(state.config.zone.id);
    const dailyLoaded = Boolean(state.daily);
    const treasureLoaded = Boolean(state.treasure);
    const dungeonLoaded = Boolean(state.dungeon);
    const selectedTreasureArea = Number(elements.treasureArea.value);
    const selectedDungeonId = Number(elements.dungeonSelect.value);
    const treasureReady = treasureLoaded
      && state.treasure.sweep.available > 0
      && state.treasure.areas.some((area) => area.id === selectedTreasureArea);
    const dungeonReady = dungeonLoaded
      && state.dungeon.dungeons.some((dungeon) => dungeon.id === selectedDungeonId);
    elements.runDaily.disabled = active || !dailyLoaded;
    elements.runArena.disabled = active || !gameSessionConfigured;
    elements.runTreasure.disabled = active || !treasureReady;
    elements.runDungeon.disabled = active || !dungeonReady;
    elements.stopDaily.disabled = !active;
    elements.stopArena.disabled = !active;
    elements.stopTreasure.disabled = !active;
    elements.stopDungeon.disabled = !active;
    elements.selectAvailable.disabled = active || !dailyLoaded;
    elements.clearSelection.disabled = active || !dailyLoaded;
    elements.refreshDaily.disabled = active || !gameSessionConfigured;
    elements.refreshArena.disabled = active || !gameSessionConfigured;
    elements.refreshTreasure.disabled = active || !gameSessionConfigured;
    elements.refreshDungeon.disabled = active || !gameSessionConfigured;
    elements.treasureArea.disabled = active || !treasureLoaded || state.treasure.areas.length === 0;
    elements.treasureTimes.disabled = active || !treasureLoaded || state.treasure.sweep.request_limit === 0;
    elements.decreaseTreasureTimes.disabled = elements.treasureTimes.disabled;
    elements.increaseTreasureTimes.disabled = elements.treasureTimes.disabled;
    elements.dungeonSelect.disabled = active || !dungeonLoaded || state.dungeon.dungeons.length === 0;
  }

  function applyJob(job) {
    job.events.forEach((event) => {
      if (event.sequence <= state.lastSequence) {
        return;
      }
      state.lastSequence = event.sequence;
      appendEvent(event);
      if (event.data.daily) {
        renderDaily(event.data.daily);
      }
      if (event.data.arena) {
        renderArenaStats(event.data.arena);
      }
      if (event.data.treasure) {
        renderTreasure(event.data.treasure);
      }
      if (event.data.dungeon) {
        renderDungeon(event.data.dungeon);
      }
      if (event.feature === "dungeon" && Object.prototype.hasOwnProperty.call(event.data, "rewards")) {
        renderDungeonRewards(event.data.rewards, event.data.draw || null);
      }
    });
    if (job.result?.daily) {
      renderDaily(job.result.daily);
    }
    if (job.result?.arena) {
      renderArenaStats(job.result.arena);
    }
    if (job.result?.treasure) {
      renderTreasure(job.result.treasure);
    }
    if (job.result?.dungeon) {
      renderDungeon(job.result.dungeon);
    }
    if (job.feature === "dungeon" && job.result && Object.prototype.hasOwnProperty.call(job.result, "rewards")) {
      renderDungeonRewards(job.result.rewards, job.result.draw || null);
    }
    const status = job.status;
    elements.jobStatus.textContent = jobStatusLabels[status] || status;
    elements.jobStatus.dataset.state = status;
    const progress = job.progress || {};
    if (job.error_message) {
      elements.jobProgress.textContent = job.error_message;
    } else if (progress.task_id) {
      const taskName = progress.task_name
        || state.daily?.tasks.find((task) => task.id === progress.task_id)?.name
        || `未知日常任务（ID ${progress.task_id}）`;
      elements.jobProgress.textContent = `${taskName} · ${progress.completed_tasks || 0}/${progress.total_tasks || 0}`;
    } else if (progress.arena) {
      elements.jobProgress.textContent = `竞技场 ${progress.arena.completed_rounds}/${progress.arena.requested_rounds} · ${progress.arena.stage}`;
    } else if (progress.treasure) {
      elements.jobProgress.textContent = `聚宝之地 · 今日可扫荡 ${progress.treasure.sweep.available} 次`;
    } else if (progress.dungeon) {
      if (progress.phase === "drawing") {
        elements.jobProgress.textContent = "地下城 · 扫荡完成，正在全部抽取";
      } else if (progress.phase === "completed") {
        const rewardLabel = progress.draw?.reward_notice_received
          ? "服务端结算奖励"
          : "抽取结果";
        elements.jobProgress.textContent = `地下城 · 已全部抽取 ${progress.rewards?.length || 0} 项${rewardLabel}`;
      } else {
        const selected = progress.dungeon.dungeons.find((dungeon) => dungeon.selected);
        elements.jobProgress.textContent = selected
          ? `地下城 · ${selected.name}，最高分 ${selected.highest_score}`
          : "地下城 · 已读取扫荡状态";
      }
    } else {
      elements.jobProgress.textContent = jobStatusLabels[status] || status;
    }
    setJobControls(status);

    if (!isActiveJobStatus(status)) {
      state.activeJobId = null;
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      refreshConfig().catch(() => {});
    }
  }

  function beginPolling(job) {
    state.activeJobId = job.id;
    state.lastSequence = 0;
    applyJob(job);
    window.clearInterval(state.pollTimer);
    state.pollTimer = window.setInterval(() => {
      pollJob().catch((error) => showToast(error.message, true));
    }, 450);
  }

  async function pollJob() {
    if (!state.activeJobId) {
      return;
    }
    const job = await api(`/api/jobs/${state.activeJobId}?after=${state.lastSequence}`, { method: "GET" });
    applyJob(job);
  }

  async function refreshConfig() {
    const config = await api("/api/config", { method: "GET" });
    applyConfig(config);
    if (config.active_job_id && !state.activeJobId) {
      state.activeJobId = config.active_job_id;
      state.lastSequence = 0;
      await pollJob();
      if (state.activeJobId) {
        state.pollTimer = window.setInterval(() => {
          pollJob().catch((error) => showToast(error.message, true));
        }, 450);
      }
    }
  }

  async function saveArenaConfig() {
    const rounds = Number(elements.rounds.value);
    const outcome = state.config.arena.outcome;
    const refreshOnExhaustion = elements.refreshOnExhaustion.checked;
    const data = await api("/api/config/arena", {
      method: "PUT",
      body: JSON.stringify({
        rounds,
        outcome,
        refresh_on_exhaustion: refreshOnExhaustion,
      }),
    });
    applyConfig(data.config);
  }

  async function refreshArena() {
    const arena = await api("/api/arena", { method: "GET" });
    applyConfig(arena.config);
    renderArenaStatus(arena);
    setJobControls();
  }

  function normalizedTreasureTimes() {
    const requestLimit = state.treasure?.sweep.request_limit || 1;
    const value = Number(elements.treasureTimes.value);
    return Number.isInteger(value) ? Math.min(Math.max(value, 1), requestLimit) : 1;
  }

  async function saveTreasureConfig() {
    const areaId = Number(elements.treasureArea.value);
    if (!Number.isInteger(areaId) || areaId <= 0) {
      return;
    }
    const times = normalizedTreasureTimes();
    elements.treasureTimes.value = String(times);
    const data = await api("/api/config/treasure", {
      method: "PUT",
      body: JSON.stringify({ area_id: areaId, times }),
    });
    applyConfig(data.config);
  }

  async function refreshTreasure() {
    const treasure = await api("/api/treasure", { method: "GET" });
    applyConfig(treasure.config);
    renderTreasure(treasure);
    setJobControls();
  }

  async function saveDungeonConfig() {
    const dungeonId = Number(elements.dungeonSelect.value);
    if (!Number.isInteger(dungeonId) || dungeonId <= 0) {
      return;
    }
    const data = await api("/api/config/dungeon", {
      method: "PUT",
      body: JSON.stringify({ dungeon_id: dungeonId }),
    });
    applyConfig(data.config);
  }

  async function refreshDungeon() {
    const dungeon = await api("/api/dungeon", { method: "GET" });
    applyConfig(dungeon.config);
    renderDungeon(dungeon);
    setJobControls();
  }

  async function cancelActiveJob() {
    if (!state.activeJobId) {
      return;
    }
    try {
      const data = await api(`/api/jobs/${state.activeJobId}/cancel`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      applyJob(data.job);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function activateTab(name) {
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const selected = button.dataset.tab === name;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      document.getElementById(`${button.dataset.tab}-panel`).hidden = !selected;
    });
  }

  elements.loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    elements.loginButton.disabled = true;
    elements.loginMessage.textContent = "登录中";
    renderDaily(null);
    renderArenaStatus(null);
    renderArenaStats(null);
    renderTreasure(null);
    renderDungeon(null);
    renderDungeonRewards();
    setJobControls();
    try {
      const data = await api("/api/account/login", {
        method: "POST",
        body: JSON.stringify({
          username: elements.username.value,
          password: elements.password.value,
          remember_password: elements.rememberPassword.checked,
        }),
      });
      applyConfig(data.config);
      setJobControls();
      elements.password.value = "";
      elements.loginMessage.textContent = data.message;
    } catch (error) {
      elements.loginMessage.textContent = error.message;
      showToast(error.message, true);
    } finally {
      elements.loginButton.disabled = false;
    }
  });

  elements.togglePassword.addEventListener("click", () => {
    const visible = elements.password.type === "text";
    elements.password.type = visible ? "password" : "text";
    elements.togglePassword.setAttribute("aria-pressed", String(!visible));
    elements.togglePassword.setAttribute("aria-label", visible ? "显示密码" : "隐藏密码");
    elements.togglePassword.title = visible ? "显示密码" : "隐藏密码";
  });

  elements.zoneSelect.addEventListener("change", async () => {
    const option = elements.zoneSelect.selectedOptions[0];
    if (!option?.value) {
      return;
    }
    try {
      const data = await api("/api/config/zone", {
        method: "PUT",
        body: JSON.stringify({ id: option.value, name: option.dataset.zoneName }),
      });
      applyConfig(data.config);
      renderDaily(null);
      renderArenaStatus(null);
      renderArenaStats(null);
      renderTreasure(null);
      renderDungeon(null);
      renderDungeonRewards();
      setJobControls();
      const [daily, arena, treasure, dungeon] = await Promise.all([
        api("/api/daily-tasks", { method: "GET" }),
        api("/api/arena", { method: "GET" }),
        api("/api/treasure", { method: "GET" }),
        api("/api/dungeon", { method: "GET" }),
      ]);
      renderDaily(daily);
      applyConfig(arena.config);
      renderArenaStatus(arena);
      applyConfig(treasure.config);
      renderTreasure(treasure);
      renderDungeon(dungeon);
      setJobControls();
    } catch (error) {
      showToast(error.message, true);
      renderZones(state.config);
    }
  });

  elements.selectAvailable.addEventListener("click", () => {
    if (!state.daily) {
      return;
    }
    persistSelection(state.daily.tasks.filter((task) => task.available).map((task) => task.id)).catch((error) => showToast(error.message, true));
  });

  elements.clearSelection.addEventListener("click", () => {
    persistSelection([]).catch((error) => showToast(error.message, true));
  });

  elements.refreshDaily.addEventListener("click", async () => {
    try {
      const daily = await api("/api/daily-tasks", { method: "GET" });
      renderDaily(daily);
      setJobControls();
      showToast("日常状态已刷新");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  elements.runDaily.addEventListener("click", async () => {
    let started = false;
    elements.runDaily.disabled = true;
    elements.jobStatus.textContent = "连接游戏服";
    elements.jobProgress.textContent = "正在解析区服并建立游戏服 WebSocket 会话";
    try {
      const data = await api("/api/jobs/daily", { method: "POST", body: JSON.stringify({}) });
      if (data.daily) {
        renderDaily(data.daily);
      }
      beginPolling(data.job);
      started = true;
    } catch (error) {
      showToast(error.message, true);
      elements.jobStatus.textContent = "空闲";
      elements.jobProgress.textContent = "等待操作";
    } finally {
      if (!started) {
        setJobControls();
      }
    }
  });

  elements.stopDaily.addEventListener("click", cancelActiveJob);
  elements.stopArena.addEventListener("click", cancelActiveJob);
  elements.stopTreasure.addEventListener("click", cancelActiveJob);
  elements.stopDungeon.addEventListener("click", cancelActiveJob);

  elements.decreaseRounds.addEventListener("click", () => {
    elements.rounds.value = String(Math.max(1, Number(elements.rounds.value || 1) - 1));
    saveArenaConfig().catch((error) => showToast(error.message, true));
  });

  elements.increaseRounds.addEventListener("click", () => {
    elements.rounds.value = String(Math.min(100, Number(elements.rounds.value || 1) + 1));
    saveArenaConfig().catch((error) => showToast(error.message, true));
  });

  elements.rounds.addEventListener("change", () => {
    const next = Math.min(100, Math.max(1, Number(elements.rounds.value || 1)));
    elements.rounds.value = String(next);
    saveArenaConfig().catch((error) => showToast(error.message, true));
  });

  document.querySelectorAll("[data-outcome]").forEach((button) => {
    button.addEventListener("click", () => {
      state.config.arena.outcome = button.dataset.outcome;
      document.querySelectorAll("[data-outcome]").forEach((item) => {
        const selected = item === button;
        item.classList.toggle("is-selected", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      saveArenaConfig().catch((error) => showToast(error.message, true));
    });
  });

  elements.refreshOnExhaustion.addEventListener("change", () => {
    saveArenaConfig().catch((error) => showToast(error.message, true));
  });

  elements.refreshArena.addEventListener("click", () => {
    refreshArena()
      .then(() => showToast("龙痕竞技场状态已刷新"))
      .catch((error) => showToast(error.message, true));
  });

  elements.refreshTreasure.addEventListener("click", () => {
    refreshTreasure()
      .then(() => showToast("聚宝之地状态已刷新"))
      .catch((error) => showToast(error.message, true));
  });

  elements.refreshDungeon.addEventListener("click", () => {
    refreshDungeon()
      .then(() => showToast("地下城状态已刷新"))
      .catch((error) => showToast(error.message, true));
  });

  elements.treasureArea.addEventListener("change", () => {
    saveTreasureConfig()
      .then(() => setJobControls())
      .catch((error) => {
        renderTreasure(state.treasure);
        showToast(error.message, true);
      });
  });

  elements.dungeonSelect.addEventListener("change", () => {
    saveDungeonConfig()
      .then(() => {
        renderDungeon(state.dungeon);
        setJobControls();
      })
      .catch((error) => {
        renderDungeon(state.dungeon);
        showToast(error.message, true);
      });
  });

  elements.decreaseTreasureTimes.addEventListener("click", () => {
    const next = Math.max(1, normalizedTreasureTimes() - 1);
    elements.treasureTimes.value = String(next);
    saveTreasureConfig()
      .then(() => setJobControls())
      .catch((error) => showToast(error.message, true));
  });

  elements.increaseTreasureTimes.addEventListener("click", () => {
    const next = Math.min(state.treasure.sweep.request_limit, normalizedTreasureTimes() + 1);
    elements.treasureTimes.value = String(next);
    saveTreasureConfig()
      .then(() => setJobControls())
      .catch((error) => showToast(error.message, true));
  });

  elements.treasureTimes.addEventListener("change", () => {
    const next = normalizedTreasureTimes();
    elements.treasureTimes.value = String(next);
    saveTreasureConfig()
      .then(() => setJobControls())
      .catch((error) => showToast(error.message, true));
  });

  elements.runArena.addEventListener("click", async () => {
    const rounds = Number(elements.rounds.value);
    try {
      const data = await api("/api/jobs/arena", {
        method: "POST",
        body: JSON.stringify({
          rounds,
          outcome: state.config.arena.outcome,
          refresh_on_exhaustion: elements.refreshOnExhaustion.checked,
        }),
      });
      elements.arenaRequested.textContent = String(rounds);
      beginPolling(data.job);
    } catch (error) {
      showToast(error.message, true);
    }
  });

  elements.runTreasure.addEventListener("click", async () => {
    const areaId = Number(elements.treasureArea.value);
    const times = normalizedTreasureTimes();
    if (!Number.isInteger(areaId) || areaId <= 0) {
      showToast("请选择可扫荡地图", true);
      return;
    }
    try {
      const data = await api("/api/jobs/treasure", {
        method: "POST",
        body: JSON.stringify({ area_id: areaId, times }),
      });
      applyConfig(data.config);
      renderTreasure(data.treasure);
      beginPolling(data.job);
    } catch (error) {
      showToast(error.message, true);
      setJobControls();
    }
  });

  elements.runDungeon.addEventListener("click", async () => {
    const dungeonId = Number(elements.dungeonSelect.value);
    if (!Number.isInteger(dungeonId) || dungeonId <= 0) {
      showToast("请选择可扫荡地下城", true);
      return;
    }
    try {
      const data = await api("/api/jobs/dungeon", {
        method: "POST",
        body: JSON.stringify({ dungeon_id: dungeonId }),
      });
      applyConfig(data.config);
      renderDungeon(data.dungeon);
      renderDungeonRewards();
      beginPolling(data.job);
    } catch (error) {
      showToast(error.message, true);
      setJobControls();
    }
  });

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });

  applyConfig(state.config);
  renderDaily(state.daily);
  renderArenaStatus(null);
  renderArenaStats(null);
  renderTreasure(null);
  renderDungeon(null);
  renderDungeonRewards();
  setJobControls();
  if (state.activeJobId) {
    pollJob().then(() => {
      if (state.activeJobId) {
        state.pollTimer = window.setInterval(() => {
          pollJob().catch((error) => showToast(error.message, true));
        }, 450);
      }
    }).catch((error) => showToast(error.message, true));
  }
})();
