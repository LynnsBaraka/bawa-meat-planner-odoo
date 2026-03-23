/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class BawaPlanningEngine extends Component {
    static template = "bawa_meat_planner.PlanningEngine";

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");

        this.state = useState({
            orders: [],
            minStock: {},
            overrides: { custom_yields: {}, substitution_choices: {} },
            planResult: null,
            planSteps: [],
            loading: false,
            calculating: false,
            recursiveDriverEnabled: false,
            newOrder: { productName: '', level: '3', qty: 0 },
            // What If delta state (lightweight JS only: no server round trip)
            whatIfYieldOverrides: {},
            whatIfDeltaHQ: 0,
            whatIfDeltaFQ: 0,
            // Yield template loaded at startup for What If delta math
            tpl: null,
        });

        // Load yield template at startup so the What If calculator has
        // L2_to_L3 and L1_to_L2 yields available in memory.
        onWillStart(async () => {
            try {
                this.state.tpl = await this.orm.call(
                    'bawa.yield.template', 'get_active_template', []
                );
            } catch (e) {
                // Template not configured yet: What If will be disabled
                this.state.tpl = null;
            }
        });
    }

    // ORDER MANAGEMENT

    addOrder() {
        const { productName, level, qty } = this.state.newOrder;
        if (!productName || parseFloat(qty) <= 0) {
            this.notification.add(
                'Select a product and enter a positive quantity.',
                { type: 'warning' }
            );
            return;
        }
        this.state.orders.push({
            id: Date.now(),
            product: productName,
            level: parseInt(level),
            qty: parseFloat(qty),
        });
        this.state.newOrder = { productName: '', level: '3', qty: 0 };
        this.state.planResult = null;
        this.state.planSteps = [];
    }

    removeOrder(id) {
        this.state.orders = this.state.orders.filter(o => o.id !== id);
        this.state.planResult = null;
        this.state.planSteps = [];
    }

    toggleRecursiveDriver() {
        this.state.recursiveDriverEnabled = !this.state.recursiveDriverEnabled;
        this.state.planResult = null;
        this.state.planSteps = [];
    }

    // MAIN PLAN GENERATION: CALLS PYTHON

    async generatePlan() {
        if (!this.state.orders.length) {
            this.notification.add(
                'Add at least one order before generating a plan.',
                { type: 'warning' }
            );
            return;
        }
        this.state.calculating = true;
        this.state.planResult = null;
        this.state.planSteps = [];

        try {
            const payload = {
                orders: this.state.orders.map(o => ({
                    product: o.product,
                    level: o.level,
                    qty: o.qty,
                })),
                min_stock: this.state.minStock,
                overrides: this.state.overrides,
                recursive_driver_enabled: this.state.recursiveDriverEnabled,
            };

            // Single RPC call: Python does all the work
            const result = await this.orm.call(
                'bawa.plan.wizard',
                'calculate_plan_from_ui',
                [payload]
            );

            this.state.planResult = result;
            this.state.planSteps = result.steps || [];
            this.state.whatIfYieldOverrides = {};
            this.state.whatIfDeltaHQ = 0;
            this.state.whatIfDeltaFQ = 0;

        } catch (e) {
            this.notification.add(
                `Planning failed: ${e.message || e}`,
                { type: 'danger' }
            );
        } finally {
            this.state.calculating = false;
        }
    }

    // WHAT IF SIMULATOR: LIGHTWEIGHT JS DELTA
    // This is the ONE place JavaScript does math.
    // It operates on the already computed plan result to show
    // instant yield sensitivity without a server round trip.

    applyWhatIfYield(productName, newYieldPct) {
        this.state.whatIfYieldOverrides[productName] = parseFloat(newYieldPct);
        this._computeWhatIfDelta();
    }

    _computeWhatIfDelta() {
        /**
         * Lightweight What If delta calculator.
         * Runs entirely in the browser on the already computed plan result.
         * No server round trip: gives Gabriel instant slider feedback.
         *
         * For each L3 end cut with a yield override:
         *
         *   original_primal_needed = qty / (template_yield / 100)
         *   new_primal_needed      = qty / (override_yield / 100)
         *   primal_delta           = new_primal_needed - original_primal_needed
         *
         * Then trace the primal delta up to the quarter:
         *   quarter_delta = primal_delta / (l1_to_l2_yield / 100)
         *
         * Accumulate into HQ delta or FQ delta depending on which quarter
         * the primal belongs to. Display as an adjustment on the base result.
         *
         * This is deliberately approximate: it uses MAX of one driver logic
         * per override rather than re running the full MAX across all drivers.
         * Full accuracy requires the server re plan via recalculateWithOverrides().
         */
        if (!this.state.planResult) return;

        const planResult = this.state.planResult;
        const tpl = this.state.tpl;
        let deltaHQ = 0;
        let deltaFQ = 0;

        for (const [productName, overrideYieldPct] of Object.entries(
            this.state.whatIfYieldOverrides
        )) {
            // CRITICAL GUARD: skip zero or undefined override yields.
            // Without this, division by zero produces Infinity and cascades
            // garbage into the delta calculation.
            if (!overrideYieldPct || overrideYieldPct <= 0) continue;

            const endCutQty = planResult.end_cut_demands?.[productName];
            if (!endCutQty || endCutQty <= 0) continue;

            // Find this cut's parent primal and its template yield from tpl
            let templateYield = null;
            let parentPrimalName = null;
            let parentQuarterName = null;

            for (const [primalName, cuts] of Object.entries(
                tpl?.L2_to_L3 || {}
            )) {
                if (cuts[productName] !== undefined) {
                    templateYield = cuts[productName];
                    parentPrimalName = primalName;
                    break;
                }
            }
            if (!templateYield || !parentPrimalName) continue;

            // Find which quarter this primal belongs to
            // and its L1 to L2 yield
            let l1Yield = null;
            for (const [quarterName, primals] of Object.entries(
                tpl?.L1_to_L2 || {}
            )) {
                if (primals[parentPrimalName] !== undefined) {
                    l1Yield = primals[parentPrimalName];
                    parentQuarterName = quarterName;
                    break;
                }
            }
            if (!l1Yield || !parentQuarterName) continue;

            // Delta calculation
            const originalPrimalNeeded = endCutQty / (templateYield / 100);
            const newPrimalNeeded = endCutQty / (overrideYieldPct / 100);
            const primalDelta = newPrimalNeeded - originalPrimalNeeded;
            const quarterDelta = primalDelta / (l1Yield / 100);

            if (parentQuarterName === 'Hind Quarter') {
                deltaHQ += quarterDelta;
            } else if (parentQuarterName === 'Fore Quarter') {
                deltaFQ += quarterDelta;
            }
        }

        this.state.whatIfDeltaHQ = deltaHQ;
        this.state.whatIfDeltaFQ = deltaFQ;
    }

    async recalculateWithOverrides() {
        // Merge What If overrides into the overrides dict
        // and re run full plan on server
        this.state.overrides.custom_yields = {
            ...this.state.whatIfYieldOverrides,
        };
        await this.generatePlan();
    }

    // SAVE PLAN

    async savePlan() {
        if (!this.state.planResult) return;
        this.state.loading = true;
        try {
            const planId = await this.orm.call(
                'bawa.plan.wizard',
                'commit_plan',
                [{
                    plan_result: this.state.planResult,
                    orders: this.state.orders,
                    recursive_driver_used: this.state.recursiveDriverEnabled,
                }]
            );
            this.notification.add(
                'Plan saved. Open to commit and create Manufacturing Orders.',
                { type: 'success' }
            );
            this.action.doAction({
                type: 'ir.actions.act_window',
                res_model: 'bawa.plan',
                res_id: planId,
                views: [[false, 'form']],
            });
        } catch (e) {
            this.notification.add(
                `Save failed: ${e.message || e}`,
                { type: 'danger' }
            );
        } finally {
            this.state.loading = false;
        }
    }

    // HELPERS

    get effectiveHQ() {
        if (!this.state.planResult) return 0;
        return (
            this.state.planResult.hqNeeded
            + (this.state.whatIfDeltaHQ || 0)
        ).toFixed(1);
    }

    get effectiveFQ() {
        if (!this.state.planResult) return 0;
        return (
            this.state.planResult.fqNeeded
            + (this.state.whatIfDeltaFQ || 0)
        ).toFixed(1);
    }

    stepBadgeClass(type) {
        const map = {
            ok: 'text-success',
            warn: 'text-warning',
            header: 'fw-bold text-muted',
            detail: 'text-muted',
            neutral: '',
        };
        return map[type] || '';
    }
}

registry.category("actions").add("bawa_planning_engine", BawaPlanningEngine);
