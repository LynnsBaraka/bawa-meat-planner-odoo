# WARNING: This wizard resolves products by name string matching.
# Product renames will break planning. This is known technical debt for V11.
# The V11 migration should replace all name based lookups with product_id
# references and restructure yield template JSON to use integer IDs.

from odoo import models, fields, api
from odoo.exceptions import UserError
import json
from datetime import date
from collections import defaultdict


class BawaPlanWizard(models.TransientModel):
    """
    Transient wizard that holds an in progress plan session.
    The OWL component calls calculate_plan_from_ui() via RPC.
    The result is a JSON payload rendered by OWL.
    When the user is satisfied, commit_plan() promotes to a permanent bawa.plan record.
    """
    _name = 'bawa.plan.wizard'
    _description = 'Butchery Planning Wizard (Transient)'

    yield_template_id = fields.Many2one(
        'bawa.yield.template',
        string='Yield Template',
        default=lambda self: self.env['bawa.yield.template'].get_active_template_id()
    )
    recursive_driver_enabled = fields.Boolean(
        string='Auto Scale Quarters',
        default=False,
        help='When enabled, automatically escalates quarter procurement '
             'to resolve persistent trim shortfalls.'
    )
    plan_result_json = fields.Text(
        string='Plan Result (JSON)', readonly=True
    )

    # PUBLIC API (called by OWL via this.orm.call)

    @api.model
    def calculate_plan_from_ui(self, payload):
        """
        Main entry point called by the OWL component.
        payload = {
            'orders': [{'product': str, 'level': int, 'qty': float}],
            'min_stock': {'product_name': float},
            'overrides': {'custom_yields': {}, 'substitution_choices': {}},
            'recursive_driver_enabled': bool,
        }
        Returns the full plan result dict
        (steps, mos, hqNeeded, fqNeeded, substitutions).
        """
        if not payload.get('orders'):
            raise UserError(
                'No orders provided. '
                'Add at least one order before generating a plan.'
            )

        tpl = self.env['bawa.yield.template'].get_active_template()
        orders = payload.get('orders', [])
        min_stock = payload.get('min_stock', {})
        overrides = payload.get('overrides', {})
        recursive_driver_enabled = payload.get(
            'recursive_driver_enabled', False
        )

        result = self._run_plan(
            tpl, orders, min_stock, overrides, recursive_driver_enabled
        )
        return result

    @api.model
    def commit_plan(self, payload):
        """
        Promotes a plan result to a permanent bawa.plan record.
        payload = {
            'plan_result': dict,
            'orders': list,
            'recursive_driver_used': bool,
        }
        Returns the new plan ID.
        """
        plan_result = payload.get('plan_result', {})
        orders = payload.get('orders', [])
        recursive_driver_used = payload.get('recursive_driver_used', False)
        template_id = self.env['bawa.yield.template'].get_active_template_id()

        order_lines = []
        for o in orders:
            product = self.env['product.product'].search(
                [('name', '=', o['product'])], limit=1
            )
            if product:
                order_lines.append((0, 0, {
                    'product_id': product.id,
                    'product_level': str(o.get('level', 3)),
                    'qty': o.get('qty', 0),
                }))

        plan = self.env['bawa.plan'].create({
            'yield_template_id': template_id,
            'hq_required': plan_result.get('hqNeeded', 0),
            'fq_required': plan_result.get('fqNeeded', 0),
            'recursive_driver_used': recursive_driver_used,
            'plan_result_json': json.dumps(plan_result),
            'order_line_ids': order_lines,
        })
        return plan.id

    @api.model
    def run_forward_explosion(self, payload):
        """
        Supply driven forward planning.
        payload = {
            'mode': 'carcass' | 'quarters' | 'primals',
            'carcass_count': int,
            'avg_hq_weight_kg': float,
            'avg_fq_weight_kg': float,
            'quarter_inputs': {'Hind Quarter': float, 'Fore Quarter': float},
            'primal_inputs': {'Topside': float, ...},
            'l4_allocations': {'Minced Meat': float, ...},
            'l5_targets': {'Beef Sausages': float, ...},
        }
        """
        tpl = self.env['bawa.yield.template'].get_active_template()
        return self._run_forward(tpl, payload)

    # STEP 1: DEMAND RESOLUTION

    def _resolve_demands(self, tpl, orders, min_stock, steps):
        """
        Aggregates all demand from customer orders and min stock rules.
        Returns a dict: {product_name: total_qty_kg}
        """
        demands = defaultdict(float)

        for order in orders:
            product_name = order.get('product', '')
            qty = float(order.get('qty', 0))
            level = int(order.get('level', 3))

            if level == 5:
                recipe = tpl['L5_recipes'].get(product_name)
                if not recipe:
                    steps.append({
                        'type': 'warn',
                        'title': f'No L5 recipe found for {product_name}',
                        'detail': 'Check yield template configuration.',
                        'indent': False,
                    })
                    continue
                raw_needed = qty / (recipe['yieldPct'] / 100.0)
                steps.append({
                    'type': 'info',
                    'title': f'{product_name}: {qty:.2f} kg ordered',
                    'detail': (
                        f'L5 product. {recipe["yieldPct"]}% yield requires '
                        f'{raw_needed:.2f} kg raw batch.'
                    ),
                    'indent': False,
                })
                for inp in recipe.get('inputs', []):
                    meat_qty = round(raw_needed * inp['ratio'], 4)
                    demands[inp['product']] += meat_qty
                    steps.append({
                        'type': 'detail',
                        'title': f'Meat input: {inp["product"]}: {meat_qty:.2f} kg',
                        'detail': f'Ratio: {inp["ratio"] * 100:.0f}%',
                        'indent': True,
                    })
                # Non meat inputs (informational: logged so the planner
                # knows what to procure separately)
                for nm in recipe.get('nonMeat', []):
                    nm_qty = round(raw_needed * nm['ratio'], 4)
                    steps.append({
                        'type': 'detail',
                        'title': f'Non meat: {nm["item"]}: {nm_qty:.2f} {nm.get("uom", "kg")}',
                        'detail': 'Procure separately',
                        'indent': True,
                    })
            else:
                demands[product_name] += qty
                steps.append({
                    'type': 'info',
                    'title': f'{product_name}: {qty:.2f} kg',
                    'detail': f'Direct L{level} order.',
                    'indent': False,
                })

        # Min stock injection: uses ORM, not in memory filter
        for product_name, min_qty in min_stock.items():
            if not min_qty or min_qty <= 0:
                continue
            on_hand = self._get_on_hand(product_name)
            shortfall = max(0.0, min_qty - on_hand)
            if shortfall > 0:
                demands[product_name] += shortfall
                steps.append({
                    'type': 'info',
                    'title': (
                        f'Min stock rule: {product_name}: '
                        f'injecting {shortfall:.2f} kg'
                    ),
                    'detail': (
                        f'On hand: {on_hand:.2f} kg, '
                        f'Minimum: {min_qty:.2f} kg'
                    ),
                    'indent': False,
                })

        return dict(demands)

    # STEP 2: FEFO NETTING

    def _fefo_net_demands(self, demands, steps):
        """
        Nets demands against existing stock using FEFO.
        Queries stock.quant and stock.lot via ORM. PostgreSQL does the work.
        Returns net_demands dict: {product_name: remaining_shortfall_kg}
        """
        net_demands = {}

        for product_name, qty in demands.items():
            on_hand = self._get_on_hand(product_name)
            if on_hand <= 0:
                net_demands[product_name] = qty
                steps.append({
                    'type': 'neutral',
                    'title': f'{product_name}: need {qty:.2f} kg: no stock',
                    'detail': f'Full {qty:.2f} kg must be produced.',
                    'indent': False,
                })
                continue

            # FEFO: query lots ordered by expiry date ascending (PostgreSQL ORDER BY)
            lots = self._get_lots_fefo(product_name)
            remaining = qty
            consumed_detail = []

            for lot in lots:
                if remaining <= 0:
                    break
                take = min(lot['qty'], remaining)
                consumed_detail.append({
                    'lot': lot['lot'],
                    'qty': take,
                    'expiry': lot['expiry'],
                    'cost': lot['cost'],
                })
                remaining -= take

            shortfall = max(0.0, remaining)
            net_demands[product_name] = round(shortfall, 4)
            consumed_total = sum(c['qty'] for c in consumed_detail)

            if shortfall > 0:
                steps.append({
                    'type': 'info',
                    'title': (
                        f'{product_name}: need {qty:.2f} kg, '
                        f'on hand {on_hand:.2f} kg'
                    ),
                    'detail': (
                        f'Consuming {consumed_total:.2f} kg. '
                        f'Still short: {shortfall:.2f} kg.'
                    ),
                    'indent': False,
                })
            else:
                steps.append({
                    'type': 'ok',
                    'title': (
                        f'{product_name}: need {qty:.2f} kg: '
                        f'fully covered from stock'
                    ),
                    'detail': (
                        f'Consuming {consumed_total:.2f} kg from '
                        f'{len(consumed_detail)} lot(s).'
                    ),
                    'indent': False,
                })

            for c in consumed_detail:
                steps.append({
                    'type': 'detail',
                    'title': (
                        f'FEFO: lot {c["lot"]}: {c["qty"]:.2f} kg '
                        f'(expiry: {c["expiry"] or "none"})'
                    ),
                    'detail': f'Cost: NGN {c["cost"]:.2f}/kg',
                    'indent': True,
                })

        return net_demands

    # STEP 3: REVERSE EXPLOSION

    def _run_reverse_explosion(
        self, tpl, net_demands, overrides, recursive_driver_enabled, steps
    ):
        """
        Main reverse explosion: L4 trim pool + substitution + Recursive Driver,
        L3 end cuts to primals with MAX logic, primals to quarters with MAX logic.
        Returns: l4_demands, end_cut_demands, substitutions, quarter_max_demand
        """
        l4_demands = {}
        end_cut_demands = {}
        substitutions = []
        quarter_max_demand = defaultdict(float)

        # Pre pass: estimate quarters from L3 net demands for Avalanche calculation
        pre_pass_quarters = self._avalanche_pre_pass(tpl, net_demands, overrides)
        avalanche_trim = self._calc_avalanche_trim(tpl, pre_pass_quarters)
        avalanche_total = sum(avalanche_trim.values())

        for product_name, qty in net_demands.items():
            if qty <= 0:
                continue

            product = self.env['product.product'].search(
                [('name', '=', product_name),
                 ('butchery_level', '!=', False)], limit=1
            )
            if not product:
                continue

            level = int(product.butchery_level)

            if level == 4:
                self._process_l4_demand(
                    tpl, product_name, qty, avalanche_trim, avalanche_total,
                    overrides, recursive_driver_enabled, quarter_max_demand,
                    l4_demands, substitutions, steps
                )
            elif level == 3:
                end_cut_demands[product_name] = qty

        # L3 end cuts to L2 primals with MAX logic
        primal_demands = self._l3_to_primal_demands(
            tpl, end_cut_demands, overrides, steps
        )

        # L2 primals to L1 quarters with MAX logic
        self._primal_to_quarter_demands(
            tpl, primal_demands, quarter_max_demand, steps
        )

        return (
            l4_demands, end_cut_demands, substitutions,
            dict(quarter_max_demand)
        )

    def _process_l4_demand(
        self, tpl, product_name, qty, avalanche_trim, avalanche_total,
        overrides, recursive_driver_enabled, quarter_max_demand,
        l4_demands, substitutions, steps
    ):
        """Handles one L4 product: trim pool check, substitution, Recursive Driver."""
        l4_spec = tpl['trim_to_L4'].get(product_name)
        if not l4_spec:
            return

        conversion_yield = l4_spec['conversionYield'] / 100.0
        raw_needed = round(qty / conversion_yield, 4)
        l4_demands[product_name] = raw_needed

        # Build fridge trim pool using ORM
        trim_pool = self._build_trim_pool(tpl)
        fridge_total = sum(t['qty'] for t in trim_pool)

        # Inject Avalanche virtual lots (expiry=today, cost=0)
        today_str = date.today().isoformat()
        for candidate, avl_qty in avalanche_trim.items():
            if avl_qty > 0:
                trim_pool.append({
                    'lot': f'AVALANCHE-{candidate}',
                    'product': candidate,
                    'qty': round(avl_qty, 4),
                    'cost': 0.0,
                    'expiry': today_str,
                    'is_avalanche': True,
                })

        # Sort entire trim pool by expiry (FEFO. Avalanche lots sort first as today)
        trim_pool.sort(key=lambda t: t['expiry'] or '9999-12-31')

        steps.append({
            'type': 'info',
            'title': (
                f'{product_name}: {qty:.2f} kg needed '
                f'({l4_spec["conversionYield"]}% yield)'
            ),
            'detail': (
                f'Requires {raw_needed:.2f} kg trim. '
                f'Fridge: {fridge_total:.2f} kg + '
                f'Avalanche: {avalanche_total:.2f} kg.'
            ),
            'indent': False,
        })

        # Consume trim pool FEFO
        trim_remaining = raw_needed
        for lot in trim_pool:
            if trim_remaining <= 0:
                break
            take = min(lot['qty'], trim_remaining)
            src_yield = l4_spec.get('sourceYields', {}).get(
                lot['product'], l4_spec['conversionYield']
            )
            label = 'AVALANCHE' if lot.get('is_avalanche') else 'FEFO'
            steps.append({
                'type': 'detail',
                'title': (
                    f'{label}: {lot["product"]} lot {lot["lot"]}: '
                    f'{take:.2f} kg ({src_yield}% yield)'
                ),
                'detail': (
                    'Projected from this run'
                    if lot.get('is_avalanche')
                    else f'Expiry: {lot["expiry"] or "none"}'
                ),
                'indent': True,
            })
            trim_remaining -= take

        # Substitution if still short
        if trim_remaining > 0:
            steps.append({
                'type': 'warn',
                'title': (
                    f'Trim shortfall for {product_name}: '
                    f'{trim_remaining:.2f} kg'
                ),
                'detail': 'Activating substitution optimizer.',
                'indent': True,
            })
            trim_remaining = self._run_substitution(
                tpl, product_name, l4_spec, trim_remaining,
                overrides, substitutions, steps
            )

        # Recursive Driver if still short
        if trim_remaining > 0:
            if recursive_driver_enabled:
                trim_remaining = self._run_recursive_driver(
                    tpl, product_name, l4_spec, trim_remaining,
                    quarter_max_demand, steps
                )
            else:
                steps.append({
                    'type': 'warn',
                    'title': (
                        f'Shortfall of {trim_remaining:.2f} kg '
                        f'for {product_name}'
                    ),
                    'detail': 'Enable Auto Scale Quarters to resolve automatically.',
                    'indent': True,
                })
        else:
            steps.append({
                'type': 'ok',
                'title': f'{product_name} shortfall resolved',
                'detail': '',
                'indent': True,
            })

    def _run_substitution(
        self, tpl, product_name, l4_spec, trim_remaining,
        overrides, substitutions, steps
    ):
        """
        FEFO + cost substitution optimizer.
        Queries canConvertL4 products from stock via ORM.
        FEFO (expiry) is the primary sort. Cost is the tiebreaker.
        Source specific yields applied per candidate (v8.1 behaviour).
        """
        forced_sub = overrides.get(
            'substitution_choices', {}
        ).get(product_name)
        sub_candidates = self._get_substitution_candidates(forced_sub)

        if not sub_candidates:
            steps.append({
                'type': 'warn',
                'title': 'No substitution candidates available in stock',
                'detail': '',
                'indent': True,
            })
            return trim_remaining

        for candidate in sub_candidates:
            if trim_remaining <= 0:
                break

            # Source specific yield (v8.1: primal name key in sourceYields)
            src_yield_pct = l4_spec.get('sourceYields', {}).get(
                candidate['product'], l4_spec['conversionYield']
            )
            conv_yield = src_yield_pct / 100.0
            raw_needed_from_sub = trim_remaining / conv_yield
            take = min(candidate['qty'], raw_needed_from_sub)
            output_gained = round(take * conv_yield, 4)

            substitutions.append({
                'for_product': product_name,
                'from_product': candidate['product'],
                'from_lot': candidate['lot'],
                'qty': round(take, 4),
                'cost': candidate['cost'],
                'output_gained': output_gained,
                'effective_yield_pct': src_yield_pct,
                'is_forced': bool(forced_sub),
                'expiry_used': candidate['expiry'],
            })

            label = 'SUBSTITUTION (FORCED)' if forced_sub else 'SUBSTITUTION'
            src_note = (
                '(source specific rate)'
                if l4_spec.get('sourceYields', {}).get(candidate['product'])
                else '(default rate)'
            )
            steps.append({
                'type': 'warn',
                'title': (
                    f'{label}: {candidate["product"]} '
                    f'lot {candidate["lot"]}: {take:.2f} kg '
                    f'@ {src_yield_pct}% yield'
                ),
                'detail': (
                    f'Produces {output_gained:.2f} kg '
                    f'{product_name} {src_note}.'
                ),
                'indent': True,
            })

            trim_remaining -= output_gained

        return max(0.0, trim_remaining)

    def _run_recursive_driver(
        self, tpl, product_name, l4_spec, trim_remaining,
        quarter_max_demand, steps
    ):
        """
        Recursive Driver: escalates quarter procurement to generate more
        grindable surplus. Picks HQ or FQ based on grindable density.
        Max 10 iterations. Modifies quarter_max_demand in place.
        """
        hq_density = self._calc_grindable_density(tpl, 'Hind Quarter')
        fq_density = self._calc_grindable_density(tpl, 'Fore Quarter')
        best_quarter = (
            'Hind Quarter' if hq_density >= fq_density else 'Fore Quarter'
        )
        best_density = max(hq_density, fq_density)

        steps.append({
            'type': 'info',
            'title': (
                f'Auto Scale Quarters activated: '
                f'{trim_remaining:.2f} kg {product_name} unresolved'
            ),
            'detail': (
                f'Grindable density: HQ={hq_density * 100:.1f}%, '
                f'FQ={fq_density * 100:.1f}%. Using {best_quarter}.'
            ),
            'indent': True,
        })

        if best_density <= 0:
            steps.append({
                'type': 'warn',
                'title': (
                    'Auto Scale Quarters: no grindable density '
                    'in yield template'
                ),
                'detail': 'Manual procurement required.',
                'indent': True,
            })
            return trim_remaining

        conversion_yield = l4_spec['conversionYield'] / 100.0
        rd_remaining = trim_remaining
        rd_iter = 0
        MAX_ITERATIONS = 10

        while rd_remaining > 0.001 and rd_iter < MAX_ITERATIONS:
            rd_iter += 1
            raw_trim_needed = rd_remaining / conversion_yield
            additional_q = round(raw_trim_needed / best_density, 4)
            output_gained = round(
                additional_q * best_density * conversion_yield, 4
            )
            quarter_max_demand[best_quarter] += additional_q
            rd_remaining = max(0.0, rd_remaining - output_gained)

            steps.append({
                'type': 'info',
                'title': (
                    f'Auto Scale iteration {rd_iter}: '
                    f'+{additional_q:.2f} kg {best_quarter}'
                ),
                'detail': (
                    f'Generates {output_gained:.2f} kg {product_name}. '
                    f'Running total escalation: '
                    f'{quarter_max_demand[best_quarter]:.2f} kg.'
                ),
                'indent': True,
            })

        if rd_remaining <= 0.01:
            steps.append({
                'type': 'ok',
                'title': (
                    f'Auto Scale resolved {product_name} shortfall '
                    f'in {rd_iter} iteration(s)'
                ),
                'detail': (
                    'Quarter order escalated. '
                    'Review Step 4 for updated requirements.'
                ),
                'indent': True,
            })
        else:
            steps.append({
                'type': 'warn',
                'title': (
                    f'Auto Scale exhausted after {rd_iter} iteration(s): '
                    f'{rd_remaining:.2f} kg still unresolved'
                ),
                'detail': 'Manual procurement required for remaining shortfall.',
                'indent': True,
            })

        return rd_remaining

    def _l3_to_primal_demands(self, tpl, end_cut_demands, overrides, steps):
        """
        Converts L3 end cut demands to L2 primal demands.
        Uses MAX logic within each primal (not SUM).
        """
        primal_demand_drivers = defaultdict(dict)

        for cut_name, qty in end_cut_demands.items():
            product = self.env['product.product'].search(
                [('name', '=', cut_name),
                 ('butchery_level', '=', '3')], limit=1
            )
            if not product or not product.butchery_parent_product_id:
                continue
            parent_name = product.butchery_parent_product_id.name
            l2_to_l3 = tpl['L2_to_L3'].get(parent_name, {})
            template_yield = l2_to_l3.get(cut_name)
            if not template_yield:
                continue

            custom_yield = overrides.get('custom_yields', {}).get(cut_name)
            applied_yield = (
                custom_yield if custom_yield is not None else template_yield
            ) / 100.0
            if applied_yield <= 0:
                continue

            primal_needed = round(qty / applied_yield, 4)
            primal_demand_drivers[parent_name][cut_name] = primal_needed

        primal_max_demand = {}
        for primal_name, drivers in primal_demand_drivers.items():
            max_demand = max(drivers.values())
            primal_max_demand[primal_name] = max_demand
            top_driver = max(drivers, key=drivers.get)
            steps.append({
                'type': 'info',
                'title': (
                    f'Primal {primal_name}: {max_demand:.2f} kg '
                    f'(driven by {top_driver})'
                ),
                'detail': (
                    f'MAX of {len(drivers)} co product(s). '
                    f'Others: {", ".join(k for k in drivers if k != top_driver)}.'
                ),
                'indent': False,
            })

        return primal_max_demand

    def _primal_to_quarter_demands(
        self, tpl, primal_demands, quarter_max_demand, steps
    ):
        """
        Converts L2 primal demands to L1 quarter demands.
        Nets each primal against existing stock first.
        Uses MAX logic across primals per quarter.
        """
        quarter_drivers = defaultdict(dict)

        for primal_name, qty in primal_demands.items():
            on_hand = self._get_on_hand(primal_name)
            net_primal = max(0.0, qty - on_hand)
            if net_primal <= 0:
                steps.append({
                    'type': 'ok',
                    'title': (
                        f'Primal {primal_name}: {qty:.2f} kg: '
                        f'fully covered by {on_hand:.2f} kg on hand'
                    ),
                    'detail': '',
                    'indent': False,
                })
                continue

            product = self.env['product.product'].search(
                [('name', '=', primal_name),
                 ('butchery_level', '=', '2')], limit=1
            )
            if not product or not product.butchery_parent_product_id:
                continue

            quarter_name = product.butchery_parent_product_id.name
            l1_to_l2 = tpl['L1_to_L2'].get(quarter_name, {})
            template_yield = l1_to_l2.get(primal_name)
            if not template_yield:
                continue

            applied_yield = template_yield / 100.0
            q_needed = round(net_primal / applied_yield, 4)
            quarter_drivers[quarter_name][primal_name] = q_needed

        for q_name, drivers in quarter_drivers.items():
            max_q = max(drivers.values())
            top_driver = max(drivers, key=drivers.get)
            quarter_max_demand[q_name] += max_q
            steps.append({
                'type': 'info',
                'title': (
                    f'{q_name}: {max_q:.2f} kg gross '
                    f'(driven by {top_driver})'
                ),
                'detail': f'MAX of {len(drivers)} primal driver(s).',
                'indent': False,
            })

    # STEP 4: QUARTER REQUIREMENTS

    def _compute_final_quarters(self, quarter_max_demand, steps):
        """Nets quarter requirements against existing quarter stock."""
        final_quarters = {}
        for q_name, qty in quarter_max_demand.items():
            on_hand = self._get_on_hand(q_name)
            net = max(0.0, qty - on_hand)
            final_quarters[q_name] = round(net, 4)
            steps.append({
                'type': 'info',
                'title': (
                    f'{q_name}: {qty:.2f} kg gross: net {net:.2f} kg '
                    f'to procure ({on_hand:.2f} kg on hand)'
                ),
                'detail': '',
                'indent': False,
            })
        return final_quarters

    # MAIN ORCHESTRATOR

    def _run_plan(
        self, tpl, orders, min_stock, overrides, recursive_driver_enabled
    ):
        steps = []

        steps.append({
            'type': 'header',
            'title': 'Step 1: Demand Resolution',
            'detail': 'Breaking down orders and minimum stock rules into material requirements.',
        })
        demands = self._resolve_demands(tpl, orders, min_stock, steps)

        steps.append({
            'type': 'header',
            'title': 'Step 2: Inventory Netting (FEFO)',
            'detail': 'Consuming earliest expiry lots first. PostgreSQL sorts by expiry date.',
        })
        net_demands = self._fefo_net_demands(demands, steps)

        steps.append({
            'type': 'header',
            'title': 'Step 3: Reverse Explosion',
            'detail': 'Tracing net demands back through the production hierarchy.',
        })
        l4_demands, end_cut_demands, substitutions, quarter_max_demand = (
            self._run_reverse_explosion(
                tpl, net_demands, overrides, recursive_driver_enabled, steps
            )
        )

        steps.append({
            'type': 'header',
            'title': 'Step 4: Quarter Requirements',
            'detail': 'MAX co product logic applied. Netted against existing quarter stock.',
        })
        final_quarters = self._compute_final_quarters(
            quarter_max_demand, steps
        )

        hq_needed = final_quarters.get('Hind Quarter', 0.0)
        fq_needed = final_quarters.get('Fore Quarter', 0.0)

        return {
            'steps': steps,
            'demands': demands,
            'net_demands': net_demands,
            'l4_demands': l4_demands,
            'end_cut_demands': end_cut_demands,
            'substitutions': substitutions,
            'quarter_max_demand': quarter_max_demand,
            'final_quarters': final_quarters,
            'hqNeeded': round(hq_needed, 2),
            'fqNeeded': round(fq_needed, 2),
            'mos': self._build_mo_suggestions(
                tpl, l4_demands, substitutions, final_quarters
            ),
        }

    # ORM HELPERS

    def _get_on_hand(self, product_name):
        """Returns total on hand quantity for a product across all internal locations."""
        quants = self.env['stock.quant'].search([
            ('product_id.name', '=', product_name),
            ('location_id.usage', '=', 'internal'),
        ])
        return sum(q.quantity for q in quants if q.quantity > 0)

    def _get_lots_fefo(self, product_name):
        """
        Returns lot details for a product sorted by expiry date ascending (FEFO).

        CRITICAL: Query stock.quant directly. Do NOT query stock.lot and loop.
        Querying stock.lot first and then searching stock.quant inside the loop
        creates an N+1 query problem (1 query + N queries for N lots = disaster
        at scale).

        Instead, query stock.quant once with lot_id.expiration_date ordering.
        PostgreSQL sorts the join in a single query.

        NOTE: lot_id.expiration_date is provided by the product_expiry module
        which is a mandatory dependency in __manifest__.py.
        """
        quants = self.env['stock.quant'].search([
            ('product_id.name', '=', product_name),
            ('location_id.usage', '=', 'internal'),
            ('lot_id', '!=', False),
            ('quantity', '>', 0),
        ], order='lot_id.expiration_date asc')

        # Group by lot (a product can have multiple quant lines
        # per lot across locations)
        lot_map = {}
        for quant in quants:
            lot_id = quant.lot_id.id
            if lot_id not in lot_map:
                expiry = quant.lot_id.expiration_date
                lot_map[lot_id] = {
                    'lot': quant.lot_id.name,
                    'product': product_name,
                    'qty': 0.0,
                    'cost': quant.product_id.standard_price or 0.0,
                    'expiry': (
                        expiry.date().isoformat() if expiry else None
                    ),
                }
            lot_map[lot_id]['qty'] += quant.quantity

        # Return as list. Already FEFO ordered by PostgreSQL.
        return [v for v in lot_map.values() if v['qty'] > 0]

    def _build_trim_pool(self, tpl):
        """
        Queries all trim candidate products from stock.
        Returns list of lot dicts sorted by expiry.
        """
        trim_candidates = tpl.get('trim_candidates', [])
        result = []
        for candidate in trim_candidates:
            lots = self._get_lots_fefo(candidate)
            for lot in lots:
                lot['product'] = candidate
                result.append(lot)
        result.sort(key=lambda t: t['expiry'] or '9999-12-31')
        return result

    def _get_substitution_candidates(self, forced_product=None):
        """
        Queries canConvertL4 products from stock.
        FEFO (expiry) is primary sort. Cost is tiebreaker.
        If forced_product is set, returns only lots for that product.
        """
        domain = [
            ('product_id.can_convert_l4', '=', True),
            ('product_id.butchery_level', 'in', ['2', '3']),
            ('product_id.butchery_category', 'not in',
             ['byproduct', 'trim']),
            ('location_id.usage', '=', 'internal'),
            ('quantity', '>', 0),
        ]
        if forced_product:
            domain.append(('product_id.name', '=', forced_product))

        quants = self.env['stock.quant'].search(domain)
        candidates = []

        for quant in quants:
            if not quant.lot_id:
                continue
            expiry = quant.lot_id.expiration_date
            candidates.append({
                'product': quant.product_id.name,
                'lot': quant.lot_id.name,
                'qty': quant.quantity,
                'cost': quant.product_id.standard_price or 0.0,
                'expiry': (
                    expiry.date().isoformat() if expiry else None
                ),
                'level': int(quant.product_id.butchery_level),
            })

        # FEFO first, cost as tiebreaker
        candidates.sort(
            key=lambda c: (c['expiry'] or '9999-12-31', c['cost'])
        )
        return candidates

    # AVALANCHE HELPERS

    def _avalanche_pre_pass(self, tpl, net_demands, overrides):
        """
        Estimates quarter quantities driven by L3 net demands only.
        Used to project Avalanche trim before the main trim check.
        Intentionally approximate: conservative floor estimate.
        """
        pre_pass_quarters = defaultdict(float)

        for product_name, qty in net_demands.items():
            if qty <= 0:
                continue
            product = self.env['product.product'].search(
                [('name', '=', product_name),
                 ('butchery_level', '=', '3')], limit=1
            )
            if not product or not product.butchery_parent_product_id:
                continue

            parent_name = product.butchery_parent_product_id.name
            l2_to_l3 = tpl['L2_to_L3'].get(parent_name, {})
            template_yield = l2_to_l3.get(product_name)
            if not template_yield:
                continue

            custom_yield = overrides.get('custom_yields', {}).get(product_name)
            applied_yield = (
                custom_yield if custom_yield is not None else template_yield
            ) / 100.0
            if applied_yield <= 0:
                continue

            primal_needed = qty / applied_yield

            grandparent = (
                product.butchery_parent_product_id.butchery_parent_product_id
            )
            if not grandparent:
                continue

            quarter_name = grandparent.name
            l1_to_l2 = tpl['L1_to_L2'].get(quarter_name, {})
            primal_yield = l1_to_l2.get(parent_name)
            if not primal_yield:
                continue

            q_needed = primal_needed / (primal_yield / 100.0)
            pre_pass_quarters[quarter_name] += q_needed

        return dict(pre_pass_quarters)

    def _calc_avalanche_trim(self, tpl, quarter_qtys):
        """
        Projects trim generated from processing a given set of quarters.
        Returns {trim_product_name: projected_qty_kg}
        """
        avalanche = defaultdict(float)

        for q_name, q_qty in quarter_qtys.items():
            primals = tpl['L1_to_L2'].get(q_name, {})
            for primal_name, pct in primals.items():
                primal_qty = q_qty * pct / 100.0

                # Direct L1 fat/trim products go straight to trim pool
                product = self.env['product.product'].search(
                    [('name', '=', primal_name)], limit=1
                )
                if product and (
                    product.butchery_category == 'trim'
                    or 'fat' in primal_name.lower()
                ):
                    avalanche[primal_name] += primal_qty
                    continue

                # Trim off cuts from L2 to L3 breakdown
                cuts = tpl['L2_to_L3'].get(primal_name, {})
                for cut_name, cut_pct in cuts.items():
                    if 'trim' in cut_name.lower():
                        avalanche[cut_name] += primal_qty * cut_pct / 100.0

        return {k: round(v, 4) for k, v in avalanche.items()}

    def _calc_grindable_density(self, tpl, quarter_name):
        """
        Calculates kg of canConvertL4 eligible material per kg of quarter
        processed. Used by the Recursive Driver to pick the most efficient
        quarter type.
        """
        density = 0.0
        primals = tpl['L1_to_L2'].get(quarter_name, {})

        for p_name, p_pct in primals.items():
            product = self.env['product.product'].search(
                [('name', '=', p_name)], limit=1
            )

            # Direct trim/fat from quarter
            if product and (
                product.butchery_category == 'trim'
                or 'fat' in p_name.lower()
            ):
                density += p_pct / 100.0
                continue

            # canConvertL4 primal counts as grindable
            if product and product.can_convert_l4:
                density += p_pct / 100.0
                continue

            # Trim off cuts from non canConvertL4 primals
            cuts = tpl['L2_to_L3'].get(p_name, {})
            for c_name, c_pct in cuts.items():
                if 'trim' in c_name.lower():
                    density += (p_pct / 100.0) * (c_pct / 100.0)

        return density

    # MO SUGGESTION BUILDER

    def _build_mo_suggestions(
        self, tpl, l4_demands, substitutions, final_quarters
    ):
        """Builds MO suggestion data for the OWL component to display."""
        mos = []

        # MO1: Disassembly (L1 to L2)
        mo1_inputs = []
        for q_name, qty in final_quarters.items():
            if qty > 0:
                lots = self._get_lots_fefo(q_name)
                planned_lot = lots[0]['lot'] if lots else None
                mo1_inputs.append({
                    'product': q_name,
                    'qty': round(qty, 2),
                    'planned_lot': planned_lot,
                })
        if mo1_inputs:
            mos.append({
                'mo_type': 'disassembly',
                'label': 'MO1: Disassembly (L1 to L2)',
                'inputs': mo1_inputs,
                'outputs': [],
            })

        # MO3: Value Add (trim/subs to L4)
        mo3_inputs = []
        for sub in substitutions:
            mo3_inputs.append({
                'product': sub['from_product'],
                'lot': sub['from_lot'],
                'qty': round(sub['qty'], 2),
                'is_substitution': True,
                'is_forced': sub['is_forced'],
                'effective_yield_pct': sub['effective_yield_pct'],
            })
        if l4_demands or mo3_inputs:
            mo3_outputs = [
                {
                    'product': k,
                    'qty': round(
                        v * (
                            tpl['trim_to_L4'].get(k, {}).get(
                                'conversionYield', 92
                            ) / 100.0
                        ), 2
                    ),
                }
                for k, v in l4_demands.items()
            ]
            mos.append({
                'mo_type': 'value_add',
                'label': 'MO3: Value Add (Trim to L4)',
                'inputs': mo3_inputs,
                'outputs': mo3_outputs,
            })

        return mos

    # FORWARD EXPLOSION

    def _run_forward(self, tpl, payload):
        """Supply driven forward planning (v9.0 Supply Planner)."""
        mode = payload.get('mode', 'carcass')
        quarter_qtys = {}

        if mode == 'carcass':
            count = int(payload.get('carcass_count', 0))
            quarter_qtys['Hind Quarter'] = count * float(
                payload.get('avg_hq_weight_kg', 145)
            )
            quarter_qtys['Fore Quarter'] = count * float(
                payload.get('avg_fq_weight_kg', 120)
            )
        elif mode == 'quarters':
            quarter_qtys = {
                k: float(v)
                for k, v in payload.get('quarter_inputs', {}).items()
                if float(v) > 0
            }

        primal_outputs = {}
        trim_pool = {}
        end_cut_outputs = {}

        if mode != 'primals':
            for q_name, q_qty in quarter_qtys.items():
                for p_name, pct in tpl['L1_to_L2'].get(q_name, {}).items():
                    primal_outputs[p_name] = (
                        primal_outputs.get(p_name, 0)
                        + (q_qty * pct / 100.0)
                    )
        else:
            primal_outputs = {
                k: float(v)
                for k, v in payload.get('primal_inputs', {}).items()
                if float(v) > 0
            }

        for primal_name, primal_qty in primal_outputs.items():
            product = self.env['product.product'].search(
                [('name', '=', primal_name)], limit=1
            )
            if product and (
                product.butchery_category == 'trim'
                or 'fat' in primal_name.lower()
            ):
                trim_pool[primal_name] = (
                    trim_pool.get(primal_name, 0) + primal_qty
                )
                continue
            cuts = tpl['L2_to_L3'].get(primal_name, {})
            for cut_name, cut_pct in cuts.items():
                cut_qty = primal_qty * cut_pct / 100.0
                if 'trim' in cut_name.lower():
                    trim_pool[cut_name] = (
                        trim_pool.get(cut_name, 0) + cut_qty
                    )
                else:
                    end_cut_outputs[cut_name] = (
                        end_cut_outputs.get(cut_name, 0) + cut_qty
                    )

        total_trim_kg = sum(trim_pool.values())
        l4_max_possible = {
            l4_name: round(
                total_trim_kg * spec['conversionYield'] / 100.0, 2
            )
            for l4_name, spec in tpl['trim_to_L4'].items()
        }

        # Feasibility check for L5 targets
        feasibility = self._check_feasibility(
            tpl, total_trim_kg,
            payload.get('l4_allocations', {}),
            payload.get('l5_targets', {})
        )

        return {
            'quarter_qtys': {
                k: round(v, 2) for k, v in quarter_qtys.items()
            },
            'primal_outputs': {
                k: round(v, 2) for k, v in primal_outputs.items()
            },
            'end_cut_outputs': {
                k: round(v, 2) for k, v in end_cut_outputs.items()
            },
            'trim_pool': {
                k: round(v, 2) for k, v in trim_pool.items()
            },
            'total_trim_kg': round(total_trim_kg, 2),
            'l4_max_possible': l4_max_possible,
            'feasibility': feasibility,
        }

    def _check_feasibility(
        self, tpl, total_trim_kg, l4_allocations, l5_targets
    ):
        checks = []
        trim_allocated = 0.0

        for l5_name, target_qty in l5_targets.items():
            if not target_qty or target_qty <= 0:
                continue
            recipe = tpl['L5_recipes'].get(l5_name)
            if not recipe:
                continue
            raw_batch = target_qty / (recipe['yieldPct'] / 100.0)
            for inp in recipe.get('inputs', []):
                l4_needed = round(raw_batch * inp['ratio'], 2)
                l4_spec = tpl['trim_to_L4'].get(inp['product'], {})
                trim_needed = round(
                    l4_needed / (
                        l4_spec.get('conversionYield', 92) / 100.0
                    ), 2
                ) if l4_spec else 0
                l4_allocated = l4_allocations.get(inp['product'], 0)
                gap = round(l4_needed - l4_allocated, 2)
                status = (
                    'covered' if gap <= 0
                    else ('nearly' if gap <= l4_needed * 0.1 else 'short')
                )
                checks.append({
                    'l5_product': l5_name,
                    'l4_product': inp['product'],
                    'l4_needed': l4_needed,
                    'l4_allocated': l4_allocated,
                    'trim_needed': trim_needed,
                    'gap': gap,
                    'status': status,
                })

        for l4_name, alloc in l4_allocations.items():
            spec = tpl['trim_to_L4'].get(l4_name, {})
            if spec and alloc:
                trim_allocated += alloc / (
                    spec.get('conversionYield', 92) / 100.0
                )

        return {
            'checks': checks,
            'trim_allocated': round(trim_allocated, 2),
            'trim_balance': round(total_trim_kg - trim_allocated, 2),
        }
