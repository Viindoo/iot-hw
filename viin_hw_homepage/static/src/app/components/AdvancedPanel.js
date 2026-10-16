/* Advanced: the certificate, the address given to browsers, the logs, the shell password and the restart. */

import { day, has, t } from "../i18n.js";
import { sentence } from "../selftext.js";
import { openDialog, store } from "../store.js";

const { Component, xml, useState } = owl;

const OPEN_KEY = "viin_box_advanced";

function wasOpen() {
    try {
        return window.localStorage.getItem(OPEN_KEY) === "1";
    } catch {
        return false;
    }
}

export class AdvancedPanel extends Component {
    static props = {};
    static template = xml`
        <section class="viin-card" aria-labelledby="advanced-title" id="advanced">
            <div class="viin-card-head">
                <h2 id="advanced-title" t-esc="t('advanced.title')"/>
                <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="toggle" t-att-aria-expanded="state.open ? 'true' : 'false'" aria-controls="advanced-body">
                    <i t-attf-class="fa {{ state.open ? 'fa-chevron-up' : 'fa-chevron-down' }}" aria-hidden="true"/>
                    <t t-esc="state.open ? t('advanced.hide') : t('advanced.show')"/>
                </button>
            </div>
            <div class="viin-card-body" id="advanced-body" t-if="state.open">

                <h3 class="viin-subhead" t-esc="t('advanced.certificate')"/>
                <dl class="viin-rows">
                    <div class="viin-row"><dt t-esc="t('certificate.kind')"/><dd t-esc="t('certificate.kind_' + certificate.kind)"/></div>
                    <t t-if="certificate.kind !== 'missing'">
                        <div class="viin-row"><dt t-esc="t('certificate.subject')"/><dd t-esc="certificate.subject"/></div>
                        <div class="viin-row"><dt t-esc="t('certificate.ends')"/><dd t-esc="day(certificate.expiry)"/></div>
                        <div class="viin-row"><dt t-esc="t('certificate.names')"/><dd t-esc="certificate.names.join(', ')"/></div>
                        <div class="viin-row"><dt t-esc="t('certificate.fingerprint')"/><dd class="viin-mono" t-esc="certificate.fingerprint"/></div>
                    </t>
                    <div class="viin-row"><dt t-esc="t('viindoo_name.name')"/><dd t-esc="name.name || t('viindoo_name.none')"/></div>
                    <div class="viin-row"><dt t-esc="t('viindoo_name.ends')"/><dd t-esc="name.not_after ? day(name.not_after) : t('viindoo_name.none')"/></div>
                    <div class="viin-row"><dt t-esc="t('viindoo_name.state')"/><dd t-esc="nameState"/></div>
                    <div class="viin-row"><dt t-esc="t('viindoo_name.meaning')"/><dd t-esc="nameMeaning"/></div>
                </dl>
                <div class="viin-actions mt-2">
                    <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="() => this.open('certificate')" t-esc="t('button.own_certificate')"/>
                    <button type="button" class="btn btn-outline-primary btn-sm" t-if="certificate.kind === 'customer'" t-on-click="() => this.open('certificate_restore')" t-esc="t('button.own_certificate_off')"/>
                </div>

                <h3 class="viin-subhead" t-esc="t('advanced.address')"/>
                <dl class="viin-rows">
                    <div class="viin-row"><dt t-esc="t('address.given')"/><dd t-esc="advanced.box_address.address"/></div>
                    <div class="viin-row"><dt t-esc="t('address.kind')"/><dd t-esc="t('address.kind_' + advanced.box_address.kind)"/></div>
                </dl>
                <p class="viin-muted small" t-esc="t('address.help')"/>

                <h3 class="viin-subhead" t-esc="t('advanced.identity')"/>
                <dl class="viin-rows">
                    <div class="viin-row"><dt t-esc="t('identity.name')"/><dd t-esc="advanced.identity.hostname"/></div>
                    <div class="viin-row"><dt t-esc="t('identity.mac')"/><dd class="viin-mono" t-esc="advanced.identity.mac_address"/></div>
                    <div class="viin-row"><dt t-esc="t('identity.serial')"/><dd class="viin-mono" t-esc="advanced.identity.serial_number"/></div>
                </dl>
                <p class="viin-muted small" t-esc="t('identity.name_help')"/>

                <h3 class="viin-subhead" t-esc="t('advanced.logs')"/>
                <div class="viin-actions">
                    <a class="btn btn-outline-primary btn-sm" href="/logs" target="_blank" rel="noopener"><i class="fa fa-file-text-o" aria-hidden="true"/><t t-esc="t('button.log')"/></a>
                    <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="() => this.open('loglevels')"><i class="fa fa-sliders" aria-hidden="true"/><t t-esc="t('button.log_levels')"/></button>
                    <a class="btn btn-outline-primary btn-sm" href="/viin_hw/diagnostic"><i class="fa fa-download" aria-hidden="true"/><t t-esc="t('selftest.diagnostic')"/></a>
                </div>

                <h3 class="viin-subhead" t-esc="t('advanced.maintenance')"/>
                <div class="viin-actions">
                    <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="() => this.open('clock')"><i class="fa fa-clock-o" aria-hidden="true"/><t t-esc="t('button.set_clock')"/></button>
                    <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="() => this.open('ssh')"><i class="fa fa-key" aria-hidden="true"/><t t-esc="t('button.ssh')"/></button>
                    <button type="button" class="btn btn-outline-danger btn-sm" t-on-click="() => this.open('restart')"><i class="fa fa-power-off" aria-hidden="true"/><t t-esc="t('button.restart')"/></button>
                </div>
                <p class="viin-muted small mt-2 mb-0" t-if="store.status.server.locked" t-esc="t('advanced.locked_help')"/>
            </div>
        </section>`;

    setup() {
        this.t = t;
        this.day = day;
        this.store = useState(store);
        this.state = useState({ open: wasOpen() });
    }

    get advanced() {
        return this.store.status.advanced;
    }

    get certificate() {
        return this.advanced.certificate;
    }

    get name() {
        return this.advanced.viindoo_name;
    }

    get nameState() {
        const state = this.name.state || "none";
        return has(`viindoo_name.state_${state}`) ? t(`viindoo_name.state_${state}`) : state;
    }

    get nameMeaning() {
        const line = this.store.status.selftest.lines.find((found) => found.id === "viindoo_name");
        return sentence(line);
    }

    toggle() {
        this.state.open = !this.state.open;
        try {
            window.localStorage.setItem(OPEN_KEY, this.state.open ? "1" : "0");
        } catch {
            /* The panel then opens closed at the next visit. */
        }
    }

    open(name) {
        openDialog(name, name === "clock" ? { serverUrl: this.store.status.server.address } : {});
    }
}
