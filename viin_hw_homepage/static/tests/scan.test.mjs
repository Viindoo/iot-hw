import assert from "node:assert/strict";
import { test } from "node:test";

import { loadStandalone } from "./load.mjs";

const { scanUntilHeard } = await loadStandalone("scan.js");

/* A radio that hears nothing for the first `silent` scans, then the networks `names`; counts the scans and the pauses. */
function radio(silent, names) {
    const seen = { scans: 0, pauses: [] };
    return {
        seen,
        scan: async () => (++seen.scans > silent ? names : []),
        wait: async (milliseconds) => {
            seen.pauses.push(milliseconds);
        },
    };
}

test("a radio that hears at once is asked once", async () => {
    const { scan, wait, seen } = radio(0, ["Workshop"]);

    assert.deepEqual(await scanUntilHeard(scan, { wait }), ["Workshop"]);
    assert.equal(seen.scans, 1);
    assert.deepEqual(seen.pauses, []);
});

test("a radio that has just come back from a join hears its networks at a later scan", async () => {
    const { scan, wait, seen } = radio(2, ["Workshop", "Office"]);

    assert.deepEqual(await scanUntilHeard(scan, { tries: 4, pause: 10, wait }), ["Workshop", "Office"]);
    assert.equal(seen.scans, 3);
    assert.deepEqual(seen.pauses, [10, 10]);
});

test("nothing is heard only when every scan heard nothing", async () => {
    const { scan, wait, seen } = radio(99, ["Never"]);

    assert.deepEqual(await scanUntilHeard(scan, { tries: 4, pause: 10, wait }), []);
    assert.equal(seen.scans, 4);
    assert.equal(seen.pauses.length, 3);
});

test("a dialog that was closed asks no more", async () => {
    const { scan, wait, seen } = radio(99, ["Never"]);

    assert.deepEqual(await scanUntilHeard(scan, { tries: 4, wait, stopped: () => true }), []);
    assert.equal(seen.scans, 0);
});
