#!/usr/bin/env node
/** Inject the runtime hook into a copy of the extracted game bundle. */

import { readFile, writeFile } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";

const ROOT = resolve(new URL(".", import.meta.url).pathname);
const DEFAULT_SOURCE = join(ROOT, "decrypted-js", "main.js");
const DEFAULT_HOOK = join(ROOT, "dragon_arena_runtime_hook.js");
const MARKER = "const __webpack_exports__default = __webpack_exports__.A;";
const HOOK_MARKER = "globalThis.DragonArenaAutomation";

function usage() {
    return [
        "Usage:",
        "  node inject_dragon_arena_runtime.mjs [--source PATH] [--output PATH] [--hook PATH]",
        "",
        "The default output is decrypted-js/main.dragon-arena.js. The original bundle is not changed.",
    ].join("\n");
}

function parseArgs(argv) {
    const options = { source: DEFAULT_SOURCE, hook: DEFAULT_HOOK, output: undefined };
    for (let index = 0; index < argv.length; index += 1) {
        const argument = argv[index];
        if (argument === "--help" || argument === "-h") {
            options.help = true;
            continue;
        }
        if (argument === "--source" || argument === "--output" || argument === "--hook") {
            const value = argv[index + 1];
            if (!value) {
                throw new Error(`${argument} requires a path`);
            }
            options[argument.slice(2)] = resolve(value);
            index += 1;
            continue;
        }
        throw new Error(`Unknown argument: ${argument}`);
    }
    if (!options.output) {
        const name = basename(options.source);
        const extensionOffset = name.lastIndexOf(".");
        const stem = extensionOffset >= 0 ? name.slice(0, extensionOffset) : name;
        options.output = join(dirname(options.source), `${stem}.dragon-arena.js`);
    }
    return options;
}

async function main() {
    const options = parseArgs(process.argv.slice(2));
    if (options.help) {
        console.log(usage());
        return;
    }
    if (resolve(options.source) === resolve(options.output)) {
        throw new Error("--output must differ from --source");
    }

    const [bundle, hook] = await Promise.all([
        readFile(options.source, "utf8"),
        readFile(options.hook, "utf8"),
    ]);
    if (bundle.includes(HOOK_MARKER)) {
        throw new Error(`Source already contains ${HOOK_MARKER}`);
    }
    const markerIndex = bundle.lastIndexOf(MARKER);
    if (markerIndex < 0) {
        throw new Error("Could not locate the webpack export marker");
    }

    const patched = `${bundle.slice(0, markerIndex)}\n\n/* Dragon Arena runtime hook */\n${hook}\n${bundle.slice(markerIndex)}`;
    await writeFile(options.output, patched, "utf8");
    console.log(`Wrote ${options.output}`);
}

main().catch(error => {
    console.error(`Injection failed: ${error.message || error}`);
    process.exitCode = 1;
});
