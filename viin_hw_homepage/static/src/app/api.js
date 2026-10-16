/* Every call to the box. A call never throws: it answers {status, reason} even when the box does not answer. */

import { store } from "./store.js";

export const PROOF_HEADER = "X-Viindoo-Unlock";
const PROOF_KEY = "viin_box_unlock";

let proof = "";
let proofUntil = 0;

try {
    const kept = JSON.parse(window.sessionStorage.getItem(PROOF_KEY) || "null");
    if (kept && kept.until > Date.now()) {
        ({ proof, until: proofUntil } = kept);
    }
} catch {
    /* No stored proof: the code is asked again. */
}

export function isUnlocked() {
    return Boolean(proof) && Date.now() < proofUntil;
}

export function unlockedUntil() {
    return isUnlocked() ? proofUntil : 0;
}

function keepProof(value, seconds) {
    proof = value;
    proofUntil = Date.now() + seconds * 1000 - 5000;
    try {
        window.sessionStorage.setItem(PROOF_KEY, JSON.stringify({ proof, until: proofUntil }));
    } catch {
        /* The proof then lasts for this page only. */
    }
}

/* One request. The unlock proof, when held, goes in its header. */
export async function send(url, { method = "GET", params = null, signal = null } = {}) {
    const headers = {};
    const options = { method, headers, cache: "no-store", signal };
    if (isUnlocked()) {
        headers[PROOF_HEADER] = proof;
    }
    if (method === "POST") {
        headers["Content-Type"] = "application/json; charset=utf-8";
        options.body = JSON.stringify({ jsonrpc: "2.0", method: "call", params: params || {} });
    }
    let response;
    try {
        response = await fetch(url, options);
    } catch (error) {
        return { body: { status: "failed", reason: error.name === "AbortError" ? "aborted" : "no_answer" }, headers: null, http: 0 };
    }
    let body = null;
    try {
        body = await response.json();
    } catch {
        body = null;
    }
    if (method === "POST" && body && typeof body === "object" && "jsonrpc" in body) {
        body = "result" in body ? body.result : null;
    }
    if (body === null || typeof body !== "object") {
        body = { status: "failed", reason: response.status === 404 ? "not_served" : "box_error" };
    }
    return { body, headers: response.headers, http: response.status };
}

/* Ask the person for the settings code; true once the box gave a proof, or needs none. */
export function askUnlock() {
    return new Promise((resolve) => {
        store.unlock = { resolve };
    });
}

/* Change a setting or read one: when the box says it is locked, ask for the settings code and try once more. */
export async function call(url, options = {}) {
    let { body } = await send(url, options);
    if (body.status === "locked" && options.locked !== false) {
        if (await askUnlock()) {
            ({ body } = await send(url, options));
        }
    }
    return body;
}

/* Exchange a settings code for the proof; the answer of the box. */
export async function unlock(code) {
    const { body, headers } = await send("/viin_hw_homepage/unlock", { method: "POST", params: { code } });
    const given = headers && headers.get(PROOF_HEADER);
    if (body.status === "unlocked" && given) {
        keepProof(given, Number(body.valid_for) || 900);
    }
    return body;
}

export function forgetProof() {
    proof = "";
    proofUntil = 0;
    try {
        window.sessionStorage.removeItem(PROOF_KEY);
    } catch {
        /* Nothing was stored. */
    }
}
