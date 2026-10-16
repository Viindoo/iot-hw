/* The clock of the box beside the clock of the server, and the choice to set it. */

import { call } from "../../api.js";
import { reasonText } from "../../answers.js";
import { exact, t } from "../../i18n.js";
import { closeDialog, openDialog, store } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState, onMounted } = owl;

export class ClockDialog extends Component {
    static props = { serverUrl: { type: String, optional: true }, then: { type: String, optional: true } };
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('clock.title')" onClose.bind="close">
            <p t-esc="t('clock.explain')"/>
            <form id="clock-form" t-on-submit.prevent="read" t-if="!paired" autocomplete="off">
                <label class="form-label" for="clock-server" t-esc="t('pair.server')"/>
                <input id="clock-server" class="form-control" type="text" spellcheck="false" autocapitalize="off" t-model="state.server" t-att-placeholder="t('pair.server_placeholder')"/>
            </form>
            <p t-if="state.busy" class="mt-3 mb-0"><i class="fa fa-circle-o-notch fa-spin" aria-hidden="true"/><t t-esc="t('clock.reading')"/></p>
            <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert">
                <div t-esc="state.error"/>
            </div>
            <t t-if="state.read">
                <dl class="viin-rows mt-3">
                    <div class="viin-row"><dt t-esc="t('clock.box_time')"/><dd t-esc="exact(state.read.box_time)"/></div>
                    <div class="viin-row"><dt t-esc="t('clock.server_time')"/><dd t-esc="exact(state.read.server_time)"/></div>
                    <div class="viin-row"><dt t-esc="t('clock.difference')"/><dd t-esc="t('clock.seconds', {seconds: state.read.difference})"/></div>
                </dl>
                <div t-if="!state.read.verified" class="alert alert-warning mb-0" role="alert" t-esc="t('clock.unverified')"/>
                <div t-if="state.read.synchronised" class="alert alert-info mb-0 mt-2" role="alert" t-esc="t('clock.synchronised')"/>
            </t>
            <div t-if="state.done" class="alert alert-success mt-3 mb-0" role="status" t-esc="t('clock.done')"/>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.close')"/>
                <button type="button" class="btn btn-outline-primary" t-on-click="read" t-att-disabled="state.busy or (!paired and !state.server.trim())" t-esc="t('clock.read')"/>
                <button type="button" class="btn btn-primary" t-if="state.read and !state.done and !state.read.synchronised" t-on-click="apply" t-att-disabled="state.busy" t-esc="t('clock.set')"/>
                <button type="button" class="btn btn-primary" t-if="state.done and props.then === 'pair'" t-on-click="back" t-esc="t('clock.back_to_pair')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.exact = exact;
        this.store = useState(store);
        this.state = useState({ server: this.props.serverUrl || "", busy: false, error: "", read: null, done: false });
        onMounted(() => (this.paired || this.state.server) && this.read());
    }

    get paired() {
        return this.store.status.server.paired;
    }

    params(action) {
        return { action, server_url: this.paired ? undefined : this.state.server.trim() };
    }

    async read() {
        this.state.busy = true;
        this.state.error = "";
        this.state.read = null;
        this.state.done = false;
        try {
            const answer = await call("/viin_hw/clock", { method: "POST", params: this.params("read") });
            if (answer.status === "read") {
                this.state.read = answer;
            } else {
                this.state.error = reasonText("clock", answer.reason);
            }
        } finally {
            this.state.busy = false;
        }
    }

    async apply() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/viin_hw/clock", { method: "POST", params: this.params("apply") });
            if (answer.status === "set") {
                this.state.done = true;
                await this.env.refresh(true);
            } else {
                this.state.error = reasonText("clock", answer.reason);
            }
        } finally {
            this.state.busy = false;
        }
    }

    back() {
        openDialog("pair");
    }

    close() {
        closeDialog();
    }
}
