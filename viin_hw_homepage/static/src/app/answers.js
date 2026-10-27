/* The words for an answer of the box that is not a success: what happened, and what to do next. */

import { SUPPORT_URL } from "./links.js";
import { has, t } from "./i18n.js";

/* `prefix.reason` first, then the answers every call can get, then a text that sends the installer to support. */
export function reasonText(prefix, reason, params = {}) {
    const values = { support: SUPPORT_URL, reason: reason || "", ...params };
    for (const key of [`${prefix}.${reason}`, `common.${reason}`]) {
        if (has(key)) {
            return t(key, values);
        }
    }
    return t("common.unknown_reason", values);
}

/* Bring the message that an action just raised into view, when a long dialog hides it under its buttons. */
export function reveal() {
    setTimeout(() => {
        const found = document.querySelectorAll(".viin-modal .alert-danger, .viin-modal .alert-warning");
        if (found.length) {
            found[found.length - 1].scrollIntoView({ block: "nearest" });
        }
    }, 80);
}
