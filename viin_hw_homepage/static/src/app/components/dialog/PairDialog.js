/* Connect the box to its server with a pairing link, or an address and a code, and say what to do when it fails. */

import { call } from "../../api.js";
import { reasonText, reveal } from "../../answers.js";
import { day, exact, has, t } from "../../i18n.js";
import { closeDialog, openDialog, store } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState } = owl;

/* The server address a pairing link or a typed address names, else the text as typed. */
export function serverOf(draft) {
    const text = draft.mode === "link" ? draft.link.split("#")[0] : draft.serverUrl;
    return text.trim();
}

export class PairDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('pair.title')" onClose.bind="close" large="true">
            <form id="pair-form" t-on-submit.prevent="() => this.submit()" autocomplete="off">
                <p t-esc="t('pair.intro')"/>
                <div class="btn-group mb-3" role="group" t-att-aria-label="t('pair.title')">
                    <button type="button" t-attf-class="btn btn-sm {{ draft.mode === 'link' ? 'btn-primary' : 'btn-outline-primary' }}" t-on-click="() => this.mode('link')" t-esc="t('pair.mode_link')"/>
                    <button type="button" t-attf-class="btn btn-sm {{ draft.mode === 'separate' ? 'btn-primary' : 'btn-outline-primary' }}" t-on-click="() => this.mode('separate')" t-esc="t('pair.mode_separate')"/>
                </div>
                <t t-if="draft.mode === 'link'">
                    <label class="form-label" for="pair-link" t-esc="t('pair.link')"/>
                    <input id="pair-link" class="form-control" type="text" spellcheck="false" autocapitalize="off" t-model="draft.link" t-att-placeholder="t('pair.link_placeholder')"/>
                    <div class="form-text" t-esc="t('pair.link_help')"/>
                </t>
                <t t-else="">
                    <label class="form-label" for="pair-server" t-esc="t('pair.server')"/>
                    <input id="pair-server" class="form-control" type="text" spellcheck="false" autocapitalize="off" t-model="draft.serverUrl" t-att-placeholder="t('pair.server_placeholder')"/>
                    <div class="form-text" t-esc="t('pair.server_help')"/>
                    <label class="form-label mt-3" for="pair-code" t-esc="t('pair.code')"/>
                    <input id="pair-code" class="form-control viin-mono" type="text" spellcheck="false" autocapitalize="off" t-model="draft.code"/>
                    <div class="form-text" t-esc="t('pair.code_help')"/>
                    <div t-if="codeMissing" class="form-text text-danger" role="status" aria-live="polite">
                        <t t-esc="t('pair.code_missing.title')"/> <t t-esc="t('pair.code_missing.todo')"/>
                    </div>
                </t>
                <div t-if="insecure" class="alert alert-warning mt-3 mb-0" role="alert" t-esc="t('pair.insecure')"/>
            </form>

            <div t-if="failure" t-attf-class="alert alert-{{ failure.untrusted ? 'warning' : 'danger' }} mt-3 mb-0" role="alert">
                <strong t-esc="failure.title"/>
                <div t-if="failure.todo" class="mt-1"><t t-esc="failure.todo"/></div>
            </div>

            <t t-if="state.result and state.result.reason === 'server_certificate_untrusted'">
                <dl class="viin-rows mt-3">
                    <div class="viin-row"><dt t-esc="t('certificate.subject')"/><dd t-esc="state.result.subject"/></div>
                    <div class="viin-row"><dt t-esc="t('certificate.ends')"/><dd t-esc="day(state.result.expiry)"/></div>
                    <div class="viin-row"><dt t-esc="t('certificate.names')"/><dd t-esc="(state.result.names || []).join(', ')"/></div>
                    <div class="viin-row"><dt t-esc="t('certificate.fingerprint')"/><dd class="viin-mono" t-esc="state.result.fingerprint"/></div>
                </dl>
                <p t-if="state.result.trustable" class="mb-0" t-esc="t('pair.trust_help')"/>
                <t t-if="state.result.problem === 'private_authority'">
                    <label class="form-label mt-3" for="pair-authority" t-esc="t('pair.authority')"/>
                    <textarea id="pair-authority" class="form-control viin-mono" rows="5" spellcheck="false" t-model="state.authority" placeholder="-----BEGIN CERTIFICATE-----"/>
                    <div class="form-text" t-esc="t('pair.authority_help')"/>
                </t>
            </t>

            <t t-if="state.result and state.result.reason === 'box_clock_wrong'">
                <dl class="viin-rows mt-3">
                    <div class="viin-row"><dt t-esc="t('clock.box_time')"/><dd t-esc="exact(state.result.box_time)"/></div>
                    <div class="viin-row"><dt t-esc="t('clock.server_time')"/><dd t-esc="exact(state.result.server_time)"/></div>
                </dl>
            </t>

            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.cancel')"/>
                <t t-if="state.result and state.result.reason === 'box_clock_wrong'">
                    <button type="button" class="btn btn-primary" t-on-click="setClock" t-esc="t('button.set_clock')"/>
                </t>
                <t t-elif="state.result and state.result.reason === 'server_certificate_untrusted' and state.result.trustable">
                    <button type="button" class="btn btn-primary" t-on-click="trust" t-att-disabled="state.busy" t-esc="t('pair.trust_button')"/>
                </t>
                <t t-elif="state.result and state.result.problem === 'private_authority'">
                    <button type="button" class="btn btn-primary" t-on-click="useAuthority" t-att-disabled="state.busy or !state.authority.trim()" t-esc="t('pair.authority_button')"/>
                </t>
                <button t-else="" type="submit" form="pair-form" class="btn btn-primary" t-att-disabled="state.busy or !ready">
                    <i t-if="state.busy" class="fa fa-circle-o-notch fa-spin" aria-hidden="true"/><t t-esc="t('button.connect')"/>
                </button>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.exact = exact;
        this.day = day;
        this.store = useState(store);
        this.draft = this.store.pairDraft;
        this.state = useState({ busy: false, result: null, authority: "" });
    }

    get ready() {
        return this.draft.mode === "link" ? Boolean(this.draft.link.trim()) : Boolean(this.draft.serverUrl.trim() && this.draft.code.trim());
    }

    /* An address is typed and the code is not: Connect stays disabled, and the person reads why. */
    get codeMissing() {
        const address = this.draft.serverUrl.trim();
        if (this.draft.mode !== "separate" || !address || this.draft.code.trim()) {
            return false;
        }
        try {
            return Boolean(new URL(address.includes("://") ? address : `https://${address}`).hostname);
        } catch {
            return false;
        }
    }

    get insecure() {
        return serverOf(this.draft).toLowerCase().startsWith("http://");
    }

    /* The message the last answer asks to show: a title and what to do. */
    get failure() {
        const result = this.state.result;
        if (!result) {
            return null;
        }
        const reason = result.reason;
        const params = { box_name: result.box_name || "", server_reason: result.server_reason || "", problem: result.problem || "" };
        if (reason === "identifier_exists" && !result.box_name) {
            return { title: t("pair.identifier_exists_other.title"), todo: t("pair.identifier_exists_other.todo") };
        }
        if (reason === "server_certificate_untrusted") {
            const problem = `pair.problem_${result.problem}`;
            return {
                untrusted: true,
                title: has(problem) ? t(problem) : t("pair.problem_invalid"),
                todo: result.problem === "private_authority" ? t("pair.private_authority") : (result.trustable ? t("pair.trust_explain") : t("pair.untrusted_todo")),
            };
        }
        const key = `pair.${reason}`;
        const known = has(`${key}.title`);
        return {
            title: known ? t(`${key}.title`, params) : reasonText("pair", reason, params),
            todo: known ? t(`${key}.todo`, params) : "",
        };
    }

    mode(mode) {
        this.draft.mode = mode;
        this.state.result = null;
    }

    close() {
        closeDialog();
    }

    trust() {
        return this.submit({ trust_fingerprint: this.state.result.fingerprint, authority: this.state.authority.trim() || undefined });
    }

    useAuthority() {
        return this.submit({ authority: this.state.authority.trim() });
    }

    setClock() {
        openDialog("clock", { serverUrl: serverOf(this.draft), then: "pair" });
    }

    async submit(extra = {}) {
        if (this.state.busy || !this.ready) {
            return;
        }
        this.state.busy = true;
        try {
            const params = this.draft.mode === "link"
                ? { link: this.draft.link.trim() }
                : { server_url: this.draft.serverUrl.trim(), code: this.draft.code.trim() };
            const answer = await call("/viin_hw_homepage/pair", { method: "POST", params: { ...params, ...extra } });
            if (answer.status === "paired") {
                this.draft.link = "";
                this.draft.code = "";
                closeDialog();
                store.restarting = { kind: "paired", server: answer.server, boxName: answer.box_name };
            } else if (answer.status === "locked") {
                this.state.result = { status: "failed", reason: "locked" };
            } else {
                this.state.result = answer;
                reveal();
            }
        } finally {
            this.state.busy = false;
        }
    }
}
