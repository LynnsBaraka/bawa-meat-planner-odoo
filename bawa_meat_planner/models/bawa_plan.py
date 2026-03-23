from odoo import models, fields, api
from odoo.exceptions import UserError
import json


class BawaPlan(models.Model):
    _name = 'bawa.plan'
    _description = 'Butchery Production Plan'
    _rec_name = 'name'
    _order = 'create_date desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(
        string='Plan Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: self.env['ir.sequence'].next_by_code('bawa.plan')
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('committed', 'Committed'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', tracking=True)

    plan_date = fields.Date(
        string='Plan Date', default=fields.Date.today, required=True
    )
    yield_template_id = fields.Many2one(
        'bawa.yield.template',
        string='Yield Template',
        required=True,
        default=lambda self: self.env['bawa.yield.template'].get_active_template_id()
    )
    plan_result_json = fields.Text(string='Plan Result (JSON)')
    hq_required = fields.Float(string='HQ Required (kg)', digits=(10, 2))
    fq_required = fields.Float(string='FQ Required (kg)', digits=(10, 2))
    recursive_driver_used = fields.Boolean(
        string='Recursive Driver Used', default=False
    )

    order_line_ids = fields.One2many(
        'bawa.plan.order.line', 'plan_id', string='Planning Orders'
    )
    production_ids = fields.Many2many(
        'mrp.production',
        'bawa_plan_production_rel',
        'plan_id', 'production_id',
        string='Manufacturing Orders'
    )
    production_count = fields.Integer(
        compute='_compute_production_count', string='MO Count'
    )
    notes = fields.Text(string='Notes')

    @api.depends('production_ids')
    def _compute_production_count(self):
        for rec in self:
            rec.production_count = len(rec.production_ids)

    def action_view_productions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Manufacturing Orders',
            'res_model': 'mrp.production',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.production_ids.ids)],
        }

    def action_commit(self):
        """Commits the plan and creates mrp.production records."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError('Only draft plans can be committed.')
        if not self.plan_result_json:
            raise UserError('No plan result found. Generate a plan first.')
        plan_result = json.loads(self.plan_result_json)
        productions = self._create_manufacturing_orders(plan_result)
        self.write({
            'state': 'committed',
            'production_ids': [(6, 0, productions.ids)],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': 'Manufacturing Orders',
            'res_model': 'mrp.production',
            'view_mode': 'list,form',
            'domain': [('id', 'in', productions.ids)],
        }

    def _create_manufacturing_orders(self, plan_result):
        Production = self.env['mrp.production']
        created = Production.browse()
        for mo_data in plan_result.get('mos', []):
            product = self.env['product.product'].search(
                [('name', '=', mo_data.get('product'))], limit=1
            )
            if not product:
                continue
            bom = self.env['mrp.bom'].search(
                [('product_tmpl_id', '=', product.product_tmpl_id.id)], limit=1
            )
            production = Production.create({
                'product_id': product.id,
                'product_qty': mo_data.get('qty', 0),
                'product_uom_id': product.uom_id.id,
                'bom_id': bom.id if bom else False,
                'origin': self.name,
                'date_start': self.plan_date,
            })
            if mo_data.get('inputs'):
                production.move_raw_ids.unlink()
                for inp in mo_data['inputs']:
                    inp_product = self.env['product.product'].search(
                        [('name', '=', inp.get('product'))], limit=1
                    )
                    if not inp_product:
                        continue
                    lot = False
                    if inp.get('lot'):
                        lot = self.env['stock.lot'].search(
                            [('name', '=', inp['lot']),
                             ('product_id', '=', inp_product.id)], limit=1
                        )
                    self.env['stock.move'].create({
                        'name': inp_product.name,
                        'product_id': inp_product.id,
                        'product_uom_qty': inp.get('qty', 0),
                        'product_uom': inp_product.uom_id.id,
                        'raw_material_production_id': production.id,
                        'location_id': production.location_src_id.id,
                        'location_dest_id': production.location_dest_id.id,
                        'restrict_lot_id': lot.id if lot else False,
                    })
            created |= production
        return created

    def action_cancel(self):
        self.ensure_one()
        if self.state == 'committed':
            self.production_ids.filtered(
                lambda p: p.state in ('draft', 'confirmed')
            ).action_cancel()
        self.state = 'cancelled'


class BawaPlanOrderLine(models.Model):
    _name = 'bawa.plan.order.line'
    _description = 'Planning Order Line'

    plan_id = fields.Many2one(
        'bawa.plan', string='Plan', required=True, ondelete='cascade'
    )
    product_id = fields.Many2one(
        'product.product', string='Product', required=True
    )
    product_level = fields.Selection([
        ('3', 'L3: End Cut'),
        ('4', 'L4: Value Add'),
        ('5', 'L5: Finished Product'),
    ], string='Level', required=True)
    qty = fields.Float(string='Quantity (kg)', digits=(10, 3), required=True)
    due_date = fields.Date(string='Due Date')
    sale_order_line_id = fields.Many2one(
        'sale.order.line', string='Source Sale Order Line'
    )
