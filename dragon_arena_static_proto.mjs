#!/usr/bin/env node
/**
 * Load the generated protobuf codecs from decrypted-js/main.js without
 * starting the Unity application. This keeps packet decoding aligned with the
 * extracted client bundle.
 */

import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import vm from "node:vm";

const ROOT = resolve(new URL(".", import.meta.url).pathname);
const DEFAULT_BUNDLE = resolve(ROOT, "decrypted-js/main.js");
const RUNTIME_MARKER = "}, __webpack_module_cache__ = {}, installedChunks, installChunk;";
const nodeRequire = createRequire(import.meta.url);

const CODECS = Object.freeze({
    "game-data": [26136, "gMB"],
    "arena-info": [26136, "vq"],
    "arena-match": [26136, "cOW"],
    "arena-challenge": [26136, "i_0"],
    "arena-challenge-result": [26136, "bvP"],
    "arena-winchoice": [26136, "OOl"],
    "arena-winchoice-result": [26136, "wK$"],
    "battle-info": [26136, "Xpy"],
    "battle-start": [26136, "Vdo"],
    "battle-start-debug": [26136, "C7x"],
    "battle-start-result": [26136, "lT1"],
    "battle-end": [26136, "FO"],
    "battle-timescale": [63965, "cl"],
    "battle-auto": [63965, "NS"],
    "battle-frame": [63965, "wI"],
    "battle-hash-ret": [63965, "yT"],
});

function usage() {
    return [
        "Usage:",
        "  node dragon_arena_static_proto.mjs self-test",
        "  node dragon_arena_static_proto.mjs encode CODEC JSON",
        "  node dragon_arena_static_proto.mjs decode CODEC BASE64 [--full]",
        "",
        `Codecs: ${Object.keys(CODECS).join(", ")}`,
    ].join("\n");
}

function bundleLoaderSource(bundlePrefix) {
    return `${bundlePrefix}
var __webpack_module_cache__ = {};
function __webpack_require__(id) {
    var cached = __webpack_module_cache__[id];
    if (cached !== undefined) return cached.exports;
    var module = __webpack_module_cache__[id] = { id: id, loaded: false, exports: {} };
    __webpack_modules__[id].call(module.exports, module, module.exports, __webpack_require__);
    module.loaded = true;
    return module.exports;
}
__webpack_require__.d = (exports, definition) => {
    for (var key in definition) {
        if (Object.prototype.hasOwnProperty.call(definition, key) && !Object.prototype.hasOwnProperty.call(exports, key)) {
            Object.defineProperty(exports, key, { enumerable: true, get: definition[key] });
        }
    }
};
__webpack_require__.n = module => {
    var getter = module && module.__esModule ? () => module.default : () => module;
    __webpack_require__.d(getter, { a: getter });
    return getter;
};
__webpack_require__.o = (object, property) => Object.prototype.hasOwnProperty.call(object, property);
globalThis.__dragonArenaBundleRequire = __webpack_require__;
`;
}

async function loadCodecs(bundlePath = DEFAULT_BUNDLE) {
    const source = await readFile(bundlePath, "utf8");
    const markerIndex = source.indexOf(RUNTIME_MARKER);
    if (markerIndex < 0) {
        throw new Error("Could not locate the webpack runtime marker");
    }
    const prefix = source.slice(0, markerIndex + 1);
    const sandbox = {
        console,
        require: nodeRequire,
        Buffer,
        Uint8Array,
        ArrayBuffer,
        DataView,
        TextEncoder,
        TextDecoder,
        setTimeout,
        clearTimeout,
    };
    vm.createContext(sandbox);
    vm.runInContext(bundleLoaderSource(prefix), sandbox, {
        filename: bundlePath,
        timeout: 5_000,
    });
    const getModule = sandbox.__dragonArenaBundleRequire;
    return {
        game: getModule(26136),
        battle: getModule(63965),
    };
}

function getCodec(modules, name) {
    const descriptor = CODECS[name];
    if (!descriptor) {
        throw new Error(`Unknown codec: ${name}`);
    }
    const [moduleId, exportName] = descriptor;
    const module = moduleId === 26136 ? modules.game : modules.battle;
    const codec = module[exportName];
    if (!codec?.encode || !codec?.decode) {
        throw new Error(`Bundle codec is unavailable: ${name}/${exportName}`);
    }
    return codec;
}

function normalizeBytes(value) {
    if (Array.isArray(value)) {
        return value.map(normalizeBytes);
    }
    if (!value || typeof value !== "object") {
        return value;
    }
    if (typeof value.$bytes === "string") {
        return Uint8Array.from(Buffer.from(value.$bytes, "base64"));
    }
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, normalizeBytes(entry)]));
}

function printable(value, full, depth = 0) {
    if (value instanceof Uint8Array) {
        return full ? { $bytes: Buffer.from(value).toString("base64") } : `<bytes:${value.length}>`;
    }
    if (Array.isArray(value)) {
        if (!full && depth >= 3) {
            return `<array:${value.length}>`;
        }
        return value.map(entry => printable(entry, full, depth + 1));
    }
    if (!value || typeof value !== "object") {
        return value;
    }
    if (!full && depth >= 4) {
        return "<object>";
    }
    return Object.fromEntries(
        Object.entries(value).map(([key, entry]) => [key, printable(entry, full, depth + 1)]),
    );
}

function assert(condition, message) {
    if (!condition) {
        throw new Error(message);
    }
}

async function runSelfTest() {
    const modules = await loadCodecs();
    const challenge = getCodec(modules, "arena-challenge");
    const choice = getCodec(modules, "arena-winchoice");
    const timescale = getCodec(modules, "battle-timescale");
    const auto = getCodec(modules, "battle-auto");
    const start = getCodec(modules, "battle-start");

    assert(Buffer.from(challenge.encode({ index: 3 }).finish()).equals(Buffer.from([8, 3])), "challenge codec mismatch");
    assert(Buffer.from(choice.encode({ choiceid: 2 }).finish()).equals(Buffer.from([8, 2])), "choice codec mismatch");
    assert(Buffer.from(timescale.encode({ timescale: 3 }).finish()).equals(Buffer.from([8, 3])), "timescale codec mismatch");
    assert(Buffer.from(auto.encode({ auto: true }).finish()).equals(Buffer.from([8, 1])), "auto codec mismatch");
    const decoded = start.decode(start.encode({
        id: 91,
        team: { 1001: { x: 1, y: 2 } },
        eteam: { 7: { x: 3, y: 4 } },
        extra: { auto: true, speed: 3, items: [] },
    }).finish());
    assert(
        decoded.id === 91 &&
            decoded.team[1001].x === 1 &&
            decoded.eteam[7].y === 4 &&
            decoded.extra.auto === true &&
            decoded.extra.speed === 3,
        "battle start codec mismatch",
    );
    console.log("Static bundle protobuf self-test passed");
}

async function main(argv) {
    const [command, codecName, argument, ...rest] = argv;
    if (!command || command === "--help" || command === "-h") {
        console.log(usage());
        return;
    }
    if (command === "self-test") {
        await runSelfTest();
        return;
    }
    if (command !== "encode" && command !== "decode") {
        throw new Error(`Unknown command: ${command}`);
    }
    if (!codecName || !argument) {
        throw new Error(`${command} requires CODEC and data`);
    }
    const modules = await loadCodecs();
    const codec = getCodec(modules, codecName);
    if (command === "encode") {
        const value = normalizeBytes(JSON.parse(argument));
        console.log(Buffer.from(codec.encode(value).finish()).toString("base64"));
        return;
    }
    const full = rest.includes("--full");
    const input = Uint8Array.from(Buffer.from(argument, "base64"));
    console.log(JSON.stringify(printable(codec.decode(input), full), null, 2));
}

if (import.meta.url === `file://${process.argv[1]}`) {
    main(process.argv.slice(2)).catch(error => {
        console.error(`Static protobuf tool failed: ${error.message || error}`);
        process.exitCode = 1;
    });
}

export { CODECS, getCodec, loadCodecs };
