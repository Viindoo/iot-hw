/* The words of the self-test: what a line says, and what to do about it. */

import { SUPPORT_URL } from "./links.js";
import { day, has, t, when } from "./i18n.js";

function minutes(seconds) {
    return Math.max(1, Math.round(Math.abs(Number(seconds) || 0) / 60));
}

/* The detail that holds how many seconds ago the moment of a sentence was, by the line and reason it belongs to. */
const AGE_OF = {
    "registration.accepted": "accepted_age",
    "registration.refused": "at_age",
    "registration.unreachable": "at_age",
    "live_channel.connected": "since_age",
    "live_channel.reconnecting": "since_age",
};

/* Sentences that name a moment, and the plainer sentence they fall back to when the box gave no age. */
const TIMED = ["registration.accepted", "live_channel.connected", "live_channel.reconnecting"];

/* Sentences that end with the moment of the last attempt, when the box gave one. */
const ATTEMPTED = ["registration.refused", "registration.unreachable"];

/* The addresses the box gives browsers when nothing is wrong with them. */
const GIVEN_ADDRESS = ["own_name", "address", "name"];

/* A moment, as the reader's clock reads it, from the seconds the box says have passed since then; "" without them. */
function momentAgo(age) {
    if (typeof age !== "number" || !Number.isFinite(age) || age < 0) {
        return "";
    }
    return when(new Date(Date.now() - age * 1000).toISOString());
}

/* The values a sentence of the line may name. */
function values(line) {
    const details = line.details || {};
    const certificate = details.certificate || {};
    return {
        address: details.address || "",
        ssid: details.ssid || "",
        wifi: details.join_ssid || "",
        server: details.server || "",
        name: details.fqdn || "",
        zone: details.zone || "",
        date: day(details.not_after || certificate.expiry || ""),
        when: momentAgo(details[AGE_OF[`${line.id}.${line.reason}`]]),
        minutes: minutes(details.difference),
        count: details.count ?? 0,
        files: (details.failed || []).map((failed) => failed.file).join(", "),
        offered: details.offered || "",
        problem: details.problem || "",
        check: details.check || "",
        server_reason: details.server_reason || "",
        support: SUPPORT_URL,
    };
}

export function lineName(line) {
    return has(`line.${line.id}`) ? t(`line.${line.id}`) : line.id;
}

/* The address a browser is given, the certificate it meets there, and until when the certificate holds. */
function givenAddress(line, key) {
    const details = line.details || {};
    const address = t(key, values(line)).replace(/\.$/, "");
    const certificate = `reason.browser_address.certificate_${details.certificate_kind}`;
    if (!has(certificate)) {
        return `${address}.`;
    }
    const ends = day(details.certificate_ends || "");
    const until = ends ? `, ${t("reason.browser_address.valid_until", { date: ends })}` : "";
    const again =
        details.accept_again === true ? ` ${t("reason.browser_address.accept_again")}` : "";
    return `${address}, ${t(certificate)}${until}.${again}`;
}

/* What the line says of the box. */
export function sentence(line) {
    const name = `${line.id}.${line.reason}`;
    let key = `reason.${name}`;
    if (TIMED.includes(name) && !values(line).when) {
        key += "_no_time";
    }
    if (has(key)) {
        if (
            line.id === "browser_address" &&
            line.state === "ok" &&
            GIVEN_ADDRESS.includes(line.reason)
        ) {
            return givenAddress(line, key);
        }
        const text = t(key, values(line));
        const attempt = ATTEMPTED.includes(name) ? values(line).when : "";
        return attempt
            ? `${text} ${t("reason.registration.last_attempt", { when: attempt })}`
            : text;
    }
    if (line.reason === "box_error" && has("reason.box_error")) {
        return t("reason.box_error");
    }
    return t("reason.unknown", { reason: line.reason });
}

/* What to do, empty when the line asks for nothing. */
export function todo(line) {
    if (!line.action) {
        return "";
    }
    let key = `action.${line.action}`;
    if (line.action === "apply_update" && !values(line).offered) {
        key += "_no_release";
    }
    return has(key) ? t(key, values(line)) : t("action.unknown", { support: SUPPORT_URL });
}

export const STATE_CLASS = {
    ok: "success",
    neutral: "success",
    warning: "warning",
    failed: "danger",
    pending: "info",
};

export const STATE_ICON = {
    ok: "fa-check-circle text-success",
    neutral: "fa-check-circle text-success",
    warning: "fa-exclamation-triangle text-warning",
    failed: "fa-times-circle text-danger",
    pending: "fa-circle-o-notch fa-spin text-info",
};

export function stateWord(state) {
    return has(`state.${state}`) ? t(`state.${state}`) : state;
}
