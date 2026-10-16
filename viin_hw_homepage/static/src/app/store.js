/* What the page knows, shared by every component. Components read it through useState and change it through the functions below. */

import { language } from "./i18n.js";

const { reactive } = owl;

export const store = reactive({
    language: language(),
    status: null,        // /viin_hw/status
    layer: null,         // the Viindoo part of /hw_posbox_homepage/data (offered log levels)
    offline: 0,          // how many looks in a row got no answer
    dialog: null,        // {name, props}: the dialog on screen
    unlockedUntil: 0,    // when the unlock proof runs out, in milliseconds since 1970; 0 without one
    unlock: null,        // {resolve}: the settings code is being asked
    restarting: null,    // {kind, ...}: the box is restarting; the page waits for it
    notice: null,        // {kind, text}: the line shown on top of the page after an action
    pairDraft: { mode: "link", link: "", serverUrl: "", code: "" },
});

export function openDialog(name, props = {}) {
    store.dialog = { name, props };
}

export function closeDialog() {
    store.dialog = null;
}

/* A line on top of the page after an action: `kind` is a Bootstrap alert colour. */
export function setNotice(kind, text) {
    store.notice = { kind, text };
}
