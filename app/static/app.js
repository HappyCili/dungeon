(() => {
  "use strict";

  const initialState = JSON.parse(document.getElementById("initial-state").textContent);
  const state = {
    config: initialState.config,
    daily: initialState.daily,
    arena: null,
    treasure: null,
    treasureFarm: null,
    dungeon: null,
    dungeonRewards: [],
    dungeonDraw: null,
    abyss: null,
    activeJobId: initialState.config.active_job_id,
    lastSequence: 0,
    pollTimer: null,
    toastTimer: null,
    activeTab: "daily",
    tabRefreshGeneration: 0,
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
    treasureClearedResult: document.getElementById("treasure-cleared-result"),
    treasureFarmArea: document.getElementById("treasure-farm-area"),
    treasureFarmHearth: document.getElementById("treasure-farm-hearth"),
    decreaseTreasureHearth: document.getElementById("treasure-hearth-decrease"),
    increaseTreasureHearth: document.getElementById("treasure-hearth-increase"),
    refreshTreasureFarm: document.getElementById("refresh-treasure-farm"),
    runTreasureFarm: document.getElementById("run-treasure-farm"),
    stopTreasureFarm: document.getElementById("stop-treasure-farm"),
    treasureFarmTarget: document.getElementById("treasure-farm-target"),
    treasureFarmGained: document.getElementById("treasure-farm-gained"),
    treasureFarmTotal: document.getElementById("treasure-farm-total"),
    treasureFarmKeys: document.getElementById("treasure-farm-keys"),
    treasureFarmPhase: document.getElementById("treasure-farm-phase"),
    treasureFarmTransition: document.getElementById("treasure-farm-transition"),
    dungeonSelect: document.getElementById("dungeon-select"),
    refreshDungeon: document.getElementById("refresh-dungeon"),
    runDungeon: document.getElementById("run-dungeon"),
    stopDungeon: document.getElementById("stop-dungeon"),
    dungeonName: document.getElementById("dungeon-name"),
    dungeonHighestScore: document.getElementById("dungeon-highest-score"),
    dungeonDrawProgress: document.getElementById("dungeon-draw-progress"),
    dungeonRewardSummary: document.getElementById("dungeon-reward-summary"),
    dungeonRewardList: document.getElementById("dungeon-reward-list"),
    abyssMaxRounds: document.getElementById("abyss-max-rounds"),
    decreaseAbyssRounds: document.getElementById("abyss-rounds-decrease"),
    increaseAbyssRounds: document.getElementById("abyss-rounds-increase"),
    abyssAutoBuff: document.getElementById("abyss-auto-buff"),
    refreshAbyss: document.getElementById("refresh-abyss"),
    runAbyss: document.getElementById("run-abyss"),
    stopAbyss: document.getElementById("stop-abyss"),
    abyssSeason: document.getElementById("abyss-season"),
    abyssPassLevel: document.getElementById("abyss-pass-level"),
    abyssNextLevel: document.getElementById("abyss-next-level"),
    abyssBuff: document.getElementById("abyss-buff"),
    abyssWinLoss: document.getElementById("abyss-win-loss"),
    abyssCompleted: document.getElementById("abyss-completed"),
    abyssStage: document.getElementById("abyss-stage"),
    abyssLastResult: document.getElementById("abyss-last-result"),
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

  function gameSessionConfigured() {
    return state.config.connection.status === "available" && Boolean(state.config.zone.id);
  }

  function applyConfig(config) {
    state.config = config;
    elements.username.value = config.account.username;
    elements.rememberPassword.checked = config.account.remember_password;
    elements.password.required = !config.account.password_configured;
    elements.password.placeholder = config.account.password_configured
      ? "已记住密码，可直接登录"
      : "";
    elements.rounds.value = String(config.arena.rounds);
    elements.refreshOnExhaustion.checked = config.arena.refresh_on_exhaustion;
    if (elements.abyssMaxRounds && config.abyss) {
      elements.abyssMaxRounds.value = String(config.abyss.max_rounds ?? 0);
    }
    if (elements.abyssAutoBuff && config.abyss) {
      elements.abyssAutoBuff.checked = Boolean(config.abyss.auto_buff);
    }
    elements.treasureTimes.value = String(config.treasure.times);
    if (elements.treasureFarmHearth) {
      elements.treasureFarmHearth.value = String(config.treasure.farm_target_hearth || 100);
    }
    if (elements.treasureFarmTarget) {
      elements.treasureFarmTarget.textContent = String(config.treasure.farm_target_hearth || 100);
    }
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
    const clearedResult = treasure?.cleared_result;
    if (elements.treasureClearedResult) {
      if (clearedResult?.acknowledged) {
        const areaLabel = clearedResult.area_name || "聚宝之地";
        const rewardSummary = clearedResult.summary || "服务端未返回物品明细";
        elements.treasureClearedResult.textContent = `已确认未读扫荡结算 · ${areaLabel} · ${rewardSummary}`;
        elements.treasureClearedResult.hidden = false;
      } else {
        elements.treasureClearedResult.textContent = "";
        elements.treasureClearedResult.hidden = true;
      }
    }
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

  function renderTreasureFarm(farm) {
    state.treasureFarm = farm;
    if (!elements.treasureFarmArea) {
      return;
    }
    const selectedAreaId = state.config.treasure.farm_area_id;
    const fragment = document.createDocumentFragment();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    const areas = farm?.farm_areas || [];
    if (!farm || areas.length === 0) {
      placeholder.textContent = "暂无聚宝地图";
      fragment.append(placeholder);
      elements.treasureFarmArea.replaceChildren(fragment);
      elements.treasureFarmArea.disabled = true;
      if (elements.treasureFarmTransition) elements.treasureFarmTransition.textContent = "等待开始";
      return;
    }
    placeholder.textContent = "请选择任务地图";
    fragment.append(placeholder);
    areas.forEach((area) => {
      const option = document.createElement("option");
      option.value = String(area.id);
      option.textContent = `${area.name} · ${area.key_item_name}`;
      option.selected = area.id === selectedAreaId;
      fragment.append(option);
    });
    elements.treasureFarmArea.replaceChildren(fragment);
    elements.treasureFarmArea.value = areas.some((area) => area.id === selectedAreaId)
      ? String(selectedAreaId)
      : "";
    elements.treasureFarmArea.disabled = false;
    if (elements.treasureFarmTarget) {
      elements.treasureFarmTarget.textContent = String(
        state.config.treasure.farm_target_hearth || 100
      );
    }
    if (elements.treasureFarmTransition) {
      elements.treasureFarmTransition.textContent = "等待开始";
    }
  }

  function renderTreasureFarmProgress(farm) {
    if (!farm || !elements.treasureFarmGained) {
      return;
    }
    elements.treasureFarmGained.textContent = String(farm.hearth_gained ?? "--");
    elements.treasureFarmTotal.textContent = String(farm.hearth_total ?? "--");
    const keyLabel = farm.key_item_name
      ? `${farm.key_item_name} ${farm.keys_total ?? 0}`
      : String(farm.keys_total ?? "--");
    elements.treasureFarmKeys.textContent = keyLabel;
    if (elements.treasureFarmPhase) {
      elements.treasureFarmPhase.textContent = String(
        farm.phase_label || farm.phase || "等待选择节点"
      );
    }
    if (elements.treasureFarmTransition) {
      elements.treasureFarmTransition.textContent = String(
        farm.last_transition || farm.last_reset_reason || "等待开始"
      );
    }
    if (farm.target_hearth != null && elements.treasureFarmTarget) {
      elements.treasureFarmTarget.textContent = String(farm.target_hearth);
    }
  }

  function renderAbyssStats(stats) {
    if (!elements.abyssSeason) {
      return;
    }
    if (!stats) {
      elements.abyssSeason.textContent = "--";
      elements.abyssPassLevel.textContent = "--";
      elements.abyssNextLevel.textContent = "--";
      elements.abyssBuff.textContent = "--";
      elements.abyssWinLoss.textContent = "0 / 0";
      elements.abyssCompleted.textContent = "0";
      elements.abyssStage.textContent = "空闲";
      elements.abyssLastResult.textContent = "等待开始";
      return;
    }
    const seasonLabel = stats.season_name
      ? `${stats.season_name}${stats.season_open === false ? "（未开放）" : ""}`
      : (stats.season_id ? `赛季 ${stats.season_id}` : "--");
    elements.abyssSeason.textContent = seasonLabel;
    if (stats.pass_level != null || stats.max_level != null) {
      elements.abyssPassLevel.textContent = `${stats.pass_level ?? 0} / ${stats.max_level ?? 0}`;
    }
    if (stats.next_level) {
      const nextName = stats.next_name ? ` ${stats.next_name}` : "";
      elements.abyssNextLevel.textContent = `${stats.next_level}${nextName}`;
    } else if (stats.next_id === 0) {
      elements.abyssNextLevel.textContent = "已全部通关";
    } else {
      elements.abyssNextLevel.textContent = "--";
    }
    if (stats.optbuf) {
      const desc = stats.optbuf_desc ? ` · ${stats.optbuf_desc}` : "";
      elements.abyssBuff.textContent = `${stats.optbuf}${desc}`;
    } else {
      elements.abyssBuff.textContent = stats.optbuf === 0 ? "未选择" : "--";
    }
    if (stats.wins != null || stats.losses != null) {
      elements.abyssWinLoss.textContent = `${stats.wins || 0} / ${stats.losses || 0}`;
    }
    if (stats.completed_rounds != null) {
      elements.abyssCompleted.textContent = String(stats.completed_rounds);
    }
    if (stats.stage) {
      elements.abyssStage.textContent = stats.stage;
    }
    if (stats.last_result) {
      elements.abyssLastResult.textContent = stats.last_result;
    }
  }

  function renderAbyssStatus(abyss) {
    state.abyss = abyss;
    if (!abyss) {
      renderAbyssStats(null);
      return;
    }
    renderAbyssStats({
      ...abyss,
      wins: 0,
      losses: 0,
      completed_rounds: 0,
      stage: "空闲",
      last_result: "等待开始",
    });
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
    const featureLabels = {
      daily: "日常",
      arena: "竞技场",
      treasure: "聚宝扫荡",
      treasure_farm: "聚宝刷取",
      dungeon: "地下城",
      abyss: "罪者深渊",
    };
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
    const sessionReady = gameSessionConfigured();
    const dailyLoaded = Boolean(state.daily);
    const treasureLoaded = Boolean(state.treasure);
    const dungeonLoaded = Boolean(state.dungeon);
    const selectedTreasureArea = Number(elements.treasureArea.value);
    const selectedFarmArea = Number(elements.treasureFarmArea?.value || 0);
    const selectedDungeonId = Number(elements.dungeonSelect.value);
    const treasureReady = treasureLoaded
      && state.treasure.sweep.available > 0
      && state.treasure.areas.some((area) => area.id === selectedTreasureArea);
    const farmAreas = state.treasureFarm?.farm_areas || [];
    const farmReady = farmAreas.some((area) => area.id === selectedFarmArea)
      && Number(elements.treasureFarmHearth?.value || 0) > 0;
    const dungeonReady = dungeonLoaded
      && state.dungeon.dungeons.some((dungeon) => dungeon.id === selectedDungeonId);
    elements.runDaily.disabled = active || !dailyLoaded;
    elements.runArena.disabled = active || !sessionReady;
    elements.runTreasure.disabled = active || !treasureReady;
    if (elements.runTreasureFarm) {
      elements.runTreasureFarm.disabled = active || !sessionReady || !farmReady;
    }
    elements.runDungeon.disabled = active || !dungeonReady;
    if (elements.runAbyss) {
      elements.runAbyss.disabled = active || !sessionReady;
    }
    elements.stopDaily.disabled = !active;
    elements.stopArena.disabled = !active;
    elements.stopTreasure.disabled = !active;
    if (elements.stopTreasureFarm) {
      elements.stopTreasureFarm.disabled = !active;
    }
    elements.stopDungeon.disabled = !active;
    if (elements.stopAbyss) {
      elements.stopAbyss.disabled = !active;
    }
    elements.selectAvailable.disabled = active || !dailyLoaded;
    elements.clearSelection.disabled = active || !dailyLoaded;
    elements.refreshDaily.disabled = active || !sessionReady;
    elements.refreshArena.disabled = active || !sessionReady;
    elements.refreshTreasure.disabled = active || !sessionReady;
    if (elements.refreshTreasureFarm) {
      elements.refreshTreasureFarm.disabled = active;
    }
    elements.refreshDungeon.disabled = active || !sessionReady;
    if (elements.refreshAbyss) {
      elements.refreshAbyss.disabled = active || !sessionReady;
    }
    if (elements.abyssMaxRounds) {
      elements.abyssMaxRounds.disabled = active;
    }
    if (elements.abyssAutoBuff) {
      elements.abyssAutoBuff.disabled = active;
    }
    if (elements.decreaseAbyssRounds) {
      elements.decreaseAbyssRounds.disabled = active;
    }
    if (elements.increaseAbyssRounds) {
      elements.increaseAbyssRounds.disabled = active;
    }
    elements.treasureArea.disabled = active || !treasureLoaded || state.treasure.areas.length === 0;
    elements.treasureTimes.disabled = active || !treasureLoaded || state.treasure.sweep.request_limit === 0;
    elements.decreaseTreasureTimes.disabled = elements.treasureTimes.disabled;
    elements.increaseTreasureTimes.disabled = elements.treasureTimes.disabled;
    if (elements.treasureFarmArea) {
      elements.treasureFarmArea.disabled = active || farmAreas.length === 0;
    }
    if (elements.treasureFarmHearth) {
      elements.treasureFarmHearth.disabled = active;
    }
    if (elements.decreaseTreasureHearth) {
      elements.decreaseTreasureHearth.disabled = active;
    }
    if (elements.increaseTreasureHearth) {
      elements.increaseTreasureHearth.disabled = active;
    }
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
      if (event.data.farm) {
        renderTreasureFarmProgress(event.data.farm);
      }
      if (event.data.dungeon) {
        renderDungeon(event.data.dungeon);
      }
      if (event.data.abyss) {
        renderAbyssStats(event.data.abyss);
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
    if (job.result?.farm) {
      renderTreasureFarmProgress(job.result.farm);
    }
    if (job.result?.dungeon) {
      renderDungeon(job.result.dungeon);
    }
    if (job.result?.abyss) {
      renderAbyssStats(job.result.abyss);
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
    } else if (progress.farm) {
      const farm = progress.farm;
      elements.jobProgress.textContent = (
        `${farm.area_name || "聚宝刷取"} · ${farm.phase_label || farm.phase || "等待选择节点"} · `
        `炉温 +${farm.hearth_gained || 0}/`
        + `${farm.target_hearth || 0}`
      );
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
    } else if (progress.abyss) {
      const abyss = progress.abyss;
      elements.jobProgress.textContent = (
        `罪者深渊 · 通关 ${abyss.pass_level || 0}/${abyss.max_level || 0}`
        + ` · ${abyss.wins || 0} 胜 / ${abyss.losses || 0} 负`
        + ` · ${abyss.stage || ""}`
      );
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

  function normalizedFarmHearth() {
    const raw = Number(elements.treasureFarmHearth?.value || 0);
    if (!Number.isInteger(raw) || raw < 1) {
      return 1;
    }
    return Math.min(10000, raw);
  }

  async function saveTreasureFarmConfig() {
    const areaId = Number(elements.treasureFarmArea?.value || 0);
    if (!Number.isInteger(areaId) || areaId <= 0) {
      return;
    }
    const target = normalizedFarmHearth();
    if (elements.treasureFarmHearth) {
      elements.treasureFarmHearth.value = String(target);
    }
    const data = await api("/api/config/treasure-farm", {
      method: "PUT",
      body: JSON.stringify({
        farm_area_id: areaId,
        farm_target_hearth: target,
      }),
    });
    applyConfig(data.config);
  }

  async function refreshTreasureFarm() {
    const data = await api("/api/treasure/farm-catalog", { method: "GET" });
    applyConfig(data.config);
    renderTreasureFarm(data.farm);
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

  function normalizedAbyssMaxRounds() {
    const raw = Number(elements.abyssMaxRounds?.value || 0);
    if (!Number.isInteger(raw) || raw < 0) {
      return 0;
    }
    return Math.min(900, raw);
  }

  async function saveAbyssConfig() {
    if (!elements.abyssMaxRounds) {
      return;
    }
    const maxRounds = normalizedAbyssMaxRounds();
    elements.abyssMaxRounds.value = String(maxRounds);
    const data = await api("/api/config/abyss", {
      method: "PUT",
      body: JSON.stringify({
        max_rounds: maxRounds,
        auto_buff: Boolean(elements.abyssAutoBuff?.checked),
      }),
    });
    applyConfig(data.config);
  }

  async function refreshAbyss() {
    const data = await api("/api/abyss", { method: "GET" });
    applyConfig(data.config);
    renderAbyssStatus(data.abyss);
    setJobControls();
  }

  async function refreshDaily() {
    const daily = await api("/api/daily-tasks", { method: "GET" });
    renderDaily(daily);
    setJobControls();
  }

  async function refreshTab(name, { notify = false } = {}) {
    if (!gameSessionConfigured() || state.activeJobId) {
      return;
    }
    const generation = ++state.tabRefreshGeneration;
    try {
      if (name === "daily") {
        await refreshDaily();
        if (notify && generation === state.tabRefreshGeneration) {
          showToast("日常状态已刷新");
        }
      } else if (name === "arena") {
        await refreshArena();
        if (notify && generation === state.tabRefreshGeneration) {
          showToast("龙痕竞技场状态已刷新");
        }
      } else if (name === "treasure") {
        await Promise.all([refreshTreasure(), refreshTreasureFarm()]);
        if (notify && generation === state.tabRefreshGeneration) {
          showToast("聚宝之地状态已刷新");
        }
      } else if (name === "dungeon") {
        await refreshDungeon();
        if (notify && generation === state.tabRefreshGeneration) {
          showToast("地下城状态已刷新");
        }
      } else if (name === "abyss") {
        await refreshAbyss();
        if (notify && generation === state.tabRefreshGeneration) {
          showToast("罪者深渊状态已刷新");
        }
      }
    } catch (error) {
      if (generation === state.tabRefreshGeneration) {
        showToast(error.message, true);
      }
    }
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

  function activateTab(name, { autoRefresh = true } = {}) {
    state.activeTab = name;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const selected = button.dataset.tab === name;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      document.getElementById(`${button.dataset.tab}-panel`).hidden = !selected;
    });
    if (autoRefresh) {
      refreshTab(name).catch(() => {});
    }
  }

  elements.loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    elements.loginButton.disabled = true;
    elements.loginMessage.textContent = "登录中";
    renderDaily(null);
    renderArenaStatus(null);
    renderArenaStats(null);
    renderTreasure(null);
    renderTreasureFarm(null);
    renderDungeon(null);
    renderDungeonRewards();
    renderAbyssStatus(null);
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
      renderTreasureFarm(null);
      renderDungeon(null);
      renderDungeonRewards();
      renderAbyssStatus(null);
      setJobControls();
      const [daily, arena, treasure, dungeon, farm, abyss] = await Promise.all([
        api("/api/daily-tasks", { method: "GET" }),
        api("/api/arena", { method: "GET" }),
        api("/api/treasure", { method: "GET" }),
        api("/api/dungeon", { method: "GET" }),
        api("/api/treasure/farm-catalog", { method: "GET" }),
        api("/api/abyss", { method: "GET" }),
      ]);
      renderDaily(daily);
      applyConfig(arena.config);
      renderArenaStatus(arena);
      applyConfig(treasure.config);
      renderTreasure(treasure);
      applyConfig(farm.config);
      renderTreasureFarm(farm.farm);
      renderDungeon(dungeon);
      applyConfig(abyss.config);
      renderAbyssStatus(abyss.abyss);
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

  elements.refreshDaily.addEventListener("click", () => {
    refreshTab("daily", { notify: true }).catch(() => {});
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
    refreshTab("arena", { notify: true }).catch(() => {});
  });

  elements.refreshTreasure.addEventListener("click", () => {
    refreshTab("treasure", { notify: true }).catch(() => {});
  });

  if (elements.refreshTreasureFarm) {
    elements.refreshTreasureFarm.addEventListener("click", () => {
      refreshTreasureFarm()
        .then(() => showToast("刷取地图列表已刷新"))
        .catch((error) => showToast(error.message, true));
    });
  }

  elements.refreshDungeon.addEventListener("click", () => {
    refreshTab("dungeon", { notify: true }).catch(() => {});
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

  if (elements.treasureFarmArea) {
    elements.treasureFarmArea.addEventListener("change", () => {
      saveTreasureFarmConfig()
        .then(() => setJobControls())
        .catch((error) => {
          renderTreasureFarm(state.treasureFarm);
          showToast(error.message, true);
        });
    });
  }

  if (elements.decreaseTreasureHearth) {
    elements.decreaseTreasureHearth.addEventListener("click", () => {
      const next = Math.max(1, normalizedFarmHearth() - 10);
      elements.treasureFarmHearth.value = String(next);
      saveTreasureFarmConfig()
        .then(() => setJobControls())
        .catch((error) => showToast(error.message, true));
    });
  }

  if (elements.increaseTreasureHearth) {
    elements.increaseTreasureHearth.addEventListener("click", () => {
      const next = Math.min(10000, normalizedFarmHearth() + 10);
      elements.treasureFarmHearth.value = String(next);
      saveTreasureFarmConfig()
        .then(() => setJobControls())
        .catch((error) => showToast(error.message, true));
    });
  }

  if (elements.treasureFarmHearth) {
    elements.treasureFarmHearth.addEventListener("change", () => {
      const next = normalizedFarmHearth();
      elements.treasureFarmHearth.value = String(next);
      saveTreasureFarmConfig()
        .then(() => setJobControls())
        .catch((error) => showToast(error.message, true));
    });
  }

  if (elements.runTreasureFarm) {
    elements.runTreasureFarm.addEventListener("click", async () => {
      const areaId = Number(elements.treasureFarmArea.value);
      const target = normalizedFarmHearth();
      if (!Number.isInteger(areaId) || areaId <= 0) {
        showToast("请选择任务地图", true);
        return;
      }
      try {
        const data = await api("/api/jobs/treasure-farm", {
          method: "POST",
          body: JSON.stringify({
            farm_area_id: areaId,
            farm_target_hearth: target,
          }),
        });
        applyConfig(data.config);
        beginPolling(data.job);
      } catch (error) {
        showToast(error.message, true);
        setJobControls();
      }
    });
  }

  if (elements.stopTreasureFarm) {
    elements.stopTreasureFarm.addEventListener("click", () => {
      cancelActiveJob().catch(() => {});
    });
  }

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

  if (elements.refreshAbyss) {
    elements.refreshAbyss.addEventListener("click", () => {
      refreshAbyss()
        .then(() => showToast("罪者深渊状态已刷新"))
        .catch((error) => showToast(error.message, true));
    });
  }

  if (elements.decreaseAbyssRounds) {
    elements.decreaseAbyssRounds.addEventListener("click", () => {
      const next = Math.max(0, normalizedAbyssMaxRounds() - 1);
      elements.abyssMaxRounds.value = String(next);
      saveAbyssConfig().catch((error) => showToast(error.message, true));
    });
  }

  if (elements.increaseAbyssRounds) {
    elements.increaseAbyssRounds.addEventListener("click", () => {
      const next = Math.min(900, normalizedAbyssMaxRounds() + 1);
      elements.abyssMaxRounds.value = String(next);
      saveAbyssConfig().catch((error) => showToast(error.message, true));
    });
  }

  if (elements.abyssMaxRounds) {
    elements.abyssMaxRounds.addEventListener("change", () => {
      elements.abyssMaxRounds.value = String(normalizedAbyssMaxRounds());
      saveAbyssConfig().catch((error) => showToast(error.message, true));
    });
  }

  if (elements.abyssAutoBuff) {
    elements.abyssAutoBuff.addEventListener("change", () => {
      saveAbyssConfig().catch((error) => showToast(error.message, true));
    });
  }

  if (elements.runAbyss) {
    elements.runAbyss.addEventListener("click", async () => {
      const maxRounds = normalizedAbyssMaxRounds();
      try {
        const data = await api("/api/jobs/abyss", {
          method: "POST",
          body: JSON.stringify({
            max_rounds: maxRounds,
            auto_buff: Boolean(elements.abyssAutoBuff?.checked),
          }),
        });
        applyConfig(data.config);
        renderAbyssStats({
          ...(state.abyss || {}),
          wins: 0,
          losses: 0,
          completed_rounds: 0,
          stage: "连接中",
          last_result: "正在连接罪者深渊",
        });
        beginPolling(data.job);
      } catch (error) {
        showToast(error.message, true);
        setJobControls();
      }
    });
  }

  if (elements.stopAbyss) {
    elements.stopAbyss.addEventListener("click", () => {
      cancelActiveJob().catch(() => {});
    });
  }

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });

  applyConfig(state.config);
  renderDaily(state.daily);
  renderArenaStatus(null);
  renderArenaStats(null);
  renderTreasure(null);
  renderTreasureFarm(null);
  renderDungeon(null);
  renderDungeonRewards();
  renderAbyssStatus(null);
  setJobControls();
  refreshTreasureFarm().catch(() => {});
  if (state.config.connection.status === "available" && state.config.zones.length > 0) {
    elements.loginMessage.textContent = `已恢复登录 · ${state.config.zones.length} 个区服`;
  }
  if (state.activeJobId) {
    pollJob().then(() => {
      if (state.activeJobId) {
        state.pollTimer = window.setInterval(() => {
          pollJob().catch((error) => showToast(error.message, true));
        }, 450);
      }
    }).catch((error) => showToast(error.message, true));
  } else if (gameSessionConfigured()) {
    // 已登录且已选区服时，进入默认标签自动拉取最新状态。
    refreshTab(state.activeTab).catch(() => {});
  }
})();
