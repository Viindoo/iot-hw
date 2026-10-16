/* The dialogs that only ask "are you sure" and then do one thing: disconnect, restart, give the shell password. */
/* global owl */

import { call } from "../../api.js";
import { reasonText } from "../../answers.js";
import { t } from "../../i18n.js";
import { closeDialog, store } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState } = owl;

export class DisconnectDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('disconnect.title')" onClose.bind="close">
            <p t-esc="t('disconnect.explain')"/>
            <p t-if="ownName" t-esc="ownName"/>
            <p class="mb-0" t-esc="t('disconnect.record')"/>
            <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="state.error"/>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.cancel')"/>
                <button type="button" class="btn btn-danger" t-on-click="go" t-att-disabled="state.busy" t-esc="t('button.disconnect')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.store = useState(store);
        this.state = useState({ busy: false, error: "" });
    }

    /* The box keeps the name typed for the customer's certificate only while that certificate is installed. */
    get ownName() {
        const { certificate, box_address: address } = this.store.status.advanced;
        if (!address.own_name) {
            return "";
        }
        const key =
            certificate.kind === "customer"
                ? "disconnect.own_name_kept"
                : "disconnect.own_name_forgotten";
        return t(key, { name: address.own_name });
    }

    close() {
        closeDialog();
    }

    async go() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/hw_posbox_homepage/server_clear");
            if (answer.status === "success") {
                closeDialog();
                store.restarting = { kind: "disconnected" };
            } else {
                this.state.error = reasonText("disconnect", answer.reason || answer.status);
            }
        } finally {
            this.state.busy = false;
        }
    }
}

export class RestartDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('restart.title')" onClose.bind="close">
            <p class="mb-0" t-esc="t('restart.explain')"/>
            <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="state.error"/>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.cancel')"/>
                <button type="button" class="btn btn-danger" t-on-click="go" t-att-disabled="state.busy" t-esc="t('button.restart')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.state = useState({ busy: false, error: "" });
    }

    close() {
        closeDialog();
    }

    async go() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/hw_posbox_homepage/restart_odoo_service");
            if (answer.status === "success") {
                closeDialog();
                store.restarting = { kind: "restart" };
            } else {
                this.state.error = reasonText("restart", answer.reason || answer.status);
            }
        } finally {
            this.state.busy = false;
        }
    }
}

export class SshDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('ssh.title')" onClose.bind="close">
            <t t-if="state.password">
                <p t-esc="t('ssh.given')"/>
                <p class="viin-mono fs-5 viin-break user-select-all" t-esc="state.password"/>
                <p class="viin-muted mb-0" t-esc="t('ssh.once')"/>
            </t>
            <t t-else="">
                <p t-esc="t('ssh.explain')"/>
                <p class="mb-0" t-esc="t('ssh.replaces')"/>
            </t>
            <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="state.error"/>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.close')"/>
                <button type="button" class="btn btn-primary" t-if="!state.password" t-on-click="go" t-att-disabled="state.busy" t-esc="t('ssh.button')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.state = useState({ busy: false, error: "", password: "" });
    }

    close() {
        this.state.password = "";
        closeDialog();
    }

    async go() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/hw_posbox_homepage/generate_password");
            if (
                typeof answer.password === "string" &&
                answer.password &&
                !answer.password.startsWith("Error")
            ) {
                this.state.password = answer.password;
            } else if (answer.status !== "locked") {
                this.state.error = reasonText("ssh", answer.reason || "failed");
            }
        } finally {
            this.state.busy = false;
        }
    }
}
