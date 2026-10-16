/* Reading the box regularly, and waiting for it while it restarts. */

import { send } from "./api.js";
import { t } from "./i18n.js";
import { setNotice, store } from "./store.js";

const PERIOD = 15000;
const BUSY_PERIOD = 2500;
// The first read of the box failed: look again soon, before settling to the normal period.
const FIRST_RETRY = 3000;
const RESTART_TICK = 2000;
// A restart that has not taken the box away after this long did not happen.
const NO_RESTART_AFTER = 30000;
const GIVE_UP_AFTER = 300000;

let reading = null;
let timer = 0;

/* One look at the box. `again` asks the server to be looked at anew. */
export function refresh(again = false) {
    if (reading) {
        return reading;
    }
    reading = (async () => {
        try {
            const { body } = await send(`/viin_hw/status${again ? "?refresh=1" : ""}`);
            if (body && body.selftest) {
                store.status = body;
                store.offline = 0;
            } else {
                store.offline += 1;
            }
        } finally {
            reading = null;
        }
    })();
    return reading;
}

/* The page data of the box carries what the status does not: the log levels the box offers. */
export async function loadLayer() {
    const { body } = await send("/hw_posbox_homepage/data");
    if (body && body.viindoo) {
        store.layer = body.viindoo;
    }
}

function next() {
    const status = store.status;
    const busy = status && (status.selftest.probing || status.software.busy
        || status.selftest.lines.some((line) => line.state === "pending"));
    timer = setTimeout(async () => {
        if (!document.hidden && !store.restarting) {
            await refresh();
        }
        next();
    }, busy ? BUSY_PERIOD : (!status && store.offline === 1 ? FIRST_RETRY : PERIOD));
}

export function startPolling() {
    clearTimeout(timer);
    next();
}

export function stopPolling() {
    clearTimeout(timer);
}

const DONE = {
    paired: (restarting) => ["success", t("restart.done_paired", { server: restarting.server, name: restarting.boxName })],
    disconnected: () => ["success", t("restart.done_disconnected")],
    restart: () => ["success", t("restart.done_restart")],
    update: () => ["success", t("restart.done_update")],
};

/* Wait until the box has gone away and come back, then show the page again. */
export async function waitForBox() {
    const restarting = store.restarting;
    const started = Date.now();
    let wentDown = false;
    store.unlockedUntil = 0;
    for (;;) {
        await new Promise((resolve) => setTimeout(resolve, RESTART_TICK));
        if (store.restarting !== restarting) {
            return;
        }
        const abort = new AbortController();
        const cut = setTimeout(() => abort.abort(), 4000);
        const { body } = await send("/viin_hw/status", { signal: abort.signal });
        clearTimeout(cut);
        const answered = Boolean(body && body.selftest);
        if (!answered) {
            wentDown = true;
        } else if (wentDown || Date.now() - started > NO_RESTART_AFTER) {
            store.status = body;
            store.offline = 0;
            store.restarting = null;
            // What the box answered before it restarted stands: a pairing or a disconnection happened, restart seen or not.
            const settled = wentDown || ["paired", "disconnected"].includes(restarting.kind);
            const [kind, text] = settled ? DONE[restarting.kind](restarting) : ["warning", t("restart.not_restarted")];
            setNotice(kind, text);
            loadLayer();
            return;
        }
        if (Date.now() - started > GIVE_UP_AFTER) {
            restarting.failed = true;
            return;
        }
    }
}
