/* The certificate of the customer for the box, and the way back to the one the box makes itself. */

import { call } from "../../api.js";
import { reasonText, reveal } from "../../answers.js";
import { day, t } from "../../i18n.js";
import { closeDialog } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState } = owl;

export class CertificateDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('certificate.install_title')" onClose.bind="close" large="true">
            <t t-if="state.installed">
                <div class="alert alert-success" role="status">
                    <strong t-esc="t('certificate.installed', {host: state.installed.host_name})"/>
                    <div class="mt-1" t-esc="t('certificate.installed_todo', {host: state.installed.host_name})"/>
                </div>
                <dl class="viin-rows">
                    <div class="viin-row"><dt t-esc="t('certificate.subject')"/><dd t-esc="state.installed.subject"/></div>
                    <div class="viin-row"><dt t-esc="t('certificate.ends')"/><dd t-esc="day(state.installed.expiry)"/></div>
                    <div class="viin-row"><dt t-esc="t('certificate.fingerprint')"/><dd class="viin-mono" t-esc="state.installed.fingerprint"/></div>
                </dl>
            </t>
            <t t-else="">
                <p t-esc="t('certificate.intro')"/>
                <form id="certificate-form" t-on-submit.prevent="install" autocomplete="off">
                    <label class="form-label" for="certificate-host" t-esc="t('certificate.host')"/>
                    <input id="certificate-host" class="form-control" type="text" spellcheck="false" autocapitalize="off" t-model="state.host"/>
                    <div class="form-text" t-esc="t('certificate.host_help')"/>
                    <label class="form-label mt-3" for="certificate-pem" t-esc="t('certificate.pem')"/>
                    <textarea id="certificate-pem" class="form-control viin-mono" rows="5" spellcheck="false" t-model="state.certificate" placeholder="-----BEGIN CERTIFICATE-----"/>
                    <input class="form-control form-control-sm mt-1" type="file" accept=".pem,.crt,.cer" t-on-change="(event) => this.read(event, 'certificate')" t-att-aria-label="t('certificate.pick')"/>
                    <div class="form-text" t-esc="t('certificate.pem_help')"/>
                    <label class="form-label mt-3" for="certificate-key" t-esc="t('certificate.key')"/>
                    <textarea id="certificate-key" class="form-control viin-mono" rows="5" spellcheck="false" t-model="state.key" placeholder="-----BEGIN PRIVATE KEY-----"/>
                    <input class="form-control form-control-sm mt-1" type="file" accept=".pem,.key" t-on-change="(event) => this.read(event, 'key')" t-att-aria-label="t('certificate.pick')"/>
                    <div class="form-text" t-esc="t('certificate.key_help')"/>
                </form>
                <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert">
                    <div t-esc="state.error"/>
                </div>
            </t>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.close')"/>
                <button t-if="!state.installed" type="submit" form="certificate-form" class="btn btn-primary" t-att-disabled="state.busy or !ready" t-esc="t('certificate.button')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.day = day;
        this.state = useState({ host: "", certificate: "", key: "", busy: false, error: "", installed: null });
    }

    get ready() {
        return Boolean(this.state.host.trim() && this.state.certificate.trim() && this.state.key.trim());
    }

    async read(event, field) {
        const file = event.target.files && event.target.files[0];
        if (file) {
            this.state[field] = await file.text();
        }
    }

    async install() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/viin_hw_homepage/certificate", {
                method: "POST",
                params: { certificate: this.state.certificate, key: this.state.key, host_name: this.state.host.trim() },
            });
            if (answer.status === "installed") {
                this.state.installed = answer;
                this.state.key = "";
                await this.env.refresh();
            } else if (answer.status !== "locked") {
                this.state.error = reasonText("certificate", answer.reason);
                reveal();
            }
        } finally {
            this.state.busy = false;
        }
    }

    close() {
        this.state.key = "";
        closeDialog();
    }
}

export class CertificateRestoreDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('certificate.restore_title')" onClose.bind="close">
            <t t-if="state.done">
                <div class="alert alert-success mb-0" role="status" t-esc="t('certificate.restored')"/>
            </t>
            <t t-else="">
                <p class="mb-0" t-esc="t('certificate.restore_explain')"/>
                <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="state.error"/>
            </t>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.close')"/>
                <button t-if="!state.done" type="button" class="btn btn-primary" t-on-click="go" t-att-disabled="state.busy" t-esc="t('button.own_certificate_off')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.state = useState({ busy: false, error: "", done: false });
    }

    async go() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/viin_hw_homepage/certificate", { method: "POST", params: { restore: true } });
            if (answer.status === "restored") {
                this.state.done = true;
                await this.env.refresh();
            } else if (answer.status !== "locked") {
                this.state.error = reasonText("certificate", answer.reason);
            }
        } finally {
            this.state.busy = false;
        }
    }

    close() {
        closeDialog();
    }
}
