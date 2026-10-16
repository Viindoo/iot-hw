/* Network: how the box is connected, and the Wi-Fi setup. */

import { t } from "../i18n.js";
import { sentence } from "../selftext.js";
import { openDialog, store } from "../store.js";

const { Component, xml, useState } = owl;

export class NetworkPanel extends Component {
    static props = {};
    static template = xml`
        <section class="viin-card" aria-labelledby="network-title" id="network">
            <div class="viin-card-head">
                <h2 id="network-title" t-esc="t('network.title')"/>
                <div class="viin-actions">
                    <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="wifi">
                        <i class="fa fa-wifi" aria-hidden="true"/><t t-esc="t('button.wifi')"/>
                    </button>
                </div>
            </div>
            <div class="viin-card-body">
                <p t-esc="sentence(line)"/>
                <dl class="viin-rows">
                    <div class="viin-row"><dt t-esc="t('network.connection')"/><dd t-esc="t('network.kind_' + network.kind)"/></div>
                    <div class="viin-row" t-if="network.ssid"><dt t-esc="t('network.wifi_name')"/><dd t-esc="network.ssid"/></div>
                    <div class="viin-row" t-if="network.address"><dt t-esc="t('network.address')"/><dd t-esc="network.address"/></div>
                    <div class="viin-row" t-if="network.gateway"><dt t-esc="t('network.gateway')"/><dd t-esc="network.gateway"/></div>
                </dl>
            </div>
        </section>`;

    setup() {
        this.t = t;
        this.sentence = sentence;
        this.store = useState(store);
    }

    get network() {
        return this.store.status.network;
    }

    get line() {
        return this.store.status.selftest.lines.find((line) => line.id === "network");
    }

    wifi() {
        openDialog("wifi");
    }
}
