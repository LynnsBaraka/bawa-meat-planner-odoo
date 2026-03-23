from odoo import models, fields


class ProductTemplateExtension(models.Model):
    _inherit = 'product.template'

    butchery_level = fields.Selection([
        ('1', 'L1: Quarter'),
        ('2', 'L2: Primal'),
        ('3', 'L3: End Cut'),
        ('4', 'L4: Value Add'),
        ('5', 'L5: Further Processed'),
    ], string='Butchery Level')

    butchery_category = fields.Selection([
        ('quarter', 'Quarter'),
        ('primal', 'Primal'),
        ('endcut', 'End Cut'),
        ('trim', 'Trim/Fat'),
        ('byproduct', 'By-Product (Bone)'),
        ('valueadd', 'Value Add'),
        ('processed', 'Further Processed'),
    ], string='Butchery Category')

    can_convert_l4 = fields.Boolean(
        string='Can Convert to L4',
        default=False,
        help='Eligible as substitution input when trim pool runs short. '
             'Approved primals only: Chuck, Brisket, Fore Shin, Neck, Shoulder Clod. '
             'Never enable for Ribs, Loin, Rump, Sirloin, or Tenderloin.'
    )
    can_convert_l5 = fields.Boolean(
        string='Can Convert to L5', default=False
    )
    butchery_parent_product_id = fields.Many2one(
        'product.template',
        string='Parent Primal/Quarter',
        help='The parent this product is derived from. For example, '
             'Rump Steak parent is Rump.'
    )
    retail_price_estimate = fields.Float(
        string='Estimated Retail Price (NGN/kg)',
        digits=(10, 2),
        help='Used by the Supply Planner to estimate production value.'
    )
