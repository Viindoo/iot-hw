/* Add a network printer that does not announce itself. */

import { call } from "../../api.js";
import { reasonText } from "../../answers.js";
import { t } from "../../i18n.js";
import { closeDialog } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState } = owl;

const KINDS = ["receipt", "label", "office"];

export class PrinterDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('printer.title')" onClose.bind="close">
            <t t-if="state.added">
                <div class="alert alert-success mb-0" role="status">
                    <strong t-esc="t('printer.added', {queue: state.added.queue})"/>
                    <div class="mt-1" t-esc="t('printer.appears', {seconds: state.added.appears_within})"/>
                </div>
            </t>
            <t t-else="">
                <p t-esc="t('printer.intro')"/>
                <form id="printer-form" t-on-submit.prevent="add" autocomplete="off">
                    <label class="form-label" for="printer-address" t-esc="t('printer.address')"/>
                    <input id="printer-address" class="form-control" type="text" spellcheck="false" autocapitalize="off" t-model="state.address" t-att-placeholder="t('printer.address_placeholder')"/>
                    <div class="form-text" t-esc="t('printer.address_help')"/>
                    <label class="form-label mt-3" for="printer-kind" t-esc="t('printer.kind')"/>
                    <select id="printer-kind" class="form-select" t-model="state.kind">
                        <option t-foreach="kinds" t-as="kind" t-key="kind" t-att-value="kind" t-esc="t('printer.kind_' + kind)"/>
                    </select>
                    <label class="form-label mt-3" for="printer-name" t-esc="t('printer.name')"/>
                    <input id="printer-name" class="form-control" type="text" spellcheck="false" maxlength="60" t-model="state.name"/>
                    <div class="form-text" t-esc="t('printer.name_help')"/>
                </form>
                <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert">
                    <div t-esc="state.error"/>
                </div>
            </t>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.close')"/>
                <button t-if="!state.added" type="submit" form="printer-form" class="btn btn-primary" t-att-disabled="state.busy or !state.address.trim()" t-esc="t('printer.button')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.kinds = KINDS;
        this.state = useState({ address: "", kind: "receipt", name: "", busy: false, error: "", added: null });
    }

    async add() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/viin_hw_homepage/printer_add", {
                method: "POST", params: { address: this.state.address.trim(), kind: this.state.kind, name: this.state.name.trim() || undefined },
            });
            if (answer.status === "added") {
                this.state.added = answer;
                await this.env.refresh();
            } else if (answer.status !== "locked") {
                this.state.error = reasonText("printer", answer.reason, { queue: answer.queue || "" });
            }
        } finally {
            this.state.busy = false;
        }
    }

    close() {
        closeDialog();
    }
}
