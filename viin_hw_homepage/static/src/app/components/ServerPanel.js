/* Server: the state of the pairing, and the one main action the state offers. */
/* global owl */

import { t } from "../i18n.js";
import { pairAgain } from "../pairing.js";
import { sentence, STATE_CLASS } from "../selftext.js";
import { openDialog, store } from "../store.js";

const { Component, xml, useState } = owl;

export class ServerPanel extends Component {
    static props = {};
    static template = xml`
        <section class="viin-card" aria-labelledby="server-title" id="server">
            <div class="viin-card-head">
                <h2 id="server-title" t-esc="t('server.title')"/>
                <div class="viin-actions">
                    <t t-if="server.paired">
                        <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="pairAgain" t-esc="t('button.pair_again')"/>
                        <button type="button" class="btn btn-outline-danger btn-sm" t-on-click="() => this.open('disconnect')" t-esc="t('button.disconnect')"/>
                    </t>
                    <button t-else="" type="button" class="btn btn-primary" t-on-click="() => this.open('pair')">
                        <i class="fa fa-link" aria-hidden="true"/><t t-esc="t('button.connect')"/>
                    </button>
                </div>
            </div>
            <div class="viin-card-body">
                <t t-if="server.paired">
                    <div t-attf-class="alert alert-{{ stateClass(registration.state) }}" role="status">
                        <strong t-esc="headline"/>
                        <div t-esc="sentence(registration)"/>
                    </div>
                    <dl class="viin-rows">
                        <div class="viin-row"><dt t-esc="t('server.address')"/><dd t-esc="server.address"/></div>
                        <div class="viin-row"><dt t-esc="t('line.live_channel')"/><dd t-esc="sentence(channel)"/></div>
                        <div class="viin-row"><dt t-esc="t('server.settings')"/><dd t-esc="settingsText"/></div>
                    </dl>
                </t>
                <t t-else="">
                    <p class="mb-2"><strong t-esc="t('server.not_connected')"/></p>
                    <p t-esc="t('server.how_title')" class="mb-1"/>
                    <ol class="mb-2">
                        <li t-esc="t('server.how_1')"/>
                        <li t-esc="t('server.how_2')"/>
                        <li t-esc="t('server.how_3')"/>
                    </ol>
                    <p class="viin-muted mb-0" t-esc="t('server.kinds')"/>
                </t>
            </div>
        </section>`;

    setup() {
        this.t = t;
        this.sentence = sentence;
        this.pairAgain = pairAgain;
        this.store = useState(store);
    }

    get server() {
        return this.store.status.server;
    }

    get lines() {
        return Object.fromEntries(this.store.status.selftest.lines.map((line) => [line.id, line]));
    }

    get registration() {
        return this.lines.registration;
    }

    get channel() {
        return this.lines.live_channel;
    }

    get headline() {
        return this.registration.reason === "accepted"
            ? t("server.registered")
            : t("server.connected");
    }

    get settingsText() {
        if (!this.server.locked) {
            return t("server.settings_open");
        }
        return this.store.unlockedUntil > Date.now()
            ? t("server.settings_unlocked")
            : t("server.settings_locked");
    }

    stateClass(state) {
        return STATE_CLASS[state] || "secondary";
    }

    open(name) {
        openDialog(name);
    }
}
