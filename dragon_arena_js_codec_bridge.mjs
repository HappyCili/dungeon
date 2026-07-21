#!/usr/bin/env node
/**
 * JSONL bridge for the protobuf codecs exported by decrypted-js/main.js.
 * It intentionally loads only generated codecs; Unity battle execution stays
 * in the original game runtime.
 */

import readline from "node:readline";

import { getCodec, loadCodecs } from "./dragon_arena_static_proto.mjs";

const modules = await loadCodecs(process.env.DRAGON_ARENA_CODEC_BUNDLE || undefined);

function toWireValue(value) {
    if (Array.isArray(value)) {
        return value.map(toWireValue);
    }
    if (!value || typeof value !== "object") {
        return value;
    }
    if (typeof value.$bytes === "string") {
        return Uint8Array.from(Buffer.from(value.$bytes, "base64"));
    }
    return Object.fromEntries(
        Object.entries(value).map(([key, entry]) => [key, toWireValue(entry)]),
    );
}

function toJsonValue(value) {
    if (value instanceof Uint8Array) {
        return { $bytes: Buffer.from(value).toString("base64") };
    }
    if (Array.isArray(value)) {
        return value.map(toJsonValue);
    }
    if (!value || typeof value !== "object") {
        return value;
    }
    return Object.fromEntries(
        Object.entries(value).map(([key, entry]) => [key, toJsonValue(entry)]),
    );
}

function encode(codecName, value) {
    const codec = getCodec(modules, codecName);
    return Buffer.from(codec.encode(toWireValue(value)).finish()).toString("base64");
}

function decode(codecName, encoded) {
    const codec = getCodec(modules, codecName);
    const data = Uint8Array.from(Buffer.from(encoded, "base64"));
    return toJsonValue(codec.decode(data));
}

function encodeBattleStart(request) {
    const battleInfo = decode("battle-info", request.battleInfo);
    if (!battleInfo.id || !battleInfo.data) {
        throw new Error("Battle_info is missing id or data");
    }

    const team = Object.fromEntries(
        request.team.map(unit => [unit.heroId, { x: unit.x, y: unit.y }]),
    );
    const enemyTeam = Object.fromEntries(
        battleInfo.data.enemy.map(unit => [unit.id, { x: unit.x, y: unit.y }]),
    );
    if (!Object.keys(team).length) {
        throw new Error("Battle_C2S_start is missing player positions");
    }
    if (!Object.keys(enemyTeam).length) {
        throw new Error("Battle_info is missing enemy positions");
    }
    const value = {
        id: battleInfo.id,
        team,
        eteam: enemyTeam,
        extra: { auto: true, speed: 3, items: [] },
    };
    return encode("battle-start", value);
}

function handle(request) {
    switch (request.op) {
        case "ping":
            return { codecs: "ready" };
        case "encode":
            return { data: encode(request.codec, request.value) };
        case "decode":
            return { value: decode(request.codec, request.data) };
        case "battle-start-from-info":
            return { data: encodeBattleStart(request) };
        default:
            throw new Error(`Unknown operation: ${request.op}`);
    }
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
    if (!line.trim()) {
        continue;
    }
    let request;
    try {
        request = JSON.parse(line);
        const result = handle(request);
        process.stdout.write(`${JSON.stringify({ id: request.id, ok: true, result })}\n`);
    } catch (error) {
        process.stdout.write(
            `${JSON.stringify({
                id: request?.id ?? null,
                ok: false,
                error: error?.message || String(error),
            })}\n`,
        );
    }
}
