import assert from "node:assert/strict";
import { test } from "node:test";

import { loadStandalone } from "./load.mjs";

const { scanUntilHeard, listedNetworks } = await loadStandalone("scan.js");

/* A box whose answers come from `script`, one per scan (a list of names, or null for no answer), then `rest` for ever;
   a clock that only the pauses move. */
function box(script, rest = null) {
    const seen = { scans: 0, clock: 0 };
    return {
        seen,
        scan: async () => (seen.scans < script.length ? script[seen.scans++] : (seen.scans++, rest)),
        wait: async (milliseconds) => {
            seen.clock += milliseconds;
        },
        now: () => seen.clock,
    };
}

test("a box that answers with networks at once is asked once", async () => {
    const { scan, wait, now, seen } = box([["Workshop"]]);

    assert.deepEqual(await scanUntilHeard(scan, { wait, now }), { names: ["Workshop"], heardNothing: false });
    assert.equal(seen.scans, 1);
    assert.equal(seen.clock, 0);
});

test("a radio that hears nothing for 40 seconds gives its networks before the limit", async () => {
    const empty = Array(14).fill([]);
    const { scan, wait, now, seen } = box(empty, ["Workshop", "Office"]);

    const result = await scanUntilHeard(scan, { pause: 3000, limit: 90000, wait, now });

    assert.deepEqual(result, { names: ["Workshop", "Office"], heardNothing: false });
    assert.ok(seen.clock >= 40000 && seen.clock <= 90000);
});

test("a box that does not answer for 40 seconds is asked again and its networks are shown", async () => {
    const { scan, wait, now, seen } = box(Array(14).fill(null), ["Workshop"]);

    const result = await scanUntilHeard(scan, { pause: 3000, limit: 90000, wait, now });

    assert.deepEqual(result.names, ["Workshop"]);
    assert.ok(seen.clock >= 40000 && seen.clock <= 90000);
});

test("nothing was heard only when scans were answered and every answer was empty", async () => {
    const { scan, wait, now, seen } = box([], []);

    const result = await scanUntilHeard(scan, { pause: 3000, limit: 90000, wait, now });

    assert.deepEqual(result, { names: [], heardNothing: true });
    assert.ok(seen.clock <= 90000);
    assert.ok(seen.scans >= 2);
});

test("a box that never answers is not told as a box that heard nothing", async () => {
    const { scan, wait, now, seen } = box([], null);

    const result = await scanUntilHeard(scan, { pause: 3000, limit: 90000, wait, now });

    assert.deepEqual(result, { names: [], heardNothing: false });
    assert.ok(seen.clock <= 90000);
});

test("one empty answer among silences is still a scan that heard nothing", async () => {
    const { scan, wait, now } = box([null, [], null], null);

    assert.deepEqual(await scanUntilHeard(scan, { pause: 3000, limit: 12000, wait, now }), { names: [], heardNothing: true });
});

test("a dialog that was closed asks no more", async () => {
    const { scan, wait, now, seen } = box([], []);

    assert.deepEqual(await scanUntilHeard(scan, { wait, now, stopped: () => true }), { names: [], heardNothing: false });
    assert.equal(seen.scans, 0);
});

test("the list holds no empty name and no repeated name", () => {
    assert.deepEqual(listedNetworks(["Shop-Office", "", "Shop-Guest", "Shop-Office", "   "], ""), ["Shop-Office", "Shop-Guest"]);
});

test("three networks heard, one hidden, give two rows", () => {
    assert.deepEqual(listedNetworks(["Shop-Office", "", "Warehouse 2"], ""), ["Shop-Office", "Warehouse 2"]);
});

test("the list keeps the names that hold the typed text, whatever its case", () => {
    const heard = ["Shop-Office", "Shop-Guest", "Warehouse 2"];

    assert.deepEqual(listedNetworks(heard, "shop"), ["Shop-Office", "Shop-Guest"]);
    assert.deepEqual(listedNetworks(heard, "HOUSE"), ["Warehouse 2"]);
    assert.deepEqual(listedNetworks(heard, "  "), heard);
});

test("a typed name that is not listed leaves the list empty and is not lost", () => {
    assert.deepEqual(listedNetworks(["Shop-Office"], "Hidden-Net"), []);
});
