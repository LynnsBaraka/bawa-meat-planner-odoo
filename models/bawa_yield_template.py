from odoo import models, fields, api
from odoo.exceptions import ValidationError
import json


class BawaYieldTemplate(models.Model):
    _name = 'bawa.yield.template'
    _description = 'Butchery Yield Template'
    _rec_name = 'name'

    name = fields.Char(string='Template Name', required=True)
    code = fields.Char(string='Code', required=True)
    active = fields.Boolean(default=True)
    is_active_template = fields.Boolean(
        string='Active Template',
        default=False,
        help='Only one template can be active at a time. This template drives all planning calculations.'
    )
    notes = fields.Text(string='Notes')

    # All yield data stored as JSON for flexibility.
    # The wizard reads these and builds Python dicts at planning time.
    l1_to_l2_json = fields.Text(string='L1 to L2 Yields (JSON)')
    l2_to_l3_json = fields.Text(string='L2 to L3 Yields (JSON)')
    trim_to_l4_json = fields.Text(string='Trim to L4 Specs (JSON)')
    l5_recipes_json = fields.Text(string='L5 Recipes (JSON)')
    trim_candidates_json = fields.Text(string='Trim Candidates (JSON)')

    @api.constrains('is_active_template')
    def _check_single_active_template(self):
        for record in self:
            if record.is_active_template:
                others = self.search([
                    ('is_active_template', '=', True),
                    ('id', '!=', record.id)
                ])
                if others:
                    others.write({'is_active_template': False})

    @api.constrains(
        'l1_to_l2_json', 'l2_to_l3_json', 'trim_to_l4_json',
        'l5_recipes_json', 'trim_candidates_json'
    )
    def _validate_json_fields(self):
        """Validates all JSON fields on save so malformed data is caught
        immediately, not hours later when a plan is generated."""
        field_map = {
            'l1_to_l2_json': 'L1 to L2 Yields',
            'l2_to_l3_json': 'L2 to L3 Yields',
            'trim_to_l4_json': 'Trim to L4 Specs',
            'l5_recipes_json': 'L5 Recipes',
            'trim_candidates_json': 'Trim Candidates',
        }
        for record in self:
            for field_name, label in field_map.items():
                raw = record[field_name]
                if raw and raw.strip():
                    try:
                        json.loads(raw)
                    except json.JSONDecodeError as e:
                        raise ValidationError(
                            f'Invalid JSON in "{label}" tab: {e.msg} '
                            f'at line {e.lineno}, column {e.colno}. '
                            f'Fix the JSON syntax before saving.'
                        )

    def get_template_dict(self):
        """Returns the full template as a Python dict for the planning wizard."""
        self.ensure_one()
        field_map = {
            'L1_to_L2': ('l1_to_l2_json', '{}'),
            'L2_to_L3': ('l2_to_l3_json', '{}'),
            'trim_to_L4': ('trim_to_l4_json', '{}'),
            'L5_recipes': ('l5_recipes_json', '{}'),
            'trim_candidates': ('trim_candidates_json', '[]'),
        }
        result = {}
        for key, (field_name, default) in field_map.items():
            raw = self[field_name] or default
            try:
                result[key] = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValidationError(
                    f'Yield template "{self.name}" has invalid JSON in '
                    f'{field_name}: {e.msg} at line {e.lineno}, column {e.colno}. '
                    f'Go to Configuration > Yield Templates and fix it.'
                )
        return result

    @api.model
    def get_active_template(self):
        template = self.search([('is_active_template', '=', True)], limit=1)
        if not template:
            template = self.search([], limit=1)
        if not template:
            raise ValidationError(
                'No yield template configured. '
                'Go to Meat Planner > Configuration > Yield Templates.'
            )
        return template.get_template_dict()

    @api.model
    def get_active_template_id(self):
        template = self.search([('is_active_template', '=', True)], limit=1)
        return template.id if template else False
