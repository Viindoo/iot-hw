/* Devices: each device with its test and its controls. */
/* global owl */

import { call, send } from "../api.js";
import { reasonText } from "../answers.js";
import { has, t } from "../i18n.js";
import { openDialog, setDeviceResult, store } from "../store.js";

const { Component, xml, useState, onWillUnmount } = owl;

const ICONS = {
    printer: "fa-print",
    scale: "fa-balance-scale",
    scanner: "fa-barcode",
    keyboard: "fa-keyboard-o",
    display: "fa-desktop",
    footswitch: "fa-hand-o-down",
};
const LISTEN_SECONDS = 30;

/* What a test of a device came to, in the language of the page now. */
function testResult(device, answer) {
    const detail = answer.detail || {};
    if (answer.status === "ok") {
        if (device.type === "scale") {
            const unit = typeof detail.unit === "string" ? detail.unit.trim() : "";
            return {
                kind: "success",
                text: unit
                    ? t("device.weight", { value: detail.value, unit })
                    : t("device.weight_no_unit", { value: detail.value }),
            };
        }
        if (detail.reason === "sent_not_followed") {
            // Neither a success nor an error: the box cannot say whether the printer took the test.
            return {
                kind: "info",
                text: t("device.test.sent_not_followed"),
                todo: t("device.test_todo.sent_not_followed"),
            };
        }
        return { kind: "success", text: t("device.test_taken") };
    }
    let reason =
        answer.status === "failed" || answer.status === "unsupported"
            ? detail.reason
            : answer.reason;
    const params = { address: detail.address || "" };
    let todoReason = reason;
    if (reason === "printer_not_reached") {
        if (detail.connection === "network" && params.address) {
            reason = "printer_not_reached_network";
            todoReason = reason;
        } else if (detail.connection !== "usb") {
            // The box cannot name how the printer is connected: no address advice and no USB claim.
            todoReason = "printer_not_reached_unknown";
        }
    }
    return {
        kind: "danger",
        text: reasonText("device.test", reason, params),
        todo: has(`device.test_todo.${todoReason}`)
            ? t(`device.test_todo.${todoReason}`, params)
            : "",
    };
}

class DeviceCard extends Component {
    static props = { device: Object };
    static template = xml`
        <div class="viin-device">
            <div class="viin-device-icon"><i t-attf-class="fa {{ icon(device) }}" aria-hidden="true"/></div>
            <div class="viin-device-main">
                <div class="viin-device-name" t-esc="device.name"/>
                <div class="viin-muted small">
                    <t t-esc="kindName(device)"/><t t-if="device.connection"> - <t t-esc="connectionName(device)"/></t>
                    <t t-if="device.manufacturer"> - <t t-esc="device.manufacturer"/></t>
                </div>
                <div class="viin-muted small" t-if="why(device)" t-esc="why(device)"/>
                <div class="viin-muted small" t-if="protocol(device)" t-esc="protocol(device)"/>
                <div class="viin-muted small" t-if="device.printer and device.printer.label_queue"
                     t-esc="t('device.label_exists', {queue: device.printer.label_queue})"/>

                <div class="viin-actions mt-2">
                    <button type="button" class="btn btn-primary btn-sm" t-if="device.test === 'box'"
                            t-on-click="() => this.runTest(device)" t-att-disabled="state.busy">
                        <i t-attf-class="fa {{ state.busy ? 'fa-circle-o-notch fa-spin' : testIcon(device) }}" aria-hidden="true"/>
                        <t t-esc="testLabel(device)"/>
                    </button>
                    <t t-if="device.test === 'page'">
                        <button type="button" class="btn btn-primary btn-sm" t-if="!state.listening" t-on-click="() => this.listen(device)">
                            <i class="fa fa-headphones" aria-hidden="true"/><t t-esc="t('device.listen')"/>
                        </button>
                        <button type="button" class="btn btn-outline-primary btn-sm" t-else="" t-on-click="() => this.stopListening(device)">
                            <i class="fa fa-stop" aria-hidden="true"/><t t-esc="t('device.stop_listening')"/>
                        </button>
                    </t>
                    <t t-if="device.keyboard and device.keyboard.can_switch">
                        <button type="button" t-attf-class="btn btn-sm {{ device.keyboard.is_scanner ? 'btn-secondary' : 'btn-outline-secondary' }}"
                                t-att-aria-pressed="device.keyboard.is_scanner ? 'true' : 'false'"
                                t-on-click="() => this.useAs(device, true)" t-esc="t('device.as_scanner')"/>
                        <button type="button" t-attf-class="btn btn-sm {{ device.keyboard.is_scanner ? 'btn-outline-secondary' : 'btn-secondary' }}"
                                t-att-aria-pressed="device.keyboard.is_scanner ? 'false' : 'true'"
                                t-on-click="() => this.useAs(device, false)" t-esc="t('device.as_keyboard')"/>
                    </t>
                    <button type="button" class="btn btn-outline-primary btn-sm" t-if="device.printer and device.printer.can_become_label and !device.printer.label_queue"
                            t-on-click="() => this.makeLabel(device)" t-att-disabled="state.busy" t-esc="t('device.make_label')"/>
                </div>

                <p class="viin-muted small mt-2 mb-0" t-if="device.type === 'display'" t-esc="t('device.display_help')"/>
                <p class="viin-muted small mt-2 mb-0" t-if="device.test === 'page' and !state.listening and !state.events.length"
                   t-esc="t('device.listen_help')"/>

                <div t-if="state.listening" class="alert alert-info viin-result" role="status" aria-live="polite">
                    <div t-esc="t('device.listening', {seconds: LISTEN_SECONDS})"/>
                    <div class="small" t-esc="t('device.listen_shared')"/>
                </div>
                <div class="viin-muted small mt-2" t-if="state.events.length" t-esc="t('device.received')"/>
                <ul class="viin-events" t-if="state.events.length">
                    <li t-foreach="state.events" t-as="event" t-key="event_index" t-esc="event"/>
                </ul>
                <t t-set="shown" t-value="result"/>
                <div t-if="shown" t-attf-class="alert alert-{{ shown.kind }} viin-result" role="status" aria-live="polite">
                    <div t-esc="shown.text"/>
                    <div t-if="shown.todo" class="mt-1"><strong><t t-esc="t('selftest.todo')"/></strong> <t t-esc="shown.todo"/></div>
                </div>
            </div>
        </div>`;

    setup() {
        this.t = t;
        this.LISTEN_SECONDS = LISTEN_SECONDS;
        this.state = useState({
            busy: false,
            testing: false,
            listening: false,
            events: [],
        });
        this.store = useState(store);
        this.abort = null;
        this.labelling = false;
        onWillUnmount(() => this.abort && this.abort.abort());
    }

    get device() {
        return this.props.device;
    }

    /* The last action on this device, said in the language of the page now. */
    get result() {
        const build = this.store.deviceResults[this.props.device.identifier];
        return build ? build() : null;
    }

    setResult(build) {
        setDeviceResult(this.props.device.identifier, build);
    }

    icon(device) {
        return ICONS[device.type] || "fa-plug";
    }

    testIcon(device) {
        return device.type === "scale" ? "fa-balance-scale" : "fa-print";
    }

    kindName(device) {
        return has(`device.kind.${device.kind}`) ? t(`device.kind.${device.kind}`) : device.kind;
    }

    connectionName(device) {
        return has(`device.connection.${device.connection}`)
            ? t(`device.connection.${device.connection}`)
            : device.connection;
    }

    why(device) {
        const printer = device.printer;
        if (!printer || !printer.why || printer.why === "unknown") {
            return "";
        }
        return t(`device.why_${printer.why}`, { evidence: printer.evidence || "" });
    }

    protocol(device) {
        const protocol = device.scale && device.scale.protocol;
        if (!protocol) {
            return "";
        }
        return t("device.protocol", {
            name: protocol.name || protocol.driver,
            baudrate: protocol.baudrate ?? "",
        });
    }

    testLabel(device) {
        if (device.type === "scale") {
            return t("device.read_weight");
        }
        return this.state.testing ? t("device.print_test_waiting") : t("device.print_test");
    }

    async runTest(device) {
        const state = this.state;
        state.busy = true;
        state.testing = true;
        this.setResult(null);
        try {
            // A print test ends within about 45 seconds; the request carries no time limit of its own.
            const answer = await call("/viin_hw/device/test", {
                method: "POST",
                params: { device_identifier: device.identifier },
            });
            this.setResult(() => testResult(device, answer));
        } finally {
            state.busy = false;
            state.testing = false;
        }
    }

    async listen(device) {
        const state = this.state;
        state.listening = true;
        state.events = [];
        this.setResult(null);
        const abort = new AbortController();
        this.abort = abort;
        const timer = setTimeout(() => abort.abort(), LISTEN_SECONDS * 1000);
        const session = `viin-${Math.random().toString(36).slice(2)}`;
        let last = 0;
        try {
            while (!abort.signal.aborted) {
                const { body } = await send("/hw_drivers/event", {
                    method: "POST",
                    signal: abort.signal,
                    params: {
                        listener: {
                            session_id: session,
                            devices: [device.identifier],
                            last_event: last,
                        },
                    },
                });
                if (body && body.time && body.time > last) {
                    last = body.time;
                    state.events.unshift(String(body.value ?? ""));
                    state.events.splice(8);
                } else if (body && body.reason && body.reason !== "aborted") {
                    this.setResult(() => ({
                        kind: "danger",
                        text: reasonText("device.listen_failed", body.reason),
                    }));
                    break;
                }
            }
        } finally {
            clearTimeout(timer);
            state.listening = false;
            this.abort = null;
            if (!state.events.length && !this.result) {
                this.setResult(() => ({
                    kind: "warning",
                    text: t("device.nothing_heard"),
                    todo: t("device.nothing_heard_todo"),
                }));
            }
        }
    }

    stopListening(device) {
        if (this.abort) {
            this.abort.abort();
        }
    }

    async useAs(device, isScanner) {
        if (device.keyboard.is_scanner === isScanner) {
            return;
        }
        const answer = await call("/viin_hw/device/use_as", {
            method: "POST",
            params: { device_identifier: device.identifier, is_scanner: isScanner },
        });
        if (answer.status === "switched") {
            this.setResult(() => ({
                kind: "success",
                text: t("device.switched", { kind: t(`device.kind.${answer.kind}`) }),
            }));
            await this.env.refresh();
        } else if (answer.status !== "locked") {
            this.setResult(() => ({ kind: "danger", text: reasonText("device.use_as", answer.reason) }));
        }
    }

    async makeLabel(device) {
        // The busy state reaches the button only on the next render, so a second click of the same tick is stopped here.
        if (this.labelling) {
            return;
        }
        this.labelling = true;
        const state = this.state;
        state.busy = true;
        try {
            const answer = await call("/viin_hw/device/label_printer", {
                method: "POST",
                params: { device_identifier: device.identifier },
            });
            if (answer.status === "added") {
                this.setResult(() => ({
                    kind: "success",
                    text: t("device.label_added", { queue: answer.queue }),
                }));
                await this.env.refresh();
            } else if (answer.status === "refused" && answer.reason === "already_added") {
                this.setResult(() => ({
                    kind: "info",
                    text: reasonText("device.label", answer.reason, { queue: answer.queue || "" }),
                }));
                await this.env.refresh();
            } else if (answer.status !== "locked") {
                this.setResult(() => ({ kind: "danger", text: reasonText("device.label", answer.reason) }));
            }
        } finally {
            this.labelling = false;
            state.busy = false;
        }
    }
}

export class DevicePanel extends Component {
    static props = {};
    static components = { DeviceCard };
    static template = xml`
        <section class="viin-card" aria-labelledby="devices-title" id="devices">
            <div class="viin-card-head">
                <h2 id="devices-title" t-esc="t('devices.title')"/>
                <div class="viin-actions">
                    <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="addPrinter">
                        <i class="fa fa-plus" aria-hidden="true"/><t t-esc="t('button.add_printer')"/>
                    </button>
                </div>
            </div>
            <div class="viin-card-body">
                <p t-if="!devices.length" class="mb-0" t-esc="t('devices.none')"/>
                <DeviceCard t-foreach="devices" t-as="device" t-key="device.identifier" device="device"/>
            </div>
        </section>`;

    setup() {
        this.t = t;
        this.store = useState(store);
    }

    get devices() {
        return this.store.status.devices;
    }

    addPrinter() {
        openDialog("printer");
    }
}
