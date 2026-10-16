/* Pairing a box again: the settings code is asked first, as every change of a connected box asks it. */

import { askUnlock, isUnlocked } from "./api.js";
import { openDialog, store } from "./store.js";

export async function pairAgain() {
    const server = store.status.server;
    if (server.locked && !isUnlocked() && !(await askUnlock())) {
        return;
    }
    openDialog("pair");
}
