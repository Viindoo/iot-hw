/* The box page: header, self-test, then Server, Network, Devices, Software and Advanced. */

import { forgetProof, unlockedUntil } from "./api.js";
import { AdvancedPanel } from "./components/AdvancedPanel.js";
import { CertificateDialog, CertificateRestoreDialog } from "./components/dialog/CertificateDialog.js";
import { ClockDialog } from "./components/dialog/ClockDialog.js";
import { DisconnectDialog, RestartDialog, SshDialog } from "./components/dialog/ConfirmDialogs.js";
import { LogLevelDialog } from "./components/dialog/LogLevelDialog.js";
import { PairDialog } from "./components/dialog/PairDialog.js";
import { PrinterDialog } from "./components/dialog/PrinterDialog.js";
import { UnlockDialog } from "./components/dialog/UnlockDialog.js";
import { WifiDialog } from "./components/dialog/WifiDialog.js";
import { DevicePanel } from "./components/DevicePanel.js";
import { NetworkPanel } from "./components/NetworkPanel.js";
import { SelfTest } from "./components/SelfTest.js";
import { ServerPanel } from "./components/ServerPanel.js";
import { SoftwarePanel } from "./components/SoftwarePanel.js";
import { LANGUAGES, setLanguage, t } from "./i18n.js";
import { documentationUrl, SUPPORT_EMAIL } from "./links.js";
import { loadLayer, refresh, startPolling, stopPolling, waitForBox } from "./poll.js";
import { store } from "./store.js";

const { Component, xml, useEffect, useState, onMounted, onWillUnmount } = owl;

const DIALOGS = {
    pair: PairDialog,
    disconnect: DisconnectDialog,
    clock: ClockDialog,
    wifi: WifiDialog,
    printer: PrinterDialog,
    certificate: CertificateDialog,
    certificate_restore: CertificateRestoreDialog,
    loglevels: LogLevelDialog,
    ssh: SshDialog,
    restart: RestartDialog,
};

class Page extends Component {
    static props = {};
    static components = {
        SelfTest, ServerPanel, NetworkPanel, DevicePanel, SoftwarePanel, AdvancedPanel, UnlockDialog,
    };
    static template = xml`
        <header class="viin-header">
            <div class="viin-header-inner">
                <div class="viin-brand"><img class="viin-logo" src="/viin_hw_homepage/static/img/viindoo-logo.svg" alt="Viindoo" width="119" height="28"/> <span>IoT Box</span></div>
                <div class="viin-box-name" t-esc="boxName"/>
                <div class="viin-lang btn-group btn-group-sm" role="group" t-att-aria-label="t('header.language')">
                    <button t-foreach="languages" t-as="code" t-key="code" type="button"
                            t-attf-class="btn {{ code === store.language ? 'btn-primary' : 'btn-outline-primary' }}"
                            t-att-aria-pressed="code === store.language ? 'true' : 'false'"
                            t-on-click="() => this.choose(code)" t-esc="t('language.' + code)"/>
                </div>
            </div>
        </header>
        <div class="alert alert-danger viin-candidate" role="alert" t-if="candidate" t-esc="t('header.candidate')"/>
        <main class="viin-main">
            <div t-if="store.offline >= 2" class="alert alert-warning" role="alert">
                <strong t-esc="t('offline.title')"/>
                <div t-esc="t('offline.todo')"/>
            </div>
            <div t-if="store.notice" t-attf-class="alert alert-{{ store.notice.kind }} alert-dismissible" role="status" aria-live="polite">
                <span t-esc="store.notice.text"/>
                <button type="button" class="btn-close" t-att-aria-label="t('common.close')" t-on-click="dismiss"/>
            </div>
            <t t-if="store.status">
                <SelfTest/>
                <ServerPanel/>
                <NetworkPanel/>
                <DevicePanel/>
                <SoftwarePanel/>
                <AdvancedPanel/>
            </t>
            <p t-elif="store.offline &lt; 2" class="viin-muted" t-esc="t('selftest.reading')"/>
        </main>
        <footer class="viin-footer">
            <a t-att-href="documentation" target="_blank" rel="noopener" t-esc="t('footer.documentation')"/>
            <a t-att-href="'mailto:' + support" t-esc="t('footer.support', {email: support})"/>
        </footer>

        <t t-if="store.dialog" t-component="DIALOGS[store.dialog.name]" t-props="store.dialog.props" t-key="store.dialog.name"/>
        <UnlockDialog t-if="store.unlock"/>

        <div class="viin-overlay" t-if="store.restarting" role="alertdialog" aria-live="assertive" aria-labelledby="restart-title">
            <div class="viin-overlay-box">
                <t t-if="!store.restarting.failed">
                    <div class="spinner-border viin-spinner mb-3" role="status"><span class="visually-hidden" t-esc="t('common.loading')"/></div>
                    <h2 class="h5" id="restart-title" t-esc="t('restart.' + store.restarting.kind)"/>
                    <p t-esc="t('restart.wait')"/>
                </t>
                <t t-else="">
                    <h2 class="h5" id="restart-title" t-esc="t('restart.failed_title')"/>
                    <p t-esc="t('restart.failed_todo')"/>
                    <button type="button" class="btn btn-primary" t-on-click="reload" t-esc="t('restart.reload')"/>
                </t>
            </div>
        </div>`;

    setup() {
        this.t = t;
        this.DIALOGS = DIALOGS;
        this.languages = LANGUAGES;
        this.store = useState(store);
        useEffect(
            (open) => {
                document.body.classList.toggle("modal-open", open);
            },
            () => [Boolean(this.store.dialog || this.store.unlock)],
        );
    }

    get status() {
        return this.store.status;
    }

    get boxName() {
        return this.status ? this.status.advanced.identity.hostname : "";
    }

    get candidate() {
        return Boolean(this.status && this.status.software.candidate_banner);
    }

    get documentation() {
        return documentationUrl(this.status && this.status.software.series);
    }

    get support() {
        return SUPPORT_EMAIL;
    }

    choose(code) {
        setLanguage(code);
        store.language = code;
    }

    dismiss() {
        store.notice = null;
    }

    reload() {
        window.location.reload();
    }
}

export class Homepage extends Component {
    static props = {};
    static components = { Page };
    static template = xml`<Page t-key="store.language"/>`;

    setup() {
        this.store = useState(store);
        useEffect(
            (restarting) => {
                if (restarting && !restarting.failed) {
                    waitForBox();
                }
            },
            () => [this.store.restarting],
        );
        onMounted(async () => {
            store.unlockedUntil = unlockedUntil();
            if (!store.unlockedUntil) {
                forgetProof();
            }
            await refresh();
            await loadLayer();
            startPolling();
        });
        onWillUnmount(stopPolling);
    }
}
