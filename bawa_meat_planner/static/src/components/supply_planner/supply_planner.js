/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class BawaSupplyPlanner extends Component {
    static template = "bawa_meat_planner.SupplyPlanner";

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");

        this.state = useState({
            mode: 'carcass',
            carcassCount: 1,
            avgHQWeightKg: 145,
            avgFQWeightKg: 120,
            quarterInputs: { 'Hind Quarter': 0, 'Fore Quarter': 0 },
            primalInputs: {},
            forwardResult: null,
            l4Allocations: {},
            l5Targets: {},
            loading: false,
            tpl: null,
        });

        onWillStart(async () => {
            try {
                this.state.tpl = await this.orm.call(
                    'bawa.yield.template', 'get_active_template', []
                );
            } catch (e) {
                this.state.tpl = null;
            }
        });
    }

    // MODE SWITCHING

    setMode(mode) {
        this.state.mode = mode;
        this.state.forwardResult = null;
        this.state.l4Allocations = {};
        this.state.l5Targets = {};
    }

    // INPUT HANDLERS

    updateCarcassCount(ev) {
        this.state.carcassCount = parseInt(ev.target.value) || 0;
    }

    updateAvgHQ(ev) {
        this.state.avgHQWeightKg = parseFloat(ev.target.value) || 0;
    }

    updateAvgFQ(ev) {
        this.state.avgFQWeightKg = parseFloat(ev.target.value) || 0;
    }

    updateQuarterInput(quarterName, ev) {
        this.state.quarterInputs[quarterName] = parseFloat(ev.target.value) || 0;
    }

    updatePrimalInput(primalName, ev) {
        this.state.primalInputs[primalName] = parseFloat(ev.target.value) || 0;
    }

    updateL4Allocation(productName, ev) {
        this.state.l4Allocations[productName] = parseFloat(ev.target.value) || 0;
    }

    updateL5Target(productName, ev) {
        this.state.l5Targets[productName] = parseFloat(ev.target.value) || 0;
    }

    // MAIN CALCULATION: CALLS PYTHON

    async runForward() {
        this.state.loading = true;
        this.state.forwardResult = null;

        try {
            const payload = {
                mode: this.state.mode,
                carcass_count: this.state.carcassCount,
                avg_hq_weight_kg: this.state.avgHQWeightKg,
                avg_fq_weight_kg: this.state.avgFQWeightKg,
                quarter_inputs: this.state.quarterInputs,
                primal_inputs: this.state.primalInputs,
                l4_allocations: this.state.l4Allocations,
                l5_targets: this.state.l5Targets,
            };

            const result = await this.orm.call(
                'bawa.plan.wizard',
                'run_forward_explosion',
                [payload]
            );

            this.state.forwardResult = result;

        } catch (e) {
            this.notification.add(
                `Forward explosion failed: ${e.message || e}`,
                { type: 'danger' }
            );
        } finally {
            this.state.loading = false;
        }
    }

    async recalculateWithAllocations() {
        // Re-run forward with current allocations and L5 targets
        await this.runForward();
    }

    // CONVERT TO ORDERS

    async convertToOrders() {
        if (!this.state.forwardResult) return;

        const orders = [];

        // L5 targets become orders
        for (const [productName, qty] of Object.entries(this.state.l5Targets)) {
            if (qty > 0) {
                orders.push({
                    product: productName,
                    level: 5,
                    qty: qty,
                });
            }
        }

        // Surplus L4 allocations (not consumed by L5) become orders
        const feasibility = this.state.forwardResult.feasibility || {};
        const l4ConsumedByL5 = {};
        for (const check of (feasibility.checks || [])) {
            const product = check.l4_product;
            l4ConsumedByL5[product] = (l4ConsumedByL5[product] || 0) + check.l4_needed;
        }
        for (const [productName, alloc] of Object.entries(this.state.l4Allocations)) {
            const consumed = l4ConsumedByL5[productName] || 0;
            const surplus = alloc - consumed;
            if (surplus > 0) {
                orders.push({
                    product: productName,
                    level: 4,
                    qty: surplus,
                });
            }
        }

        if (!orders.length) {
            this.notification.add(
                'No orders to convert. Set L4 allocations or L5 targets first.',
                { type: 'warning' }
            );
            return;
        }

        this.notification.add(
            `${orders.length} order(s) ready. Navigating to Planning Engine.`,
            { type: 'success' }
        );

        // Navigate to planning engine (the orders will need to be manually added
        // since the OWL components do not share state directly)
        this.action.doAction({
            type: 'ir.actions.client',
            tag: 'bawa_planning_engine',
        });
    }

    // HELPERS

    get totalTrimKg() {
        return this.state.forwardResult?.total_trim_kg || 0;
    }

    get trimBalance() {
        return this.state.forwardResult?.feasibility?.trim_balance || 0;
    }

    get trimAllocated() {
        return this.state.forwardResult?.feasibility?.trim_allocated || 0;
    }

    get availablePrimals() {
        if (!this.state.tpl) return [];
        const primals = [];
        for (const [quarterName, primalMap] of Object.entries(this.state.tpl.L1_to_L2 || {})) {
            for (const primalName of Object.keys(primalMap)) {
                if (!primalName.includes('Bones') && !primalName.includes('Fat/Trim')) {
                    primals.push(primalName);
                }
            }
        }
        return primals;
    }

    get availableL4Products() {
        if (!this.state.tpl) return [];
        return Object.keys(this.state.tpl.trim_to_L4 || {});
    }

    get availableL5Products() {
        if (!this.state.tpl) return [];
        return Object.keys(this.state.tpl.L5_recipes || {});
    }

    feasibilityStatusClass(status) {
        const map = {
            covered: 'text-success',
            nearly: 'text-warning',
            short: 'text-danger',
        };
        return map[status] || '';
    }

    isOverAllocated(productName) {
        const maxPossible = this.state.forwardResult?.l4_max_possible?.[productName] || 0;
        const allocated = this.state.l4Allocations[productName] || 0;
        return allocated > maxPossible;
    }
}

registry.category("actions").add("bawa_supply_planner", BawaSupplyPlanner);
