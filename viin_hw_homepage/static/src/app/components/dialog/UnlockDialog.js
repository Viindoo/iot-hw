/* The settings code: asked when a change needs it, once per 15 minutes. */
/* global owl */

import { unlock, unlockedUntil } from "../../api.js";
import { reasonText } from "../../answers.js";
import { t } from "../../i18n.js";
import { store } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState } = owl;

export class UnlockDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('unlock.title')" onClose.bind="cancel">
            <p t-esc="t('unlock.explain')"/>
            <form id="unlock-form" t-on-submit.prevent="submit" autocomplete="off">
                <label class="form-label" for="settings-code" t-esc="t('unlock.label')"/>
                <input id="settings-code" class="form-control form-control-lg viin-code text-uppercase" type="text" autocomplete="off"
                       autocapitalize="characters" spellcheck="false" maxlength="24" t-model="state.code"/>
                <div class="form-text" t-esc="t('unlock.where')"/>
            </form>
            <p class="viin-muted small mt-3 mb-0" t-esc="t('unlock.no_code')"/>
            <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="state.error"/>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="cancel" t-esc="t('common.cancel')"/>
                <button type="submit" form="unlock-form" class="btn btn-primary" t-att-disabled="state.busy or !state.code.trim()" t-esc="t('unlock.button')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.state = useState({ code: "", busy: false, error: "" });
    }

    async submit() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await unlock(this.state.code.trim());
            if (answer.status === "unlocked" || answer.status === "not_locked") {
                store.unlockedUntil = unlockedUntil();
                this.finish(true);
            } else {
                this.state.error = reasonText("unlock", answer.reason);
            }
        } finally {
            this.state.busy = false;
        }
    }

    cancel() {
        this.finish(false);
    }

    finish(done) {
        const { resolve } = store.unlock;
        store.unlock = null;
        resolve(done);
    }
}
