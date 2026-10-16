/* Wi-Fi: choose a network for the box, or forget the one it has. */
/* global owl */

import { call, send } from "../../api.js";
import { reasonText } from "../../answers.js";
import { t } from "../../i18n.js";
import { closeDialog } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState, onMounted } = owl;

export class WifiDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('wifi.title')" onClose.bind="close">
            <t t-if="state.joined">
                <div class="alert alert-info mb-0" role="status">
                    <strong t-esc="t('wifi.joining', {name: state.joined.name})"/>
                    <div class="mt-1" t-esc="t('wifi.joining_todo')"/>
                    <div class="mt-1" t-if="state.joined.url"><t t-esc="t('wifi.open_by_name')"/> <span class="viin-mono viin-break" t-esc="state.joined.url"/></div>
                    <div class="mt-1" t-esc="t('wifi.open_by_address')"/>
                </div>
            </t>
            <t t-else="">
                <p t-esc="t('wifi.intro')"/>
                <p t-if="state.scanning"><i class="fa fa-circle-o-notch fa-spin" aria-hidden="true"/><t t-esc="t('wifi.scanning')"/></p>
                <form id="wifi-form" t-on-submit.prevent="join" autocomplete="off">
                    <label class="form-label" for="wifi-name" t-esc="t('wifi.name')"/>
                    <input id="wifi-name" class="form-control" type="text" list="wifi-list" spellcheck="false" autocapitalize="off" t-model="state.name"/>
                    <datalist id="wifi-list"><option t-foreach="state.found" t-as="found" t-key="found" t-att-value="found"/></datalist>
                    <div class="form-text" t-if="!state.scanning and !state.found.length" t-esc="t('wifi.none_found')"/>
                    <label class="form-label mt-3" for="wifi-password" t-esc="t('wifi.password')"/>
                    <input id="wifi-password" class="form-control" type="password" autocomplete="new-password" t-model="state.password"/>
                    <div class="form-check mt-3">
                        <input class="form-check-input" type="checkbox" id="wifi-keep" t-model="state.keep"/>
                        <label class="form-check-label" for="wifi-keep" t-esc="t('wifi.keep')"/>
                    </div>
                </form>
                <div t-if="state.error" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="state.error"/>
            </t>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.close')"/>
                <t t-if="!state.joined">
                    <button type="button" class="btn btn-outline-danger" t-on-click="forget" t-att-disabled="state.busy" t-esc="t('wifi.forget')"/>
                    <button type="submit" form="wifi-form" class="btn btn-primary" t-att-disabled="state.busy or !state.name" t-esc="t('wifi.join')"/>
                </t>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.state = useState({
            scanning: true,
            found: [],
            name: "",
            password: "",
            keep: true,
            busy: false,
            error: "",
            joined: null,
        });
        onMounted(() => this.scan());
    }

    async scan() {
        const { body } = await send("/hw_posbox_homepage/wifi");
        this.state.found = Array.isArray(body)
            ? body.filter((name) => typeof name === "string")
            : [];
        this.state.scanning = false;
    }

    async join() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/hw_posbox_homepage/update_wifi", {
                method: "POST",
                params: {
                    essid: this.state.name,
                    password: this.state.password,
                    persistent: this.state.keep,
                },
            });
            if (answer.status === "success") {
                this.state.joined = { name: this.state.name, url: this.boxUrl(answer.box) };
            } else if (answer.status !== "locked") {
                this.state.error = reasonText("wifi", answer.reason || "failed");
            }
        } finally {
            this.state.busy = false;
        }
    }

    /* The way to open the box itself once it has joined; never the address of the server. */
    boxUrl(box) {
        if (!box || typeof box !== "object") {
            return "";
        }
        if (typeof box.url === "string" && box.url) {
            return box.url;
        }
        return typeof box.local_name === "string" && box.local_name
            ? `http://${box.local_name}`
            : "";
    }

    async forget() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const answer = await call("/hw_posbox_homepage/wifi_clear");
            if (answer.status === "success") {
                this.state.error = "";
                closeDialog();
            } else if (answer.status !== "locked") {
                this.state.error = reasonText("wifi", answer.reason || "failed");
            }
        } finally {
            this.state.busy = false;
        }
    }

    close() {
        closeDialog();
    }
}
