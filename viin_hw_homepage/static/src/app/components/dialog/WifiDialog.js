/* Wi-Fi: choose a network for the box, or forget the one it has. */
/* global owl */

import { call, send } from "../../api.js";
import { reasonText } from "../../answers.js";
import { t } from "../../i18n.js";
import { refresh } from "../../poll.js";
import { scanUntilHeard } from "../../scan.js";
import { closeDialog, store } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useEffect, useState, onMounted, onWillUnmount } = owl;

// What a join that the network refused comes to; the box says it in `network.last_join.result`.
const FAILURES = ["password_refused", "not_reached"];
// After a join is asked the page looks at the box this often (milliseconds), this many times, for the result the box
// notes before it restarts: the regular look is slower, and the box is away for a while.
const FOLLOW_PERIOD = 3000;
const FOLLOW_LOOKS = 40;

export class WifiDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('wifi.title')" onClose.bind="close">
            <t t-if="!showForm">
                <div class="alert alert-info mb-0" role="status">
                    <strong t-esc="outcome === 'joined' ? t('wifi.joined', {name: state.joined.name}) : t('wifi.joining', {name: state.joined.name})"/>
                    <div class="mt-1" t-if="cableAddress" t-esc="t('wifi.joining_cable', {address: cableAddress})"/>
                    <t t-else="">
                        <div class="mt-1" t-esc="t('wifi.joining_todo')"/>
                        <div class="mt-1" t-if="state.joined.url"><t t-esc="t('wifi.open_by_name')"/> <span class="viin-mono viin-break" t-esc="state.joined.url"/></div>
                        <div class="mt-1" t-esc="t('wifi.open_by_address')"/>
                    </t>
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
                    <div class="input-group">
                        <input id="wifi-password" class="form-control" t-att-type="state.reveal ? 'text' : 'password'" autocomplete="new-password" t-model="state.password"/>
                        <button type="button" class="btn btn-outline-secondary" aria-controls="wifi-password"
                                t-att-aria-label="state.reveal ? t('wifi.hide_password') : t('wifi.show_password')"
                                t-att-title="state.reveal ? t('wifi.hide_password') : t('wifi.show_password')"
                                t-on-click="toggleReveal">
                            <i t-attf-class="fa {{ state.reveal ? 'fa-eye-slash' : 'fa-eye' }}" aria-hidden="true"/>
                        </button>
                    </div>
                    <div class="form-check mt-3">
                        <input class="form-check-input" type="checkbox" id="wifi-keep" t-model="state.keep"/>
                        <label class="form-check-label" for="wifi-keep" t-esc="t('wifi.keep')"/>
                    </div>
                </form>
                <div t-if="failure" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="t('wifi.join_' + failure, {name: state.joined.name})"/>
                <div t-elif="state.error" class="alert alert-danger mt-3 mb-0" role="alert" t-esc="state.error"/>
            </t>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-outline-secondary" t-on-click="close" t-esc="t('common.close')"/>
                <t t-if="showForm">
                    <button type="button" class="btn btn-outline-danger" t-if="kept" t-on-click="forget" t-att-disabled="state.busy" t-esc="t('wifi.forget')"/>
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
            reveal: false,
            busy: false,
            error: "",
            joined: null,
        });
        this.store = useState(store);
        this.closed = false;
        onMounted(() => this.scan());
        onWillUnmount(() => {
            this.closed = true;
        });
        // A join the network refused sends the person back to the list, which the box hears again by then.
        useEffect(
            (failure) => {
                if (failure) {
                    this.scan();
                }
            },
            () => [this.failure],
        );
    }

    get lastJoin() {
        const status = this.store.status;
        return (status && status.network && status.network.last_join) || null;
    }

    /* What the join asked from this dialog came to, once the box has noted it; "" before. */
    get outcome() {
        const { joined } = this.state;
        const last = this.lastJoin;
        return joined && last && last.at !== joined.before ? last.result : "";
    }

    get failure() {
        return FAILURES.includes(this.outcome) ? this.outcome : "";
    }

    get showForm() {
        return !this.state.joined || Boolean(this.failure);
    }

    /* A network is kept unless the box says it is not; when it cannot tell, the way to forget stays. */
    get kept() {
        const status = this.store.status;
        return !(status && status.kept_wifi) || status.kept_wifi.card_copy !== false;
    }

    /* The address the box has on its cable, if it has one: the page stays reachable there whatever a join comes to. */
    get cableAddress() {
        const status = this.store.status;
        const wired = ((status && status.network && status.network.interfaces) || []).find(
            (found) => found.kind === "wired",
        );
        return wired && wired.addresses.length ? wired.addresses[0] : "";
    }

    toggleReveal() {
        this.state.reveal = !this.state.reveal;
    }

    async scan() {
        this.state.scanning = true;
        this.state.found = await scanUntilHeard(
            async () => {
                const { body } = await send("/hw_posbox_homepage/wifi");
                return Array.isArray(body) ? body.filter((name) => typeof name === "string") : [];
            },
            { stopped: () => this.closed },
        );
        this.state.scanning = false;
    }

    /* The box restarts once a join is over, whatever it came to: look for the result it noted until it shows. */
    async follow() {
        for (let look = 0; look < FOLLOW_LOOKS && !this.closed && !this.outcome; look++) {
            await new Promise((resolve) => setTimeout(resolve, FOLLOW_PERIOD));
            await refresh();
        }
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
                this.state.joined = {
                    name: this.state.name,
                    url: this.boxUrl(answer.box),
                    before: this.lastJoin ? this.lastJoin.at : null,
                };
                this.follow();
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
