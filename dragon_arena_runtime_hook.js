/*
 * This fragment is injected into the game's existing JavaScript runtime.
 * It deliberately uses the bundle's SocketManager, protobuf codecs, battle
 * scene, and frame verification instead of implementing battle simulation.
 */
(() => {
    "use strict";

    const message = __webpack_require__(77095).lZ;
    const gameProto = __webpack_require__(26136);
    const battleProto = __webpack_require__(63965);
    const SocketManager = __webpack_require__(23307).SocketManager;

    const DEFAULTS = Object.freeze({
        start: "match",
        rounds: 10,
        index: 0,
        mercyChoiceId: 2,
        coinItemId: 0,
        pollMs: 200,
        requestTimeoutMs: 10_000,
        battleTimeoutMs: 180_000,
        battleControlDelayMs: 1_500,
        retryDelayMs: 750,
    });

    function asPositiveInt(value, fallback) {
        return Number.isInteger(value) && value > 0 ? value : fallback;
    }

    function asNonNegativeInt(value, fallback) {
        return Number.isInteger(value) && value >= 0 ? value : fallback;
    }

    function formatSigned(value) {
        const number = Number(value || 0);
        return number >= 0 ? `+${number}` : `${number}`;
    }

    class DragonArenaRunner {
        constructor() {
            this.running = false;
            this.phase = "idle";
            this.options = { ...DEFAULTS };
            this.timer = undefined;
            this.currentIndex = 0;
            this.completed = 0;
            this.attempted = new Set();
            this.requestedAt = 0;
            this.nextActionAt = 0;
            this.infoBeforeRequest = undefined;
            this.matchDataBeforeRequest = undefined;
            this.resultHandled = false;
            this.choiceSentAt = 0;
            this.activeBattleScene = undefined;
            this.battleEnteredAt = 0;
            this.battleControlAttempts = 0;
            this.nextBattleControlAt = 0;
            this.lastWaitLogAt = 0;
            this.logger = console.log.bind(console);
        }

        setLogger(logger) {
            if (typeof logger !== "function") {
                throw new Error("logger must be a function");
            }
            this.logger = logger;
        }

        log(messageText) {
            this.logger(`[龙痕竞技场] ${messageText}`);
        }

        context() {
            if (typeof gameData !== "function") {
                throw new Error("gameData runtime entry is unavailable");
            }
            const data = gameData();
            const arena = data?.dragonArena;
            const socket = SocketManager.instance;
            if (!data || !arena || !socket) {
                throw new Error("game runtime is not ready");
            }
            return { data, arena, socket };
        }

        normalizeOptions(options) {
            const next = { ...DEFAULTS, ...options };
            if (next.start !== "match" && next.start !== "challenge") {
                throw new Error('start must be "match" or "challenge"');
            }
            next.rounds = asNonNegativeInt(next.rounds, DEFAULTS.rounds);
            next.index = asNonNegativeInt(next.index, 0);
            next.mercyChoiceId = asPositiveInt(next.mercyChoiceId, DEFAULTS.mercyChoiceId);
            next.coinItemId = asNonNegativeInt(next.coinItemId, 0);
            next.pollMs = Math.max(100, asPositiveInt(next.pollMs, DEFAULTS.pollMs));
            next.requestTimeoutMs = Math.max(
                1_000,
                asPositiveInt(next.requestTimeoutMs, DEFAULTS.requestTimeoutMs),
            );
            next.battleTimeoutMs = Math.max(
                10_000,
                asPositiveInt(next.battleTimeoutMs, DEFAULTS.battleTimeoutMs),
            );
            next.battleControlDelayMs = Math.max(
                250,
                asPositiveInt(next.battleControlDelayMs, DEFAULTS.battleControlDelayMs),
            );
            next.retryDelayMs = Math.max(100, asPositiveInt(next.retryDelayMs, DEFAULTS.retryDelayMs));
            return next;
        }

        start(options = {}) {
            if (this.running) {
                this.stop();
            }
            this.options = this.normalizeOptions(options);
            this.context();
            this.running = true;
            this.phase = "initial-info";
            this.currentIndex = 0;
            this.completed = 0;
            this.attempted.clear();
            this.resultHandled = false;
            this.activeBattleScene = undefined;
            this.battleControlAttempts = 0;
            this.infoBeforeRequest = undefined;
            this.matchDataBeforeRequest = undefined;
            this.lastWaitLogAt = 0;
            this.nextActionAt = Date.now();
            this.requestInfo("启动");
            this.timer = setInterval(() => this.tick(), this.options.pollMs);
            this.log(
                `已启动：起点=${this.options.start === "match" ? "寻找对手" : "挑战"}，` +
                    `轮数=${this.options.rounds || "持续"}，仁慈选项=${this.options.mercyChoiceId}。`,
            );
            return this.status();
        }

        stop(logStop = true) {
            if (this.timer !== undefined) {
                clearInterval(this.timer);
                this.timer = undefined;
            }
            const wasRunning = this.running;
            this.running = false;
            this.phase = "idle";
            if (wasRunning && logStop) {
                this.log(`已停止：完成=${this.completed}。`);
            }
            return this.status();
        }

        status() {
            return {
                running: this.running,
                phase: this.phase,
                completed: this.completed,
                currentIndex: this.currentIndex,
                coinItemId: this.options.coinItemId,
            };
        }

        send(socket, id, payload, codec) {
            socket.sendMsg(id, payload, codec);
        }

        requestInfo(reason) {
            const { arena, socket } = this.context();
            this.infoBeforeRequest = arena.GetInfo();
            this.send(socket, message.Scararena_info, null, null);
            this.requestedAt = Date.now();
            this.log(`发送信息查询：${reason}。`);
        }

        requestMatch() {
            const { arena, socket } = this.context();
            this.matchDataBeforeRequest = arena.GetInfo()?.mdata;
            this.send(socket, message.Scararena_match, null, null);
            this.phase = "waiting-match";
            this.requestedAt = Date.now();
            this.log("发送寻找对手。");
        }

        requestChallenge(index) {
            const { socket } = this.context();
            this.currentIndex = index;
            this.resultHandled = false;
            this.activeBattleScene = undefined;
            this.battleControlAttempts = 0;
            this.send(socket, message.Scararena_challenge, { index }, gameProto.i_0);
            this.phase = "waiting-challenge";
            this.requestedAt = Date.now();
            this.log(`发送挑战：序号=${index}。`);
        }

        requestMercy() {
            const { socket } = this.context();
            this.send(
                socket,
                message.Scararena_winchoice,
                { choiceid: this.options.mercyChoiceId },
                gameProto.OOl,
            );
            this.phase = "waiting-choice";
            this.choiceSentAt = Date.now();
            this.log(`发送仁慈选择：choiceid=${this.options.mercyChoiceId}。`);
        }

        configureBattle() {
            const { socket } = this.context();
            this.send(
                socket,
                message.Battle_C2S_setTimescale,
                { timescale: 3 },
                battleProto.cl,
            );
            this.send(
                socket,
                message.Battle_C2S_auto_unique_skill,
                { auto: true },
                battleProto.NS,
            );
            this.send(
                socket,
                message.Battle_C2S_auto_artifact_skill,
                { auto: true },
                battleProto.NS,
            );
            this.battleControlAttempts += 1;
            this.log(
                `战斗控制已发送：x3、自动角色技能、自动圣物技能（第 ${this.battleControlAttempts} 次）。`,
            );
        }

        selectCandidate(info) {
            const opponents = info?.mdata || [];
            if (this.options.index > 0 && !this.attempted.has(this.options.index)) {
                const opponent = opponents[this.options.index - 1];
                if (opponent && !opponent.challenge) {
                    return this.options.index;
                }
            }
            for (let offset = 0; offset < opponents.length; offset += 1) {
                const index = offset + 1;
                if (!opponents[offset].challenge && !this.attempted.has(index)) {
                    return index;
                }
            }
            return 0;
        }

        scene() {
            try {
                return typeof sceneMgr === "function" ? sceneMgr().currentScene : undefined;
            } catch (_error) {
                return undefined;
            }
        }

        isBattleScene(scene) {
            return Boolean(scene && scene.scenePath === "Battle");
        }

        logCoin(data) {
            const itemId = this.options.coinItemId;
            if (!itemId) {
                return;
            }
            try {
                const amount = data.storage.getItemNum(itemId);
                this.log(`当前龙痕币数量：${amount}（物品 ID ${itemId}）。`);
            } catch (error) {
                this.log(`读取龙痕币失败：${error.message || error}。`);
            }
        }

        finishRound(reason) {
            const { arena } = this.context();
            this.completed += 1;
            this.currentIndex = 0;
            this.resultHandled = false;
            this.activeBattleScene = undefined;
            this.battleControlAttempts = 0;
            if (this.options.rounds > 0 && this.completed >= this.options.rounds) {
                this.log(`达到轮数上限：${reason}。`);
                this.stop(false);
                return;
            }
            arena.ClearResult();
            this.phase = "waiting-resume";
            this.nextActionAt = Date.now() + this.options.retryDelayMs;
            this.log(`继续下一轮：${reason}。`);
        }

        handleResult(data, arena, info) {
            if (!this.currentIndex || this.resultHandled) {
                return false;
            }
            const result = arena.GetResult();
            if (!result || typeof result.win !== "boolean") {
                return false;
            }
            this.resultHandled = true;
            const index = result.index || this.currentIndex;
            const score = Number(result.score || info?.score || 0);
            const addscore = Number(result.addscore || 0);
            const daily = Number(result.DailyRewardnum || info?.DailyRewardnum || 0);
            this.log(
                `战斗完成：序号=${index}，胜利=${result.win ? "是" : "否"}，` +
                    `积分=${score}（${formatSigned(addscore)}），龙痕币日计数=${daily}。`,
            );
            this.logCoin(data);
            if (!result.win) {
                this.attempted.add(index);
            }
            if (result.win && (result.ischoice || result.choiceid || info?.ischoice)) {
                this.requestMercy();
            } else {
                this.finishRound(result.win ? "无额外选择" : "战斗失败");
            }
            return true;
        }

        updateBattleState() {
            const scene = this.scene();
            const now = Date.now();
            if (!this.isBattleScene(scene)) {
                if (
                    this.phase === "waiting-battle" &&
                    now - this.requestedAt > this.options.battleTimeoutMs
                ) {
                    this.log("普通战斗等待超时，等待原客户端退出战斗场景后继续。" );
                    this.phase = "waiting-battle-exit";
                }
                if (this.phase === "waiting-battle-exit") {
                    this.finishRound("战斗场景已退出");
                }
                return;
            }

            if (this.activeBattleScene !== scene) {
                this.activeBattleScene = scene;
                this.battleEnteredAt = now;
                this.battleControlAttempts = 0;
                this.nextBattleControlAt = now + this.options.battleControlDelayMs;
                this.log(
                    `进入原生战斗场景：战斗ID=${scene.battleId || "未提供"}，` +
                        "由原客户端处理 18010、战斗帧与哈希校验。",
                );
            }

            if (
                now >= this.nextBattleControlAt &&
                this.battleControlAttempts < 2
            ) {
                this.configureBattle();
                this.nextBattleControlAt = now + 2_000;
            }

            if (
                now - this.requestedAt > this.options.battleTimeoutMs &&
                now - this.lastWaitLogAt > 5_000
            ) {
                this.lastWaitLogAt = now;
                this.log("战斗仍在进行，继续等待原客户端结算。" );
            }
        }

        handleChallengeTimeout() {
            this.attempted.add(this.currentIndex);
            this.log(`挑战序号=${this.currentIndex} 未进入战斗，按失败继续。`);
            this.finishRound("挑战未启动");
        }

        tick() {
            if (!this.running) {
                return;
            }
            try {
                const { data, arena } = this.context();
                const info = arena.GetInfo();
                const now = Date.now();

                if (!info) {
                    if (now - this.requestedAt > this.options.requestTimeoutMs) {
                        this.requestInfo("等待初始竞技场数据");
                    }
                    return;
                }

                if (this.handleResult(data, arena, info)) {
                    return;
                }

                if (this.phase === "waiting-choice") {
                    if (!info.ischoice && now - this.choiceSentAt >= this.options.pollMs) {
                        this.log(
                            `仁慈结算完成：积分=${info.score || 0}，` +
                                `龙痕币日计数=${info.DailyRewardnum || 0}。`,
                        );
                        this.logCoin(data);
                        this.finishRound("仁慈选择完成");
                        return;
                    }
                    if (now - this.choiceSentAt > this.options.requestTimeoutMs) {
                        this.log("仁慈结算等待超时，继续下一轮。" );
                        this.finishRound("仁慈结算超时");
                    }
                    return;
                }

                if (
                    this.phase === "waiting-challenge" ||
                    this.phase === "waiting-battle" ||
                    this.phase === "waiting-battle-exit"
                ) {
                    const scene = this.scene();
                    if (this.isBattleScene(scene) || arena.matchData) {
                        this.phase = "waiting-battle";
                        this.updateBattleState();
                        return;
                    }
                    if (now - this.requestedAt > this.options.requestTimeoutMs) {
                        this.handleChallengeTimeout();
                    }
                    return;
                }

                if (this.phase === "initial-info") {
                    if (this.options.start === "match") {
                        this.requestMatch();
                    } else {
                        this.phase = "ready";
                    }
                    return;
                }

                if (this.phase === "waiting-match") {
                    if (info.mdata !== this.matchDataBeforeRequest) {
                        this.attempted.clear();
                        this.phase = "ready";
                        this.log(`匹配完成：候选数=${info.mdata?.length || 0}。`);
                    } else if (now - this.requestedAt > this.options.requestTimeoutMs) {
                        this.log("寻找对手等待超时，继续使用当前候选列表。" );
                        this.phase = "ready";
                    }
                    return;
                }

                if (this.phase === "waiting-resume") {
                    if (now >= this.nextActionAt) {
                        this.requestInfo("进入下一轮");
                        this.phase = "waiting-refresh";
                    }
                    return;
                }

                if (this.phase === "waiting-refresh") {
                    if (info !== this.infoBeforeRequest) {
                        this.phase = "ready";
                    } else if (now - this.requestedAt > this.options.requestTimeoutMs) {
                        this.log("竞技场信息刷新超时，继续使用当前候选列表。" );
                        this.phase = "ready";
                    }
                    return;
                }

                if (this.phase !== "ready" || now < this.nextActionAt) {
                    return;
                }

                const candidate = this.selectCandidate(info);
                if (candidate > 0) {
                    this.requestChallenge(candidate);
                    return;
                }

                this.requestMatch();
            } catch (error) {
                this.log(`运行异常：${error.message || error}。`);
                this.stop(false);
            }
        }
    }

    const runner = new DragonArenaRunner();
    globalThis.DragonArenaAutomation = Object.freeze({
        start: options => runner.start(options),
        stop: () => runner.stop(),
        status: () => runner.status(),
        setLogger: logger => runner.setLogger(logger),
    });
})();
